from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from janus import app as app_module
from janus.app import create_app
from janus.approve import COOKIE, Click, NotApproved, Outcome, back_url, nonce
from janus.config import Settings
from janus.gitea import NotOnMain
from janus.models import Submission, Verdict

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
        (MARIO | {"Sec-Fetch-Site": "cross-site"}, "not-atlas"),
        (MARIO | {"Sec-Fetch-Site": "same-site"}, "not-atlas"),  # another clockmost.com app's page
        (MARIO | {"Origin": "https://evil.example"}, "not-atlas"),
        (FROM_ATLAS | {"X-Authentik-Groups": "clockmost-admins"}, "not-approver"),
    ],
)
def test_start_only_from_the_atlas_page_and_only_for_approvers(headers: dict[str, str], why: str):
    with client() as c:
        r = start(c, headers=headers)
    assert landed(r) == {"v": "requests", "res": why, "pr": "7"} and "set-cookie" not in r.headers


@pytest.mark.parametrize("form", [{"head": "nope", "decision": "approve"}, {"head": HEAD, "decision": "merge"}, {}])
def test_a_button_without_a_commit_and_decision_starts_nothing(form: dict[str, str]):
    with client() as c:
        assert landed(start(c, form=form))["res"] == "bad-button"


def landed(r) -> dict[str, str]:
    """Where the browser was sent back to: the atlas's Requests view, with the result in the hash."""
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://atlas.clockmost.com/" and not url.query
    return {k: v[0] for k, v in parse_qs(url.fragment).items()}


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
        r = c.get(
            "/api/janus/approve/callback",
            params={"code": "one-time", "state": state},
            headers=MARIO,
            follow_redirects=False,
        )
    assert landed(r) == {"v": "requests", "res": "done", "pr": "7", "dec": "approve", "by": "akadmin", "c": "c0ffee"}
    assert seen == [("http://xolotl.xolotl.svc/approve", 7, HEAD, "approve", "one-time")]
    assert f'{COOKIE}=""' in r.headers["set-cookie"]  # used once: the cookie goes


def test_a_code_from_an_approval_not_started_here_goes_nowhere(monkeypatch: pytest.MonkeyPatch):
    async def hand(*_args):
        raise AssertionError("must not reach Xolotl")

    monkeypatch.setattr(app_module.approve, "hand_to_xolotl", hand)
    with client() as c:
        # a page sent Mario's browser straight to Authentik: a code and a state, but no cookie from a start
        r = c.get(
            "/api/janus/approve/callback",
            params={"code": "c", "state": "x" * 43},
            headers=MARIO,
            follow_redirects=False,
        )
        assert landed(r) == {"v": "requests", "res": "not-yours"}
        start(c)  # a real start, then an answer with someone else's state
        r = c.get(
            "/api/janus/approve/callback",
            params={"code": "c", "state": "y" * 43},
            headers=MARIO,
            follow_redirects=False,
        )
        assert landed(r)["res"] == "not-yours"


def test_signed_out_at_authentik_says_sign_in_again():
    with client() as c:
        state = state_of(start(c))
        r = c.get(
            "/api/janus/approve/callback",
            params={"error": "login_required", "state": state},
            headers=MARIO,
            follow_redirects=False,
        )
    assert landed(r) == {"v": "requests", "res": "sign-in", "pr": "7", "dec": "approve"}


@pytest.mark.parametrize(("status", "result"), [(403, "refused"), (409, "changed"), (422, "problems"), (502, "down")])
def test_xolotls_no_comes_back_as_a_fixed_code_never_its_text(
    monkeypatch: pytest.MonkeyPatch, status: int, result: str
):
    async def hand(*_args):
        raise NotApproved(status, "<script>alert(1)</script> PR #7 moved on")

    monkeypatch.setattr(app_module.approve, "hand_to_xolotl", hand)
    with client() as c:
        state = state_of(start(c))
        r = c.get(
            "/api/janus/approve/callback", params={"code": "c", "state": state}, headers=MARIO, follow_redirects=False
        )
    assert landed(r) == {"v": "requests", "res": result, "pr": "7", "dec": "approve"}
    assert "script" not in r.headers["location"]


