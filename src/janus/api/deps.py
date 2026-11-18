"""The checks routes share, as FastAPI dependencies.

Not here, on purpose:

- the SSO identity check: it's middleware in app.py, so it also covers paths no route answers.
- the Gitea bot check on POST /requests and the Remove: a dependency runs before FastAPI checks the body and before the
  route's own steps, which would turn those routes' 422 answers into 503s when there's no bot. They ask
  `Backends` where they need it.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from janus.clients.backends import Backends


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


def same_origin(request: Request, backends: BackendsDep) -> None:
    """Refuse a browser POST that didn't come from the atlas's own page (a cross-site form).

    Modern browsers send Sec-Fetch-Site; Origin is the fallback. A caller without either (the janus CLI) passes.
    """
    site, origin = request.headers.get("sec-fetch-site"), request.headers.get("origin")
    if site is None and origin is None:
        return
    if (site == "same-origin") if site is not None else (origin == backends.settings.atlas_origin):
        return
    raise HTTPException(403, "this button only works from the atlas's own page")
