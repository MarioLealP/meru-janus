"""janus: Meru's front door (D82, D92-D96). Takes request files from the atlas and agents (Authentik identities via
Caddy), checks them with Shikigami, and opens them as PRs on Meru/requests that wait for Mario's approval.

J1: submit + list. J2 (D98): Approve / Deny start Authentik's silent check and hand the one-time code to Xolotl.
J3: the atlas's Remove - Janus opens the removal PR in Mario's name and approves it in the same click (same check)."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import SecretStr, TypeAdapter, ValidationError

from janus import approve, gitea, remove, request_secrets, shikigami
from janus.config import Settings
from janus.models import OnMain, Pending, Rejected, RequestPath, Submission, Submit, Submitted
from janus.propose import ProposeError, Proposer, pr_from

log = logging.getLogger("janus")
OPEN_PATHS = {"/heartbeat"}  # the probes, from the kubelet - no identity there
REQUEST_PATH = TypeAdapter(RequestPath)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()
    token = s.gitea_token.get_secret_value() if s.gitea_token else None
    state: dict[str, httpx2.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        if token is None:
            log.warning("no Gitea token yet: listing and proposing answer 503")
        async with httpx2.AsyncClient(timeout=s.http_timeout) as http:
            state["http"] = http
            yield

    app = FastAPI(title="janus", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    repo_url = f"{s.gitea_url}/{s.org}/{s.requests_repo}.git"
    proposer = Proposer(repo_url, s.gitea_user, token, s.work_dir, s.git_timeout) if token else None
    vault = request_secrets.Vault(s.openbao_addr, s.openbao_role, s.openbao_dir, s.http_timeout)

    @app.middleware("http")
    async def identity_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path not in OPEN_PATHS and not request.headers.get(s.identity_header):
            return PlainTextResponse("forbidden: no SSO identity", status_code=403)
        return await call_next(request)

    def bot() -> str:
        if token is None:
            raise HTTPException(503, "Janus has no Gitea bot yet")
        return token

    @app.get("/heartbeat", response_class=PlainTextResponse)
    def heartbeat() -> str:
        """Meru's liveness and readiness probes (nuwa-app's heartbeat.path)."""
        return "ok"

    @app.get("/api/janus/requests")
    async def list_pending() -> list[Pending]:
        """Requests waiting for approval: the open PRs on Meru/requests (D93)."""
        try:
            return await gitea.pending(state["http"], s.gitea_url, s.org, s.requests_repo, bot(), s.gitea_user)
        except httpx2.HTTPError as e:
            raise HTTPException(502, f"can't read Meru/requests: {type(e).__name__}") from e

    @app.get("/api/janus/apps")
    async def list_on_main() -> list[OnMain]:
        """What's on Meru/requests main: the apps and databases Meru runs, and the retired ones."""
        try:
            return await gitea.on_main(state["http"], s.gitea_url, s.org, s.requests_repo, bot())
        except httpx2.HTTPError as e:
            raise HTTPException(502, f"can't read Meru/requests: {type(e).__name__}") from e

    async def check_and_propose(
        submission: Submission, who: str, values: dict[str, SecretStr] | None = None
    ) -> tuple[Submitted, str] | Rejected:
        """Shikigami checks the file against main; a new app's secret values go to OpenBao (D99); then a valid file
        becomes a PR (or updates the open PR for the same file). Returns the PR and its head commit."""
        bot()
        assert proposer is not None  # bot() said there's a token
        try:
            verdict = await shikigami.validate(state["http"], s.shikigami_validate_url, submission)
        except httpx2.HTTPError as e:
            raise HTTPException(502, f"Shikigami can't check it right now: {type(e).__name__}") from e
        if verdict.problems:
            return Rejected(problems=verdict.problems, checked_against=verdict.commit)
        if problem := await store_secrets(submission, values or {}):
            return Rejected(problems=[problem], checked_against=verdict.commit)
        try:
            title, output, head = await proposer.propose(submission, who, verdict.commit)
        except ProposeError as e:
            log.error("proposing %s for %s: %s", submission.path, who, e)
            raise HTTPException(502, "couldn't open the PR on Meru/requests") from e
        pr, url = pr_from(output)
        log.info("%s proposed %s: %s", who, submission.path, url or "PR not named in the push answer")
        return Submitted(pr=pr, url=url, title=title, checked_against=verdict.commit), head

    async def store_secrets(submission: Submission, values: dict[str, SecretStr]) -> str | None:
        """A new app's secret values into OpenBao before its PR opens (D99) - so a request that reaches Mario has
        fresh values. Returns why not, or None. Only for apps not on main: changing a running app's secrets is
        later work, and OpenBao would refuse it anyway."""
        wanted = request_secrets.requested(submission.path, submission.content)
        if not wanted:
            return f"values for secrets {submission.path} doesn't name" if values else None
        try:
            await gitea.read_main(state["http"], s.gitea_url, s.org, s.requests_repo, bot(), submission.path)
        except gitea.NotOnMain:
            pass
        else:
            return (
                "secret values for an app that's already on main - changing them comes later (D99)" if values else None
            )
        app_name = submission.path.removeprefix("apps/").removesuffix(".yaml")
        try:
            filled = request_secrets.values_for(wanted, values)
            if not vault.ready:
                raise request_secrets.NotWritten("Janus has no OpenBao login yet")
            await vault.create_env(app_name, filled)
        except request_secrets.NotWritten as e:
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

    @app.post("/api/janus/requests", status_code=201, response_model=Submitted, responses={422: {"model": Rejected}})
    async def submit(body: Submit, request: Request) -> Submitted | JSONResponse:
        """Propose a request file, with the values of the secrets it names (D99). Shikigami checks it against main
        first; a new app's secret values go to OpenBao; then a valid file becomes a PR (or updates the open PR for the
        same file). Who asked is the Authentik identity Caddy passed on."""
        answer = await check_and_propose(body.submission(), request.headers[s.identity_header], body.secrets)
        if isinstance(answer, Rejected):
            return JSONResponse(answer.model_dump(), status_code=422)
        return answer[0]

    def refused(request: Request, pr: int | None = None) -> Response | None:
        """Approve / Deny / Remove only from the atlas's own page, and only for approvers."""
        if not approve.from_the_atlas(
            request.headers.get("sec-fetch-site"), request.headers.get("origin"), s.atlas_origin
        ):
            return back(approve.Result.NOT_ATLAS, pr)
        if s.approvers_group not in request.headers.get(s.groups_header, "").split("|"):
            return back(approve.Result.NOT_APPROVER, pr)
        return None

    def begin_approval(click: approve.Click) -> Response:
        """Off to Authentik's silent check, with the click in a short HttpOnly cookie for the callback."""
        url = approve.authorize_url(
            s.authorize_url, s.approvals_client_id, s.approvals_redirect_uri, click, s.org, s.requests_repo
        )
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            approve.COOKIE,
            click.model_dump_json(),
            max_age=approve.COOKIE_SECONDS,
            path=approve.COOKIE_PATH,
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/api/janus/approve/remove")
    async def remove_start(request: Request) -> Response:
        """The atlas's Remove (a form POST with the request's path): the file from main with `removed: true` becomes
        a PR in the clicker's name, and the click goes on as an Approve of that PR - one click, the same check and
        the same write by Xolotl as any approval (D83: retiring is approved like creating)."""
        if no := refused(request):
            return no
        form = parse_qs((await request.body()).decode(), max_num_fields=2)
        try:
            path = REQUEST_PATH.validate_python(form["path"][0])
        except KeyError, ValidationError:
            return back(approve.Result.BAD_BUTTON)
        try:
            text = await gitea.read_main(state["http"], s.gitea_url, s.org, s.requests_repo, bot(), path)
        except gitea.NotOnMain:
            return back(approve.Result.NOT_ON_MAIN, decision="remove")
        except httpx2.HTTPError:
            return back(approve.Result.DOWN, decision="remove")
        if remove.is_removed(text):
            return back(approve.Result.ALREADY_REMOVED, decision="remove")
        try:
            submission = Submission(path=path, content=remove.mark_removed(text))
        except remove.Unremovable as e:
            log.warning("can't mark %s removed: %s", path, e)
            return back(approve.Result.BAD_BUTTON, decision="remove")
        try:
            answer = await check_and_propose(submission, request.headers[s.identity_header])
        except HTTPException:
            return back(approve.Result.DOWN, decision="remove")
        if isinstance(answer, Rejected):
            log.warning("removing %s: Shikigami says %s", path, answer.problems)
            return back(approve.Result.PROBLEMS, decision="remove")
        submitted, head = answer
        if submitted.pr is None:
            return back(approve.Result.DOWN, decision="remove")
        return begin_approval(approve.new_click(submitted.pr, head, "approve", removing=True))

    @app.post("/api/janus/approve/{pr}/start")
    async def approve_start(pr: int, request: Request) -> Response:
        """The atlas's Approve / Deny button (a form POST with head + decision): off to Authentik's silent check."""
        if no := refused(request, pr):
            return no
        form = parse_qs((await request.body()).decode(), max_num_fields=4)
        try:
            click = approve.new_click(pr, form["head"][0], form["decision"][0])  # ty: ignore[invalid-argument-type]
        except KeyError, ValidationError:
            return back(approve.Result.BAD_BUTTON, pr)
        return begin_approval(click)

    @app.get("/api/janus/approve/callback")
    async def approve_callback(request: Request) -> Response:
        """Authentik's answer: a code (or login_required) and the state; Xolotl gets the code if the state is ours.
        Every ending goes back to the atlas's Requests view with a fixed result code (no page of Janus's own)."""
        click = approve.read_cookie(request.cookies.get(approve.COOKIE))
        query = request.query_params
        if not approve.same_click(click, query.get("state")):
            return done(back(approve.Result.NOT_YOURS))
        assert click is not None  # same_click said so
        if error := query.get("error"):
            log.warning("approval of PR #%d: Authentik answered %s", click.pr, error)
            if error in ("login_required", "consent_required", "interaction_required"):
                return done(back(approve.Result.SIGN_IN, click.pr, said(click)))
            return done(back(approve.Result.AUTHENTIK_NO, click.pr, said(click)))
        code = query.get("code")
        if not code:
            return done(back(approve.Result.AUTHENTIK_NO, click.pr, said(click)))
        try:
            outcome = await approve.hand_to_xolotl(state["http"], s.xolotl_approve_url, click, code)
        except approve.NotApproved as e:
            log.warning("approval of PR #%d not done: %s %s", click.pr, e.status, e.reason)
            return done(back(approve.result_of(e.status), click.pr, said(click)))
        return done(back(approve.Result.DONE, outcome.pr, said(click), outcome.by, outcome.commit))

    def said(click: approve.Click) -> str:
        return "remove" if click.removing else click.decision

    def back(
        result: approve.Result,
        pr: int | None = None,
        decision: str | None = None,
        by: str | None = None,
        commit: str | None = None,
    ) -> Response:
        return RedirectResponse(approve.back_url(s.atlas_origin, result, pr, decision, by, commit), status_code=303)

    def done(response: Response) -> Response:
        response.delete_cookie(approve.COOKIE, path=approve.COOKIE_PATH, secure=True, httponly=True, samesite="strict")
        return response

    return app


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
app = create_app()
