"""janus: Meru's front door (D82, D92-D96). Takes request files from the atlas and agents (Authentik identities via
Caddy), checks them with Shikigami, and opens them as PRs on Meru/requests that wait for Mario's approval.

J1: submit + list. Approving (J2) hands Mario's token to Xolotl; until then Mario merges the PRs by hand."""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from janus import gitea, shikigami
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

    return app


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
app = create_app()
