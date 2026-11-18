"""A request on its way in: checked by Shikigami, its secret values into OpenBao, then a PR on Meru/requests."""

import logging
from datetime import UTC, datetime

import httpx2
from fastapi import HTTPException
from pydantic import SecretStr

from janus.clients import gitea, shikigami
from janus.clients.backends import Backends
from janus.core import remove, request_secrets
from janus.core.propose import ProposeError, pr_from
from janus.models.requests import Rejected, Submission, Submitted

log = logging.getLogger("janus")


async def check_and_propose(
    b: Backends, submission: Submission, who: str, values: dict[str, SecretStr] | None = None
) -> tuple[Submitted, str] | Rejected:
    """Check a request and open its PR, returning the PR and its head commit.

    Shikigami checks the file against main; a new app's secret values go to OpenBao (D99), a running app's change to a
    new entry whose name replaces `entry: new` in the file first (D123); then a valid file becomes a PR (or updates the
    open PR for the same file).
    """
    proposer = b.ready_proposer()
    entry = None
    if any(s.entry == request_secrets.NEW for s in request_secrets.requested(submission.path, submission.content)):
        entry = request_secrets.new_entry(datetime.now(UTC).date())
        submission = submission.model_copy(update={"content": request_secrets.with_entry(submission.content, entry)})
    try:
        verdict = await shikigami.validate(b.http, b.settings.shikigami_validate_url, submission)
    except httpx2.HTTPError as e:
        raise HTTPException(502, f"Shikigami can't check it right now: {type(e).__name__}") from e
    if verdict.problems:
        return Rejected(problems=verdict.problems, checked_against=verdict.commit)
    if problem := await store_secrets(b, submission, values or {}, entry):
        return Rejected(problems=[problem], checked_against=verdict.commit)
    try:
        title, output, head = await proposer.propose(submission, who, verdict.commit)
    except ProposeError as e:
        log.error("proposing %s for %s: %s", submission.path, who, e)
        raise HTTPException(502, "couldn't open the PR on Meru/requests") from e
    pr, url = pr_from(output)
    log.info("%s proposed %s: %s", who, submission.path, url or "PR not named in the push answer")
    return Submitted(pr=pr, url=url, title=title, checked_against=verdict.commit), head


async def _on_main(b: Backends, path: str) -> bool:
    s = b.settings
    try:
        await gitea.read_main(b.http, s.gitea_url, s.org, s.requests_repo, b.bot(), path)
    except gitea.NotOnMainError:
        return False
    return True


async def _change_refused(b: Backends, path: str, on_main: bool) -> str | None:
    """Why a change (D123) can't go ahead: only a running app changes, and one open change per app (D124).

    A second change while a PR for the same file is open would leave the first change's entry behind for Charon.
    """
    if not on_main:
        return "a new app's secrets all go in env - drop `entry: new` (it's for changing a running app, D123)"
    s = b.settings
    try:
        open_prs = await gitea.pending(b.http, s.gitea_url, s.org, s.requests_repo, b.bot(), s.gitea_user)
    except httpx2.HTTPError as e:
        return f"secrets not written: can't see the open PRs ({type(e).__name__})"
    if open_pr := next((p for p in open_prs if path in p.files), None):
        return f"{path} already has an open PR (#{open_pr.pr}) - approve or close it before a change"
    return None


async def store_secrets(
    b: Backends, submission: Submission, values: dict[str, SecretStr], entry: str | None = None
) -> str | None:
    """Put the request's secret values into OpenBao before its PR opens, and return why not, or None.

    A new app's go to its env entry (D99), so a request that reaches Mario has fresh values. A running app takes values
    only as a change (D123): `entry` is the change's new entry, already written into the file in place of `entry: new`,
    and only the names on it get values.
    """
    wanted = request_secrets.requested(submission.path, submission.content)
    if not wanted:
        return f"values for secrets {submission.path} doesn't name" if values else None
    on_main = await _on_main(b, submission.path)
    if entry is not None:
        if problem := await _change_refused(b, submission.path, on_main):
            return problem
        wanted = [x for x in wanted if x.entry == entry]
    elif on_main:
        return (
            "secret values for an app that's already on main - mark the names you change `entry: new` (D123)"
            if (values)
            else None
        )
    app_name = submission.path.removeprefix("apps/").removesuffix(".yaml")
    try:
        filled = request_secrets.values_for(wanted, values)
        if not b.vault.ready:
            raise request_secrets.NotWrittenError("Janus has no OpenBao login yet")
        await b.vault.create_env(app_name, filled, entry or "env")
    except request_secrets.NotWrittenError as e:
        return f"secrets not written: {e}"
    except httpx2.HTTPError as e:
        return f"secrets not written: OpenBao unreachable ({type(e).__name__})"
    log.info(
        "%s: %d secret value(s) written to OpenBao entry %s (%d generated)",
        app_name,
        len(filled),
        entry or "env",
        sum(x.generate for x in wanted),
    )
    return None


async def removal_text(b: Backends, path: str) -> str:
    """The file on main with `removed: true`; a 404, 409, 422 or 502 when it can't be."""
    s = b.settings
    try:
        text = await gitea.read_main(b.http, s.gitea_url, s.org, s.requests_repo, b.bot(), path)
    except gitea.NotOnMainError:
        raise HTTPException(404, f"{path} isn't on main") from None
    except httpx2.HTTPError as e:
        raise HTTPException(502, f"can't read Meru/requests: {type(e).__name__}") from e
    if remove.is_removed(text):
        raise HTTPException(409, f"{path} is already removed")
    try:
        return remove.mark_removed(text)
    except remove.UnremovableError as e:
        log.warning("can't mark %s removed: %s", path, e)
        raise HTTPException(422, f"{path}'s layout can't be marked removed by line: {e}") from e


async def open_removal_pr(b: Backends, path: str, content: str, who: str) -> int:
    """Open the removal PR in `who`'s name and return its number (Mario merges it on Gitea)."""
    answer = await check_and_propose(b, Submission(path=path, content=content), who)
    if isinstance(answer, Rejected):
        log.warning("removing %s: Shikigami says %s", path, answer.problems)
        raise HTTPException(422, f"Shikigami refuses the removal of {path}: {'; '.join(answer.problems)}")
    if answer[0].pr is None:
        raise HTTPException(502, "the PR was opened but its number wasn't in the push answer")
    return answer[0].pr
