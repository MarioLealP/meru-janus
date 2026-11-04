"""A request on its way in: checked by Shikigami, its secret values into OpenBao, then a PR on Meru/requests."""

import logging

import httpx2
from fastapi import HTTPException
from pydantic import SecretStr

from janus import approve, gitea, remove, request_secrets, shikigami
from janus.approve import BackToAtlasError, Click, Result
from janus.backends import Backends
from janus.models import Rejected, Submission, Submitted
from janus.propose import ProposeError, pr_from

log = logging.getLogger("janus")


async def check_and_propose(
    b: Backends, submission: Submission, who: str, values: dict[str, SecretStr] | None = None
) -> tuple[Submitted, str] | Rejected:
    """Check a request and open its PR, returning the PR and its head commit.

    Shikigami checks the file against main; a new app's secret values go to OpenBao (D99); then a valid file becomes a
    PR (or updates the open PR for the same file).
    """
    proposer = b.ready_proposer()
    try:
        verdict = await shikigami.validate(b.http, b.settings.shikigami_validate_url, submission)
    except httpx2.HTTPError as e:
        raise HTTPException(502, f"Shikigami can't check it right now: {type(e).__name__}") from e
    if verdict.problems:
        return Rejected(problems=verdict.problems, checked_against=verdict.commit)
    if problem := await store_secrets(b, submission, values or {}):
        return Rejected(problems=[problem], checked_against=verdict.commit)
    try:
        title, output, head = await proposer.propose(submission, who, verdict.commit)
    except ProposeError as e:
        log.error("proposing %s for %s: %s", submission.path, who, e)
        raise HTTPException(502, "couldn't open the PR on Meru/requests") from e
    pr, url = pr_from(output)
    log.info("%s proposed %s: %s", who, submission.path, url or "PR not named in the push answer")
    return Submitted(pr=pr, url=url, title=title, checked_against=verdict.commit), head


async def store_secrets(b: Backends, submission: Submission, values: dict[str, SecretStr]) -> str | None:
    """Put a new app's secret values into OpenBao before its PR opens (D99), and return why not, or None.

    That way a request that reaches Mario has fresh values. Only for apps not on main: changing a running app's secrets
    is later work, and OpenBao would refuse it anyway.
    """
    s = b.settings
    wanted = request_secrets.requested(submission.path, submission.content)
    if not wanted:
        return f"values for secrets {submission.path} doesn't name" if values else None
    try:
        await gitea.read_main(b.http, s.gitea_url, s.org, s.requests_repo, b.bot(), submission.path)
    except gitea.NotOnMainError:
        pass
    else:
        return "secret values for an app that's already on main - changing them comes later (D99)" if values else None
    app_name = submission.path.removeprefix("apps/").removesuffix(".yaml")
    try:
        filled = request_secrets.values_for(wanted, values)
        if not b.vault.ready:
            raise request_secrets.NotWrittenError("Janus has no OpenBao login yet")
        await b.vault.create_env(app_name, filled)
    except request_secrets.NotWrittenError as e:
        return f"secrets not written: {e}"
    except httpx2.HTTPError as e:
        return f"secrets not written: OpenBao unreachable ({type(e).__name__})"
    log.info(
        "%s: %d secret value(s) written to OpenBao (%d generated)",
        app_name,
        len(filled),
        sum(x.generate for x in wanted),
    )
    return None


async def removal_text(b: Backends, path: str) -> str:
    """The file on main with `removed: true`; a click that can't go ahead ends at the atlas."""
    s = b.settings
    try:
        text = await gitea.read_main(b.http, s.gitea_url, s.org, s.requests_repo, b.bot(), path)
    except gitea.NotOnMainError:
        raise BackToAtlasError(Result.NOT_ON_MAIN, decision="remove") from None
    except httpx2.HTTPError:
        raise BackToAtlasError(Result.DOWN, decision="remove") from None
    if remove.is_removed(text):
        raise BackToAtlasError(Result.ALREADY_REMOVED, decision="remove")
    try:
        return remove.mark_removed(text)
    except remove.UnremovableError as e:
        log.warning("can't mark %s removed: %s", path, e)
        raise BackToAtlasError(Result.BAD_BUTTON, decision="remove") from None


async def removal_click(b: Backends, path: str, who: str) -> Click:
    """Open the removal PR in `who`'s name and make the Approve of it: one click, as D83 wants."""
    submission = Submission(path=path, content=await removal_text(b, path))
    try:
        answer = await check_and_propose(b, submission, who)
    except HTTPException:
        raise BackToAtlasError(Result.DOWN, decision="remove") from None
    if isinstance(answer, Rejected):
        log.warning("removing %s: Shikigami says %s", path, answer.problems)
        raise BackToAtlasError(Result.PROBLEMS, decision="remove")
    submitted, head = answer
    if submitted.pr is None:
        raise BackToAtlasError(Result.DOWN, decision="remove")
    return approve.new_click(submitted.pr, head, "approve", removing=True)
