"""The atlas's buttons now lead to the Gitea PR page (Mario merges there); nothing calls Xolotl or Authentik."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
import pytest

from janus.api.app import create_app
from janus.clients import gitea, shikigami
from janus.clients.gitea import NotOnMainError
from janus.config import Settings
from janus.core.propose import Proposer
from janus.models.requests import Pending, Submission, Verdict

HEAD = "a" * 40
MARIO = {"X-Authentik-Username": "akadmin"}
FROM_ATLAS = MARIO | {"Sec-Fetch-Site": "same-origin"}
PAGE = "https://git.clockmost.com/Meru/requests/pulls/{}"
KITSUNE = "metadata:\n  name: kitsune\nspec:\n  owner: mario\n"


def refuse_outside(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"unexpected outbound call: {request.method} {request.url}")


@asynccontextmanager
async def client(
    token: str | None = "tok", platform: frozenset[str] | None = None
) -> AsyncGenerator[httpx2.AsyncClient]:
    settings = Settings(gitea_token=token)
    if platform is not None:
        settings = settings.model_copy(update={"platform_requests": platform})
    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="https://atlas.clockmost.com") as c,
    ):
        # Nothing here may reach Xolotl or Authentik (or anything else): any outbound call fails the test.
        app.state.backends.http._transport = httpx2.MockTransport(refuse_outside)
        yield c


@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_approve_and_deny_go_to_the_gitea_pr_page(decision: str):
    async with client() as c:
        r = await c.post(
            "/api/janus/approve/7/start", data={"head": HEAD, "decision": decision}, headers=MARIO,
            follow_redirects=False,
        )  # fmt: skip
    assert r.status_code == 303
    assert r.headers["location"] == PAGE.format(7)
    assert "set-cookie" not in r.headers


async def test_the_buttons_need_no_form_no_bot_and_no_page_check():
    # a redirect to a page that isn't a request PR changes nothing, so there is no lookup to fail
    async with client(token=None) as c:
        r = await c.post("/api/janus/approve/99999/start", headers=MARIO, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == PAGE.format(99999)


async def test_a_pr_that_isnt_a_number_is_a_422():
    async with client() as c:
        r = await c.post("/api/janus/approve/abc/start", headers=MARIO)
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/api/janus/approve/callback", "/api/janus/approve/7/deny"])
async def test_the_old_authentik_callback_and_decision_routes_are_gone(path: str):
    async with client() as c:
        assert (await c.request("GET", path, headers=MARIO)).status_code in {404, 405}


def removal(
    monkeypatch: pytest.MonkeyPatch,
    text: str | None = KITSUNE,
    problems: list[str] | None = None,
    open_files: list[str] | None = None,
) -> list:
    proposed = []

    async def read_main(_http, _url, _org, _repo, _token, path: str) -> str:
        if text is None:
            raise NotOnMainError(path)
        return text

    async def validate(_http, _url, _submission: Submission) -> Verdict:
        return Verdict(commit="main0", problems=problems or [])

    async def pending(*_args) -> list[Pending]:
        if not open_files:
            return []
        return [
            Pending(pr=5, title="t", url=PAGE.format(5), author="janus", requested_by="x", head=HEAD, files=open_files)
        ]

    async def propose(_self, submission: Submission, who: str, checked: str) -> tuple[str, str, str]:
        proposed.append((submission.path, submission.content, who))
        return "feat(request): remove app kitsune", "remote:   https://git.test/Meru/requests/pulls/13\n", HEAD

    monkeypatch.setattr(gitea, "read_main", read_main)
    monkeypatch.setattr(gitea, "pending", pending)
    monkeypatch.setattr(shikigami, "validate", validate)
    monkeypatch.setattr(Proposer, "propose", propose)
    return proposed


async def remove(c: httpx2.AsyncClient, headers: dict[str, str] = FROM_ATLAS, path: str = "apps/kitsune.yaml"):
    return await c.post("/api/janus/approve/remove", data={"path": path}, headers=headers, follow_redirects=False)


async def test_remove_opens_the_removal_pr_in_the_clickers_name_and_goes_to_its_page(monkeypatch: pytest.MonkeyPatch):
    proposed = removal(monkeypatch)
    async with client() as c:
        r = await remove(c)
    assert r.status_code == 303
    assert r.headers["location"] == PAGE.format(13)
    # only `removed: true`: no removedBy / removedAt, Shikigami starts the retention clock
    assert proposed == [("apps/kitsune.yaml", KITSUNE + "  removed: true\n", "akadmin")]


async def test_remove_also_works_for_a_caller_without_browser_headers(monkeypatch: pytest.MonkeyPatch):
    removal(monkeypatch)
    async with client() as c:
        assert (await remove(c, headers=MARIO)).status_code == 303


@pytest.mark.parametrize(
    ("text", "problems", "open_files", "status"),
    [
        (None, None, None, 404),
        (KITSUNE + "  removed: true\n", None, None, 409),
        (KITSUNE, None, ["apps/kitsune.yaml"], 409),
        (KITSUNE, ["apps/kitsune.yaml: something"], None, 422),
    ],
)
async def test_a_remove_that_cant_go_ahead_is_a_problem_and_opens_no_pr(
    monkeypatch: pytest.MonkeyPatch,
    text: str | None,
    problems: list[str] | None,
    open_files: list[str] | None,
    status: int,
):
    proposed = removal(monkeypatch, text, problems, open_files)
    async with client() as c:
        r = await remove(c)
    assert r.status_code == status
    assert r.headers["content-type"] == "application/problem+json"
    assert "location" not in r.headers
    if status != 422 or not problems:
        assert proposed == []


async def test_a_cross_site_remove_is_refused_before_anything_happens(monkeypatch: pytest.MonkeyPatch):
    proposed = removal(monkeypatch)
    async with client() as c:
        r = await remove(c, headers=MARIO | {"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        r = await remove(c, headers=MARIO | {"Origin": "https://evil.example"})
        assert r.status_code == 403
    assert proposed == []


@pytest.mark.parametrize("path", ["apps/thoth.yaml", "databases/thoth.yaml", "apps/janus.yaml", "apps/xolotl.yaml"])
async def test_meru_own_parts_cannot_be_removed(monkeypatch: pytest.MonkeyPatch, path: str):
    proposed = removal(monkeypatch)
    async with client() as c:
        r = await remove(c, path=path)
    assert r.status_code == 422
    assert proposed == []


async def test_the_platform_list_is_a_setting(monkeypatch: pytest.MonkeyPatch):
    proposed = removal(monkeypatch)
    async with client(platform=frozenset({"kitsune"})) as c:
        assert (await remove(c)).status_code == 422
    assert proposed == []


async def test_without_the_bot_a_remove_is_a_503_after_the_cheap_refusals():
    async with client(token=None) as c:
        assert (await remove(c, path="../secrets.yaml")).status_code == 422
        assert (await remove(c)).status_code == 503


async def test_remove_only_takes_a_request_path(monkeypatch: pytest.MonkeyPatch):
    proposed = removal(monkeypatch)
    async with client() as c:
        assert (await remove(c, path="../secrets.yaml")).status_code == 422
    assert proposed == []
