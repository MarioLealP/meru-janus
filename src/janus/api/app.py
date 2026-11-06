"""Meru's front door (D82, D92-D96).

Takes request files from the atlas and agents (Authentik identities via Caddy), checks them with Shikigami, and opens
them as PRs on Meru/requests that wait for Mario's approval.

J1: submit + list. J2 (D98): Approve / Deny start Authentik's silent check and hand the one-time code to Xolotl.
J3: the atlas's Remove - Janus opens the removal PR in Mario's name and approves it in the same click (same check).
"""

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx2
import sentry_sdk
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from janus.api import approvals, health, requests
from janus.clients.backends import backends_for
from janus.config import Settings
from janus.core.approve import BackToAtlasError

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
            return PlainTextResponse("forbidden: no SSO identity", status_code=403)
        return await call_next(request)

    @app.exception_handler(BackToAtlasError)
    def back_to_atlas(_: Request, e: BackToAtlasError) -> Response:
        return approvals.back(s, e.result, e.pr, e.decision)

    app.include_router(health.router)
    app.include_router(requests.router)
    app.include_router(approvals.router)
    return app


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
app = create_app()
