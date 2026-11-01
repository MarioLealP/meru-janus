from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from janus import app as app_module
from janus.app import create_app
from janus.approve import COOKIE, Click, NotApproved, Outcome, nonce
from janus.config import Settings

HEAD = "a" * 40
MARIO = {"X-Authentik-Username": "akadmin", "X-Authentik-Groups": "clockmost-admins|meru-approvers"}
FROM_ATLAS = MARIO | {"Sec-Fetch-Site": "same-origin"}
FORM = {"head": HEAD, "decision": "approve"}


def client() -> TestClient:
    # https: the approval cookie is Secure, so the test browser only sends it back over https
    return TestClient(create_app(Settings(gitea_token="tok")), base_url="https://atlas.clockmost.com")


def start(c: TestClient, headers: dict[str, str] = FROM_ATLAS, form: dict[str, str] = FORM):
    return c.post("/api/janus/approve/7/start", data=form, headers=headers, follow_redirects=False)


def test_start_sends_the_browser_to_authentik_silently_with_this_clicks_fingerprint():
    with client() as c:
        r = start(c)
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://auth.clockmost.com/application/o/authorize/"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["prompt"] == "none" and q["client_id"] == "xolotl-approvals" and q["response_type"] == "code"
    assert q["redirect_uri"] == "https://atlas.clockmost.com/api/janus/approve/callback"
    assert q["nonce"] == nonce("Meru", "requests", 7, HEAD, "approve")
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE}=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" in cookie
    assert "Path=/api/janus/approve" in cookie


@pytest.mark.parametrize(
    ("headers", "why"),
    [
        (MARIO | {"Sec-Fetch-Site": "cross-site"}, "Not from the atlas"),
        (MARIO | {"Sec-Fetch-Site": "same-site"}, "Not from the atlas"),  # another clockmost.com app's page
        (MARIO | {"Origin": "https://evil.example"}, "Not from the atlas"),
        (FROM_ATLAS | {"X-Authentik-Groups": "clockmost-admins"}, "Not an approver"),
    ],
)
def test_start_only_from_the_atlas_page_and_only_for_approvers(headers: dict[str, str], why: str):
    with client() as c:
        r = start(c, headers=headers)
    assert r.status_code == 403 and why in r.text and "set-cookie" not in r.headers


@pytest.mark.parametrize("form", [{"head": "nope", "decision": "approve"}, {"head": HEAD, "decision": "merge"}, {}])
def test_a_button_without_a_commit_and_decision_starts_nothing(form: dict[str, str]):
    with client() as c:
        assert start(c, form=form).status_code == 400


def state_of(r) -> str:
    return parse_qs(urlsplit(r.headers["location"]).query)["state"][0]


def test_the_callback_hands_the_code_to_xolotl_with_what_was_clicked(monkeypatch: pytest.MonkeyPatch):
    seen = []

    async def hand(_http, url: str, click: Click, code: str) -> Outcome:
        seen.append((url, click.pr, click.head, click.decision, code))
        return Outcome(pr=7, decision="approve", by="akadmin", commit="c0ffee")

    monkeypatch.setattr(app_module.approve, "hand_to_xolotl", hand)
    with client() as c:
        state = state_of(start(c))
        r = c.get("/api/janus/approve/callback", params={"code": "one-time", "state": state}, headers=MARIO)
    assert r.status_code == 200 and "Approved by akadmin - on main in c0ffee" in r.text
    assert seen == [("http://xolotl.xolotl.svc/approve", 7, HEAD, "approve", "one-time")]
    assert f'{COOKIE}=""' in r.headers["set-cookie"]  # used once: the cookie goes


def test_a_code_from_an_approval_not_started_here_goes_nowhere(monkeypatch: pytest.MonkeyPatch):
    async def hand(*_args):
        raise AssertionError("must not reach Xolotl")

    monkeypatch.setattr(app_module.approve, "hand_to_xolotl", hand)
    with client() as c:
        # a page sent Mario's browser straight to Authentik: a code and a state, but no cookie from a start
        r = c.get("/api/janus/approve/callback", params={"code": "c", "state": "x" * 43}, headers=MARIO)
        assert r.status_code == 400 and "Not your click" in r.text
        start(c)  # a real start, then an answer with someone else's state
        r = c.get("/api/janus/approve/callback", params={"code": "c", "state": "y" * 43}, headers=MARIO)
        assert r.status_code == 400


def test_signed_out_at_authentik_says_sign_in_again():
    with client() as c:
        state = state_of(start(c))
        r = c.get("/api/janus/approve/callback", params={"error": "login_required", "state": state}, headers=MARIO)
    assert r.status_code == 401 and "Sign in again" in r.text


def test_xolotls_no_is_shown_as_it_said_it(monkeypatch: pytest.MonkeyPatch):
    async def hand(*_args):
        raise NotApproved(409, "PR #7 moved on since the button was shown - look at it again")

    monkeypatch.setattr(app_module.approve, "hand_to_xolotl", hand)
    with client() as c:
        state = state_of(start(c))
        r = c.get("/api/janus/approve/callback", params={"code": "c", "state": state}, headers=MARIO)
    assert r.status_code == 409 and "moved on since the button was shown" in r.text


def test_text_from_upstream_is_escaped(monkeypatch: pytest.MonkeyPatch):
    async def hand(*_args):
        raise NotApproved(422, "<script>alert(1)</script>")

    monkeypatch.setattr(app_module.approve, "hand_to_xolotl", hand)
    with client() as c:
        state = state_of(start(c))
        r = c.get("/api/janus/approve/callback", params={"code": "c", "state": state}, headers=MARIO)
    assert "<script>" not in r.text and "&lt;script&gt;" in r.text
