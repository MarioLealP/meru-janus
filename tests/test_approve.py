from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from janus import approve, gitea, shikigami
from janus.app import create_app
from janus.approve import COOKIE, Click, NotApprovedError, Outcome, Result, back_url, nonce
from janus.config import Settings
from janus.gitea import NotOnMainError
from janus.models import Submission, Verdict
from janus.propose import Proposer

HEAD = "a" * 40
MARIO = {"X-Authentik-Username": "akadmin", "X-Authentik-Groups": "clockmost-admins|meru-approvers"}
FROM_ATLAS = MARIO | {"Sec-Fetch-Site": "same-origin"}
FORM = {"head": HEAD, "decision": "approve"}


# https: the approval cookie is Secure, so the test browser only sends it back over https
@asynccontextmanager
async def client(token: str | None = "tok") -> AsyncGenerator[httpx2.AsyncClient]:
    app = create_app(Settings(gitea_token=token))
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="https://atlas.clockmost.com") as c,
    ):
        yield c


async def start(c: httpx2.AsyncClient, headers: dict[str, str] = FROM_ATLAS, form: dict[str, str] = FORM):
    return await c.post("/api/janus/approve/7/start", data=form, headers=headers, follow_redirects=False)


async def test_start_sends_the_browser_to_authentik_silently_with_this_clicks_fingerprint():
    async with client() as c:
        r = await start(c)
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://auth.clockmost.com/application/o/authorize/"
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["prompt"] == "none"
    assert q["client_id"] == "xolotl-approvals"
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "https://atlas.clockmost.com/api/janus/approve/callback"
    assert q["nonce"] == nonce("Meru", "requests", 7, HEAD, "approve")
    cookie = r.headers["set-cookie"]
    assert f"{COOKIE}=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
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
async def test_start_only_from_the_atlas_page_and_only_for_approvers(headers: dict[str, str], why: str):
    async with client() as c:
        r = await start(c, headers=headers)
    assert landed(r) == {"v": "requests", "res": why, "pr": "7"}
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize("form", [{"head": "nope", "decision": "approve"}, {"head": HEAD, "decision": "merge"}, {}])
async def test_a_button_without_a_commit_and_decision_starts_nothing(form: dict[str, str]):
    async with client() as c:
        assert landed(await start(c, form=form))["res"] == "bad-button"


def landed(r) -> dict[str, str]:
    """Where the browser was sent back to: the atlas's Requests view, with the result in the hash."""
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://atlas.clockmost.com/"
    assert not url.query
    return {k: v[0] for k, v in parse_qs(url.fragment).items()}


def state_of(r) -> str:
    return parse_qs(urlsplit(r.headers["location"]).query)["state"][0]


async def test_the_callback_hands_the_code_to_xolotl_with_what_was_clicked(monkeypatch: pytest.MonkeyPatch):
    seen = []

    async def hand(_http, url: str, click: Click, code: str) -> Outcome:
        seen.append((url, click.pr, click.head, click.decision, code))
        return Outcome(pr=7, decision="approve", by="akadmin", commit="c0ffee")

    monkeypatch.setattr(approve, "hand_to_xolotl", hand)
    async with client() as c:
        state = state_of(await start(c))
        r = await c.get(
            "/api/janus/approve/callback",
            params={"code": "one-time", "state": state},
            headers=MARIO,
            follow_redirects=False,
        )
    assert landed(r) == {"v": "requests", "res": "done", "pr": "7", "dec": "approve", "by": "akadmin", "c": "c0ffee"}
    assert seen == [("http://xolotl.xolotl.svc/approve", 7, HEAD, "approve", "one-time")]
    assert f'{COOKIE}=""' in r.headers["set-cookie"]  # used once: the cookie goes


async def test_a_code_from_an_approval_not_started_here_goes_nowhere(monkeypatch: pytest.MonkeyPatch):
    async def hand(*_args):
        raise AssertionError("must not reach Xolotl")

    monkeypatch.setattr(approve, "hand_to_xolotl", hand)
    async with client() as c:
        # a page sent Mario's browser straight to Authentik: a code and a state, but no cookie from a start
        r = await c.get(
            "/api/janus/approve/callback",
            params={"code": "c", "state": "x" * 43},
            headers=MARIO,
            follow_redirects=False,
        )
        assert landed(r) == {"v": "requests", "res": "not-yours"}
        await start(c)  # a real start, then an answer with someone else's state
        r = await c.get(
            "/api/janus/approve/callback",
            params={"code": "c", "state": "y" * 43},
            headers=MARIO,
            follow_redirects=False,
        )
        assert landed(r)["res"] == "not-yours"


