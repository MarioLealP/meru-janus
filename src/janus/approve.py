"""The atlas's Approve / Deny (D98): Janus starts the silent check at Authentik and hands the answer to Xolotl. It
holds no secret and decides nothing - the one-time code is only worth something to Xolotl, which redeems and checks
it. What Janus guards is that the click was Mario's, on the atlas page:

- start is a POST from the atlas itself (Sec-Fetch-Site / Origin); a cross-site POST also lacks the Lax session
  cookie, so forward-auth stops it before Janus
- start sets a 2-minute HttpOnly, SameSite=Strict cookie with a random state and what was clicked; the callback only
  passes on a code whose state matches it. A page that sends Mario's browser straight to Authentik's authorize URL
  can't set that cookie, so its code goes nowhere.
"""

import hashlib
import secrets
from typing import Annotated, Literal
from urllib.parse import urlencode

import httpx2
from pydantic import Field, StringConstraints, ValidationError

from janus.models import Incoming, Value

Decision = Literal["approve", "deny"]
Head = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
COOKIE = "janus_approval"
COOKIE_PATH = "/api/janus/approve"
COOKIE_SECONDS = 120


class Click(Value):
    """What the button was for, kept in the cookie between start and callback."""

    state: Annotated[str, StringConstraints(min_length=32, max_length=64)]
    pr: Annotated[int, Field(gt=0)]
    head: Head
    decision: Decision


class Outcome(Incoming):
    """Xolotl's answer to an approval."""

    pr: int
    decision: Decision
    by: str
    commit: str | None


class NotApproved(Exception):
    """Xolotl said no (or couldn't be asked): its status and reason, shown to Mario."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def nonce(org: str, repo: str, pr: int, head: str, decision: Decision) -> str:
    """The same fingerprint Xolotl expects in the ID token (xolotl approval.nonce)."""
    return hashlib.sha256(f"{org}/{repo}#{pr}@{head}:{decision}".encode()).hexdigest()


def new_click(pr: int, head: str, decision: Decision) -> Click:
    return Click(state=secrets.token_urlsafe(32), pr=pr, head=head, decision=decision)


def authorize_url(authorize: str, client_id: str, redirect_uri: str, click: Click, org: str, repo: str) -> str:
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "openid profile",
        "prompt": "none",  # Mario is signed in: no screen; if not, Authentik answers login_required
        "state": click.state,
        "nonce": nonce(org, repo, click.pr, click.head, click.decision),
    }
    return f"{authorize}?{urlencode(query)}"


def read_cookie(value: str | None) -> Click | None:
    if not value:
        return None
    try:
        return Click.model_validate_json(value)
    except ValidationError:
        return None


def same_click(click: Click | None, state: str | None) -> bool:
    return click is not None and state is not None and secrets.compare_digest(click.state, state)


def from_the_atlas(sec_fetch_site: str | None, origin: str | None, atlas_origin: str) -> bool:
    """A browser POST from the atlas's own page: modern browsers send Sec-Fetch-Site; Origin is the fallback."""
    if sec_fetch_site is not None:
        return sec_fetch_site == "same-origin"
    return origin == atlas_origin


async def hand_to_xolotl(http: httpx2.AsyncClient, url: str, click: Click, code: str) -> Outcome:
    body = {"pr": click.pr, "head": click.head, "decision": click.decision, "code": code}
    try:
        response = await http.post(url, json=body)
    except httpx2.HTTPError as e:
        raise NotApproved(502, f"Xolotl can't be reached: {type(e).__name__}") from e
    if response.status_code != 200:
        try:
            reason = str(response.json().get("detail", response.text))
        except ValueError:
            reason = response.text[:300]
        raise NotApproved(response.status_code, reason)
    return Outcome.model_validate_json(response.content)
