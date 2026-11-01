"""janus: Meru's front door (D82, D92-D96). Takes request files from the atlas and agents (Authentik identities via
Caddy), checks them with Shikigami, and opens them as PRs on Meru/requests that wait for Mario's approval.

J1: submit + list. J2 (D98): Approve / Deny start Authentik's silent check and hand the one-time code to Xolotl."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import ValidationError

from janus import approve, gitea, shikigami
from janus.config import Settings
from janus.models import Pending, Rejected, Submission, Submitted
from janus.propose import ProposeError, Proposer, pr_from

log = logging.getLogger("janus")
OPEN_PATHS = {"/heartbeat"}  # the probes, from the kubelet - no identity there


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

    @app.post("/api/janus/requests", status_code=201, response_model=Submitted, responses={422: {"model": Rejected}})
    async def submit(submission: Submission, request: Request) -> Submitted | JSONResponse:
        """Propose a request file. Shikigami checks it against main first; a valid one becomes a PR (or updates the
        open PR for the same file). Who asked is the Authentik identity Caddy passed on."""
        bot()
        assert proposer is not None  # bot() said there's a token
        who = request.headers[s.identity_header]
        try:
            verdict = await shikigami.validate(state["http"], s.shikigami_validate_url, submission)
        except httpx2.HTTPError as e:
            raise HTTPException(502, f"Shikigami can't check it right now: {type(e).__name__}") from e
        if verdict.problems:
            rejected = Rejected(problems=verdict.problems, checked_against=verdict.commit)
            return JSONResponse(rejected.model_dump(), status_code=422)
        try:
            title, output = await proposer.propose(submission, who, verdict.commit)
        except ProposeError as e:
            log.error("proposing %s for %s: %s", submission.path, who, e)
            raise HTTPException(502, "couldn't open the PR on Meru/requests") from e
        pr, url = pr_from(output)
        log.info("%s proposed %s: %s", who, submission.path, url or "PR not named in the push answer")
        return Submitted(pr=pr, url=url, title=title, checked_against=verdict.commit)

    @app.post("/api/janus/approve/{pr}/start")
    async def approve_start(pr: int, request: Request) -> Response:
        """The atlas's Approve / Deny button (a form POST with head + decision): off to Authentik's silent check."""
        if not approve.from_the_atlas(
            request.headers.get("sec-fetch-site"), request.headers.get("origin"), s.atlas_origin
        ):
            return back(approve.Result.NOT_ATLAS, pr)
        groups = request.headers.get(s.groups_header, "").split("|")
        if s.approvers_group not in groups:
            return back(approve.Result.NOT_APPROVER, pr)
        form = parse_qs((await request.body()).decode(), max_num_fields=4)
        try:
            click = approve.new_click(pr, form["head"][0], form["decision"][0])  # ty: ignore[invalid-argument-type]
        except KeyError, ValidationError:
            return back(approve.Result.BAD_BUTTON, pr)
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
                return done(back(approve.Result.SIGN_IN, click.pr, click.decision))
            return done(back(approve.Result.AUTHENTIK_NO, click.pr, click.decision))
        code = query.get("code")
        if not code:
            return done(back(approve.Result.AUTHENTIK_NO, click.pr, click.decision))
        try:
            outcome = await approve.hand_to_xolotl(state["http"], s.xolotl_approve_url, click, code)
        except approve.NotApproved as e:
            log.warning("approval of PR #%d not done: %s %s", click.pr, e.status, e.reason)
            return done(back(approve.result_of(e.status), click.pr, click.decision))
        return done(back(approve.Result.DONE, outcome.pr, outcome.decision, outcome.by, outcome.commit))

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
