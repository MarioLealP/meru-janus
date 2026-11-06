"""The requests: what's waiting, what's on main, and proposing a new one."""

from typing import Any

import httpx2
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from janus.api.deps import BackendsDep, BotToken, Requester
from janus.clients import gitea
from janus.core import intake
from janus.models.requests import OnMain, Pending, Rejected, Submit, Submitted

ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"description": "No SSO identity"},
    502: {"description": "An outside system didn't answer"},
    503: {"description": "Janus has no Gitea bot yet"},
}

router = APIRouter(prefix="/api/janus", responses=ERRORS)


@router.get("/requests", status_code=200)
async def list_pending(backends: BackendsDep, token: BotToken) -> list[Pending]:
    """Requests waiting for approval: the open PRs on Meru/requests (D93)."""
    s = backends.settings
    try:
        return await gitea.pending(backends.http, s.gitea_url, s.org, s.requests_repo, token, s.gitea_user)
    except httpx2.HTTPError as e:
        raise HTTPException(502, f"can't read Meru/requests: {type(e).__name__}") from e


@router.get("/apps", status_code=200)
async def list_on_main(backends: BackendsDep, token: BotToken) -> list[OnMain]:
    """What's on Meru/requests main: the apps and databases Meru runs, and the retired ones."""
    s = backends.settings
    try:
        return await gitea.on_main(backends.http, s.gitea_url, s.org, s.requests_repo, token)
    except httpx2.HTTPError as e:
        raise HTTPException(502, f"can't read Meru/requests: {type(e).__name__}") from e


@router.post("/requests", status_code=201, response_model=Submitted, responses={422: {"model": Rejected}})
async def submit(body: Submit, backends: BackendsDep, who: Requester) -> Submitted | JSONResponse:
    """Propose a request file, with the values of the secrets it names (D99).

    Shikigami checks it against main first; a new app's secret values go to OpenBao; then a valid file becomes a PR
    (or updates the open PR for the same file). Who asked is the Authentik identity Caddy passed on.
    """
    answer = await intake.check_and_propose(backends, body.submission(), who, body.secrets)
    if isinstance(answer, Rejected):
        return JSONResponse(answer.model_dump(), status_code=422)
    return answer[0]
