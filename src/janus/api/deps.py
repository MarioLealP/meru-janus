"""The checks routes share, as FastAPI dependencies.

Not here, on purpose:

- the SSO identity check: it's middleware in app.py, so it also covers paths no route answers.
- the Gitea bot check on POST /requests and the Remove: a dependency runs before FastAPI checks the body and before the
  route's own steps, which would turn those routes' 422 and redirect answers into 503s when there's no bot. They ask
  `Backends` where they need it.
"""

from typing import Annotated

from fastapi import Depends, Request

from janus.clients.backends import Backends
from janus.config import Settings
from janus.core import approve
from janus.core.approve import BackToAtlasError, Result


def get_backends(request: Request) -> Backends:
    """The app's backends, set up by the lifespan."""
    return request.app.state.backends


BackendsDep = Annotated[Backends, Depends(get_backends)]


def bot_token(backends: BackendsDep) -> str:
    """The Gitea bot's token; a 503 until there is one."""
    return backends.bot()


BotToken = Annotated[str, Depends(bot_token)]


def requester(request: Request, backends: BackendsDep) -> str:
    """Who asked: the Authentik identity Caddy passed on. The identity middleware has checked it's there."""
    return request.headers[backends.settings.identity_header]


Requester = Annotated[str, Depends(requester)]


def check_click(request: Request, s: Settings, pr: int | None) -> None:
    """Approve / Deny / Remove only from the atlas's own page, and only for approvers."""
    if not approve.from_the_atlas(request.headers.get("sec-fetch-site"), request.headers.get("origin"), s.atlas_origin):
        raise BackToAtlasError(Result.NOT_ATLAS, pr)
    if s.approvers_group not in request.headers.get(s.groups_header, "").split("|"):
        raise BackToAtlasError(Result.NOT_APPROVER, pr)


def atlas_approver(request: Request, backends: BackendsDep) -> None:
    """Check a click that isn't for a PR yet (the Remove)."""
    check_click(request, backends.settings, None)


def atlas_approver_of(pr: int, request: Request, backends: BackendsDep) -> int:
    """Check a click on a PR and hand on its number. A `pr` that isn't a number stops at FastAPI's 422 first."""
    check_click(request, backends.settings, pr)
    return pr


ApprovedPr = Annotated[int, Depends(atlas_approver_of)]