async def test_signed_out_at_authentik_says_sign_in_again():
    async with client() as c:
        state = state_of(await start(c))
        r = await c.get(
            "/api/janus/approve/callback",
            params={"error": "login_required", "state": state},
            headers=MARIO,
            follow_redirects=False,
        )
    assert landed(r) == {"v": "requests", "res": "sign-in", "pr": "7", "dec": "approve"}


@pytest.mark.parametrize(("status", "result"), [(403, "refused"), (409, "changed"), (422, "problems"), (502, "down")])
async def test_xolotls_no_comes_back_as_a_fixed_code_never_its_text(
    monkeypatch: pytest.MonkeyPatch, status: int, result: str
):
    async def hand(*_args):
        raise NotApprovedError(status, "<script>alert(1)</script> PR #7 moved on")

    monkeypatch.setattr(approve, "hand_to_xolotl", hand)
    async with client() as c:
        state = state_of(await start(c))
        r = await c.get(
            "/api/janus/approve/callback", params={"code": "c", "state": state}, headers=MARIO, follow_redirects=False
        )
    assert landed(r) == {"v": "requests", "res": result, "pr": "7", "dec": "approve"}
    assert "script" not in r.headers["location"]


def test_values_in_the_hash_are_escaped():
    url = back_url("https://a.test", Result.DONE, 7, "approve", "a&r=evil", "c0ffee")
    assert url == "https://a.test/#v=requests&res=done&pr=7&dec=approve&by=a%26r%3Devil&c=c0ffee"


KITSUNE = "metadata:\n  name: kitsune\nspec:\n  owner: mario\n"


def removal(monkeypatch: pytest.MonkeyPatch, text: str | None = KITSUNE, problems: list[str] | None = None) -> list:
    proposed = []

    async def read_main(_http, _url, _org, _repo, _token, path: str) -> str:
        if text is None:
            raise NotOnMainError(path)
        return text

    async def validate(_http, _url, _submission: Submission) -> Verdict:
        return Verdict(commit="main0", problems=problems or [])

    async def propose(_self, submission: Submission, who: str, checked: str) -> tuple[str, str, str]:
        proposed.append((submission.path, submission.content, who))
        return "feat(request): remove app kitsune", "remote:   https://git.test/Meru/requests/pulls/13\n", HEAD

    monkeypatch.setattr(gitea, "read_main", read_main)
    monkeypatch.setattr(shikigami, "validate", validate)
    monkeypatch.setattr(Proposer, "propose", propose)
    return proposed


async def remove(c: httpx2.AsyncClient, headers: dict[str, str] = FROM_ATLAS, path: str = "apps/kitsune.yaml"):
    return await c.post("/api/janus/approve/remove", data={"path": path}, headers=headers, follow_redirects=False)


async def test_remove_opens_the_removal_pr_in_marios_name_and_approves_it_in_the_same_click(
    monkeypatch: pytest.MonkeyPatch,
):
    proposed = removal(monkeypatch)
    seen = []

    async def hand(_http, _url, click: Click, code: str) -> Outcome:
        seen.append((click.pr, click.head, click.decision))
        return Outcome(pr=13, decision="approve", by="akadmin", commit="c0ffee")

    monkeypatch.setattr(approve, "hand_to_xolotl", hand)
    async with client() as c:
        r = await remove(c)
        assert proposed == [("apps/kitsune.yaml", KITSUNE + "  removed: true\n", "akadmin")]
        q = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
        assert q["prompt"] == "none"
        assert q["nonce"] == nonce("Meru", "requests", 13, HEAD, "approve")
        r = await c.get(
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
async def test_a_remove_that_cant_go_ahead_opens_no_pr(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], text: str | None, problems: list[str] | None, result: str
):
    proposed = removal(monkeypatch, text, problems)
    async with client() as c:
        r = await remove(c, headers=headers)
    assert landed(r)["res"] == result
    assert "set-cookie" not in r.headers
    if result != "problems":
        assert proposed == []


async def test_without_the_bot_a_remove_is_refused_first_then_a_503():
    async with client(token=None) as c:
        assert landed(await remove(c, headers=MARIO | {"Sec-Fetch-Site": "cross-site"}))["res"] == "not-atlas"
        assert landed(await remove(c, path="../secrets.yaml"))["res"] == "bad-button"
        assert (await remove(c)).status_code == 503


async def test_a_pr_that_isnt_a_number_is_a_422_before_any_click_check():
    async with client() as c:
        r = await c.post("/api/janus/approve/abc/start", data=FORM, headers=MARIO | {"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 422


async def test_remove_only_takes_a_request_path(monkeypatch: pytest.MonkeyPatch):
    proposed = removal(monkeypatch)
    async with client() as c:
        assert landed(await remove(c, path="../secrets.yaml"))["res"] == "bad-button"
    assert proposed == []
