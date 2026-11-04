"""The atlas's buttons (D98): Approve, Deny and Remove, and Authentik's answer to them.

Every ending is a 303 back to the atlas; `deps.py` refuses a click that isn't from the atlas or isn't an approver's.
"""

import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import TypeAdapter, ValidationError

from janus import approve, intake
from janus.approve import Click, Result
from janus.config import Settings
from janus.deps import ApprovedPr, BackendsDep, Requester, atlas_approver
from janus.models import RequestPath

log = logging.getLogger("janus")
REQUEST_PATH = TypeAdapter(RequestPath)

router = APIRouter(prefix="/api/janus/approve", responses={403: {"description": "No SSO identity"}})


def back(
    s: Settings,
    result: Result,
    pr: int | None = None,
    decision: str | None = None,
    by: str | None = None,
    commit: str | None = None,
) -> Response:
    """Send the browser back to the atlas's Requests view with a fixed result code."""
    return RedirectResponse(approve.back_url(s.atlas_origin, result, pr, decision, by, commit), status_code=303)


def done(response: Response) -> Response:
    """End the approval: its cookie goes."""
    response.delete_cookie(approve.COOKIE, path=approve.COOKIE_PATH, secure=True, httponly=True, samesite="strict")
    return response


def said(click: Click) -> str:
    """What the button said, for the atlas's message."""
    return "remove" if click.removing else click.decision


def begin_approval(s: Settings, click: Click) -> Response:
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


@router.post(
    "/remove",
    status_code=303,
    response_class=RedirectResponse,
    dependencies=[Depends(atlas_approver)],
    responses={503: {"description": "Janus has no Gitea bot yet"}},
)
async def remove_start(request: Request, backends: BackendsDep, who: Requester) -> Response:
    """Take the atlas's Remove, a form POST with the request's path.

    The file from main with `removed: true` becomes a PR in the clicker's name, and the click goes on as an Approve
    of that PR - one click, the same check and the same write by Xolotl as any approval (D83: retiring is approved
    like creating).
    """
    form = parse_qs((await request.body()).decode(), max_num_fields=2)
    try:
        path = REQUEST_PATH.validate_python(form["path"][0])
    except KeyError, ValidationError:
        return back(backends.settings, Result.BAD_BUTTON)
    return begin_approval(backends.settings, await intake.removal_click(backends, path, who))


@router.post("/{pr}/start", status_code=303, response_class=RedirectResponse)
async def approve_start(pr: ApprovedPr, request: Request, backends: BackendsDep) -> Response:
    """Take the atlas's Approve / Deny button (a form POST with head + decision): off to Authentik's check."""
    form = parse_qs((await request.body()).decode(), max_num_fields=4)
    try:
        click = approve.new_click(pr, form["head"][0], approve.DECISION.validate_python(form["decision"][0]))
    except KeyError, ValidationError:
        return back(backends.settings, Result.BAD_BUTTON, pr)
    return begin_approval(backends.settings, click)


@router.get("/callback", status_code=303, response_class=RedirectResponse)
async def approve_callback(request: Request, backends: BackendsDep) -> Response:
    """Take Authentik's answer: a code (or login_required) and the state.

    Xolotl gets the code if the state is ours. Every ending goes back to the atlas's Requests view with a fixed
    result code (no page of Janus's own).
    """
    s = backends.settings
    click = approve.read_cookie(request.cookies.get(approve.COOKIE))
    query = request.query_params
    if click is None or not approve.same_click(click, query.get("state")):
        return done(back(s, Result.NOT_YOURS))
    if error := query.get("error"):
        log.warning("approval of PR #%d: Authentik answered %s", click.pr, error)
        if error in {"login_required", "consent_required", "interaction_required"}:
            return done(back(s, Result.SIGN_IN, click.pr, said(click)))
        return done(back(s, Result.AUTHENTIK_NO, click.pr, said(click)))
    code = query.get("code")
    if not code:
        return done(back(s, Result.AUTHENTIK_NO, click.pr, said(click)))
    try:
        outcome = await approve.hand_to_xolotl(backends.http, s.xolotl_approve_url, click, code)
    except approve.NotApprovedError as e:
        log.warning("approval of PR #%d not done: %s %s", click.pr, e.status, e.reason)
        return done(back(s, approve.result_of(e.status), click.pr, said(click)))
    return done(back(s, Result.DONE, outcome.pr, said(click), outcome.by, outcome.commit))
