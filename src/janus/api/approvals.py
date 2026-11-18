"""The atlas's Approve, Deny and Remove buttons, now only a way to the Gitea PR page.

Mario merges a request PR on Gitea (the merge button; Gitea's settings, declared in Tofu, decide who may and what
must pass) and closes it there to deny. The atlas's buttons keep posting here until the atlas is retired, so:

- Approve and Deny (both post to `/{pr}/start`) answer 303 See Other to the PR's page. No PR lookup: a redirect to
  a page that doesn't exist or isn't a request PR changes nothing, and Gitea answers for itself. They hold no
  credential, call nobody and need no check: nothing changes, so nothing can be forged (the PR number is an integer
  or FastAPI's 422).
- Remove opens the `removed: true` PR for a request on main and answers 303 to it. That one does change something
  (a new PR), so it keeps the cheap same-origin check against a cross-site form POST.
"""

from pathlib import PurePosixPath
from urllib.parse import parse_qs

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import TypeAdapter, ValidationError

from janus.api.deps import BackendsDep, Requester, same_origin
from janus.clients import gitea
from janus.core import intake
from janus.models.requests import RequestPath

REQUEST_PATH = TypeAdapter(RequestPath)

router = APIRouter(prefix="/api/janus/approve", responses={403: {"description": "No SSO identity"}})


@router.post("/{pr}/start", status_code=303, response_class=RedirectResponse)
async def approve_start(pr: int, backends: BackendsDep) -> Response:
    """The atlas's Approve and Deny buttons (one form POST, a `decision` field Janus no longer needs): to the PR's page.

    Mario merges it there to approve and closes it to deny.
    """
    return RedirectResponse(backends.settings.pr_url(pr), status_code=303)


@router.post(
    "/remove",
    status_code=303,
    response_class=RedirectResponse,
    dependencies=[Depends(same_origin)],
    responses={
        404: {"description": "No such request on main"},
        409: {"description": "Already removed, or the file has an open PR"},
        422: {"description": "Not a request path, one of Meru's own parts, or Shikigami's problems"},
        502: {"description": "An outside system didn't answer"},
        503: {"description": "Janus has no Gitea bot yet"},
    },
)
async def remove_start(request: Request, backends: BackendsDep, who: Requester) -> Response:
    """The atlas's Remove, a form POST with the request's path: open the removal PR and go to its page.

    The file from main gets `removed: true` in a PR in the clicker's name; Mario merges it on Gitea. The retention
    clock is Shikigami's to start, so nothing is stamped here.
    """
    form = parse_qs((await request.body()).decode(), max_num_fields=2)
    try:
        path = REQUEST_PATH.validate_python(form["path"][0])
    except KeyError, ValidationError:
        raise HTTPException(422, "the form needs a request path (apps/<name>.yaml or databases/<name>.yaml)") from None
    s = backends.settings
    if PurePosixPath(path).stem in s.platform_requests:
        raise HTTPException(422, f"{path} is one of Meru's own parts and can't be removed here")
    token = backends.bot()
    text = await intake.removal_text(backends, path)
    try:
        open_prs = await gitea.pending(backends.http, s.gitea_url, s.org, s.requests_repo, token, s.gitea_user)
    except httpx2.HTTPError as e:
        raise HTTPException(502, f"can't read Meru/requests: {type(e).__name__}") from e
    if open_pr := next((p for p in open_prs if path in p.files), None):
        raise HTTPException(409, f"{path} already has an open PR (#{open_pr.pr}) - merge or close it first")
    pr = await intake.open_removal_pr(backends, path, text, who)
    return RedirectResponse(s.pr_url(pr), status_code=303)
