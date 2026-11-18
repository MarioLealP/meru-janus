"""Meru's front door (D82, D92-D96).

Takes request files from the atlas and agents (Authentik identities via Caddy), checks them with Shikigami, and opens
them as PRs on Meru/requests that wait for Mario's approval.

J1: submit + list. Approving is Mario's merge on the Gitea PR page (platform rework phase 3): the atlas's Approve /
Deny buttons redirect there, and its Remove opens the removal PR and redirects to it.
"""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx2
import sentry_sdk
from fastapi import FastAPI, Request, Response

from janus.api import approvals, health, problems, requests
from janus.api.logs import NoQueryString
from janus.clients.backends import backends_for
from janus.config import Settings

log = logging.getLogger("janus")
OPEN_PATHS = {"/heartbeat"}  # the probes, from the kubelet - no identity there


# Crash reports to GlitchTip (D125): SENTRY_DSN comes from nuwa-app's glitchtip.dsn; unset, the SDK stays off.
# No local variables or request bodies in a report - either can hold a secret.
sentry_sdk.init(include_local_variables=False, max_request_body_size="never")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; `settings` come from the environment unless given."""
    s = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if not s.gitea_token:
            log.warning("no Gitea token yet: listing and proposing answer 503")
        async with httpx2.AsyncClient(timeout=s.http_timeout) as http:
            app.state.backends = backends_for(s, http)
            yield

    app = FastAPI(title="janus", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    # Middleware, not a dependency: it also answers for paths no route knows, so nothing is open by being forgotten.
    @app.middleware("http")
    async def identity_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path not in OPEN_PATHS and not request.headers.get(s.identity_header):
            return problems.response(
                problems.Problem(title="Forbidden", status=403, detail="forbidden: no SSO identity")
            )
        return await call_next(request)

    problems.install(app)

    app.include_router(health.router)
    app.include_router(requests.router)
    app.include_router(approvals.router)
    return app


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logging.getLogger("uvicorn.access").addFilter(NoQueryString())  # uvicorn's dictConfig leaves logger filters alone
app = create_app()