def test_values_in_the_hash_are_escaped():
    url = back_url("https://a.test", app_module.approve.Result.DONE, 7, "approve", "a&r=evil", "c0ffee")
    assert url == "https://a.test/#v=requests&res=done&pr=7&dec=approve&by=a%26r%3Devil&c=c0ffee"


KITSUNE = "metadata:\n  name: kitsune\nspec:\n  owner: mario\n"


def removal(monkeypatch: pytest.MonkeyPatch, text: str | None = KITSUNE, problems: list[str] | None = None) -> list:
    proposed = []

    async def read_main(_http, _url, _org, _repo, _token, path: str) -> str:
        if text is None:
            raise NotOnMain(path)
        return text

    async def validate(_http, _url, _submission: Submission) -> Verdict:
        return Verdict(commit="main0", problems=problems or [])

    async def propose(_self, submission: Submission, who: str, checked: str) -> tuple[str, str, str]:
        proposed.append((submission.path, submission.content, who))
        return "feat(request): remove app kitsune", "remote:   https://git.test/Meru/requests/pulls/13\n", HEAD

    monkeypatch.setattr(app_module.gitea, "read_main", read_main)
    monkeypatch.setattr(app_module.shikigami, "validate", validate)
    monkeypatch.setattr(app_module.Proposer, "propose", propose)
    return proposed


def remove(c: TestClient, headers: dict[str, str] = FROM_ATLAS, path: str = "apps/kitsune.yaml"):
    return c.post("/api/janus/approve/remove", data={"path": path}, headers=headers, follow_redirects=False)


def test_remove_opens_the_removal_pr_in_marios_name_and_approves_it_in_the_same_click(monkeypatch: pytest.MonkeyPatch):
    proposed = removal(monkeypatch)
    seen = []

    async def hand(_http, _url, click: Click, code: str) -> Outcome:
        seen.append((click.pr, click.head, click.decision))
        return Outcome(pr=13, decision="approve", by="akadmin", commit="c0ffee")

    monkeypatch.setattr(app_module.approve, "hand_to_xolotl", hand)
    with client() as c:
        r = remove(c)
        assert proposed == [("apps/kitsune.yaml", KITSUNE + "  removed: true\n", "akadmin")]
        q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
        assert q["prompt"] == "none" and q["nonce"] == nonce("Meru", "requests", 13, HEAD, "approve")
        r = c.get(
            "/api/janus/approve/callback",
            params={"code": "c", "state": q["state"]},
            headers=MARIO,
            follow_redirects=False,
        )
    assert seen == [(13, HEAD, "approve")]
    assert landed(r) == {"v": "requests", "res": "done", "pr": "13", "dec": "remove", "by": "akadmin", "c": "c0ffee"}


@pytest.mark.parametrize(
    ("headers", "text", "problems", "result"),
    [
        (MARIO | {"Sec-Fetch-Site": "cross-site"}, KITSUNE, None, "not-atlas"),
        (FROM_ATLAS | {"X-Authentik-Groups": "clockmost-admins"}, KITSUNE, None, "not-approver"),
        (FROM_ATLAS, None, None, "not-on-main"),
        (FROM_ATLAS, KITSUNE + "  removed: true\n", None, "already-removed"),
        (FROM_ATLAS, KITSUNE, ["apps/kitsune.yaml: something"], "problems"),
    ],
)
def test_a_remove_that_cant_go_ahead_opens_no_pr(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], text: str | None, problems: list[str] | None, result: str
):
    proposed = removal(monkeypatch, text, problems)
    with client() as c:
        r = remove(c, headers=headers)
    assert landed(r)["res"] == result and "set-cookie" not in r.headers
    if result != "problems":
        assert proposed == []


def test_remove_only_takes_a_request_path(monkeypatch: pytest.MonkeyPatch):
    proposed = removal(monkeypatch)
    with client() as c:
        assert landed(remove(c, path="../secrets.yaml"))["res"] == "bad-button"
    assert proposed == []
