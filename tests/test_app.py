from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx2
import pytest

from janus import gitea, shikigami
from janus.app import create_app
from janus.config import Settings
from janus.models import Pending, Submission, Verdict
from janus.propose import Proposer

ID = {"X-Authentik-Username": "agent-claude"}
GOOD = {"path": "apps/golem.yaml", "content": "kind: App\n"}


@asynccontextmanager
async def client(token: str | None = "tok") -> AsyncGenerator[httpx2.AsyncClient]:
    app = create_app(Settings(gitea_token=token))
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://testserver") as c,
    ):
        yield c


async def test_heartbeat_needs_no_identity():
    async with client() as c:
        assert (await c.get("/heartbeat")).text == "ok"


async def test_everything_else_needs_an_sso_identity():
    async with client() as c:
        assert (await c.get("/api/janus/requests")).status_code == 403
        assert (await c.post("/api/janus/requests", json=GOOD)).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/janus/requests"),
        ("POST", "/api/janus/requests"),
        ("GET", "/api/janus/apps"),
        ("POST", "/api/janus/approve/remove"),
        ("POST", "/api/janus/approve/7/start"),
        ("GET", "/api/janus/approve/callback"),
        ("GET", "/no/such/route"),
    ],
)
async def test_no_route_is_reached_without_an_identity(method: str, path: str):
    async with client() as c:
        r = await c.request(method, path)
    assert r.status_code == 403
    assert r.text == "forbidden: no SSO identity"


async def test_a_bad_body_is_a_422_even_without_the_bot():
    async with client(token=None) as c:
        r = await c.post("/api/janus/requests", json={"path": "README.md", "content": ""}, headers=ID)
    assert r.status_code == 422


async def test_without_the_bot_nothing_is_listed_or_proposed():
    async with client(token=None) as c:
        assert (await c.get("/api/janus/requests", headers=ID)).status_code == 503
        assert (await c.post("/api/janus/requests", json=GOOD, headers=ID)).status_code == 503


@pytest.mark.parametrize("path", ["README.md", "apps/../x.yaml", "apps/X.yaml", ".gitea/workflows/ci.yaml"])
async def test_only_request_files_are_taken(path: str):
    async with client() as c:
        assert (await c.post("/api/janus/requests", json={"path": path, "content": ""}, headers=ID)).status_code == 422


async def test_a_file_shikigami_rejects_is_not_proposed(monkeypatch: pytest.MonkeyPatch):
    async def validate(_http, _url, _submission: Submission) -> Verdict:
        return Verdict(commit="c0ffee", problems=["apps/golem.yaml: spec.owner: Field required"])

    async def propose(*_args):
        raise AssertionError("must not propose")

    monkeypatch.setattr(shikigami, "validate", validate)
    monkeypatch.setattr(Proposer, "propose", propose)
    async with client() as c:
        r = await c.post("/api/janus/requests", json=GOOD, headers=ID)
    assert r.status_code == 422
    assert r.json() == {"problems": ["apps/golem.yaml: spec.owner: Field required"], "checked_against": "c0ffee"}


async def test_a_valid_file_is_proposed_in_the_callers_name(monkeypatch: pytest.MonkeyPatch):
    calls = []

    async def validate(_http, _url, _submission: Submission) -> Verdict:
        return Verdict(commit="c0ffee", problems=[])

    async def propose(_self, submission: Submission, who: str, checked: str) -> tuple[str, str, str]:
        calls.append((submission.path, who, checked))
        return "feat(request): add app golem", "remote:   https://git.test/Meru/requests/pulls/12\n", "h"

    monkeypatch.setattr(shikigami, "validate", validate)
    monkeypatch.setattr(Proposer, "propose", propose)
    async with client() as c:
        r = await c.post("/api/janus/requests", json=GOOD, headers=ID)
    assert r.status_code == 201
    assert r.json() == {
        "pr": 12,
        "url": "https://git.test/Meru/requests/pulls/12",
        "title": "feat(request): add app golem",
        "checked_against": "c0ffee",
    }
    assert calls == [("apps/golem.yaml", "agent-claude", "c0ffee")]


async def test_shikigami_down_is_a_502(monkeypatch: pytest.MonkeyPatch):
    async def validate(*_args):
        raise httpx2.ConnectError("down")

    monkeypatch.setattr(shikigami, "validate", validate)
    async with client() as c:
        assert (await c.post("/api/janus/requests", json=GOOD, headers=ID)).status_code == 502


async def test_pending_lists_the_open_prs(monkeypatch: pytest.MonkeyPatch):
    one = Pending(
        pr=12, title="feat(request): add app golem", url="u", author="janus", requested_by="agent-claude",
        head="abc", files=["apps/golem.yaml"],
    )  # fmt: skip

    async def pending(*_args) -> list[Pending]:
        return [one]

    monkeypatch.setattr(gitea, "pending", pending)
    async with client() as c:
        assert (await c.get("/api/janus/requests", headers=ID)).json() == [one.model_dump()]


TANUKI = (
    "kind: App\nmetadata:\n  name: tanuki\nspec:\n  owner: mario\n  secrets:\n"
    "    - name: discord-token\n    - name: session-key\n      generate: true\n"
)


def secrets_flow(monkeypatch: pytest.MonkeyPatch, on_main: bool = False, refuse: str | None = None) -> list[str]:
    """Shikigami says fine; records the order of OpenBao writes and PRs."""
    from janus.gitea import NotOnMainError
    from janus.request_secrets import NotWrittenError, Vault

    order: list[str] = []

    async def validate(*_args) -> Verdict:
        return Verdict(commit="c0ffee", problems=[])

    async def read_main(_http, _url, _org, _repo, _token, path: str) -> str:
        if not on_main:
            raise NotOnMainError(path)
        return TANUKI

    async def create_env(_self, app: str, values: dict[str, str]) -> None:
        if refuse:
            raise NotWrittenError(refuse)
        order.append(f"openbao {app} {sorted(values)} typed={values['discord-token']}")

    async def propose(_self, submission: Submission, who: str, checked: str) -> tuple[str, str, str]:
        assert "typed-value" not in submission.content  # the values never reach the PR
        order.append(f"pr {submission.path}")
        return "feat(request): add app tanuki", "remote:   https://git.test/Meru/requests/pulls/14\n", "h"

    monkeypatch.setattr(shikigami, "validate", validate)
    monkeypatch.setattr(gitea, "read_main", read_main)
    monkeypatch.setattr(Vault, "create_env", create_env)
    monkeypatch.setattr(Vault, "ready", property(lambda _self: True))
    monkeypatch.setattr(Proposer, "propose", propose)
    return order


async def submit_tanuki(c: httpx2.AsyncClient, secrets: dict[str, str] | None = None):
    body = {"path": "apps/tanuki.yaml", "content": TANUKI, "secrets": secrets or {"discord-token": "typed-value"}}
    return await c.post("/api/janus/requests", json=body, headers=ID)


async def test_a_new_apps_values_go_to_openbao_before_its_pr_opens(monkeypatch: pytest.MonkeyPatch):
    order = secrets_flow(monkeypatch)
    async with client() as c:
        assert (await submit_tanuki(c)).status_code == 201
    assert order == ["openbao tanuki ['discord-token', 'session-key'] typed=typed-value", "pr apps/tanuki.yaml"]


@pytest.mark.parametrize(
    ("on_main", "refuse", "secrets", "why"),
    [
        (False, "tanuki already has secret values in OpenBao", None, "already has secret values"),
        (False, None, {"session-key": "x"}, "no value for discord-token"),
        (True, None, None, "already on main"),
    ],
)
async def test_no_pr_when_the_values_cant_be_written(
    monkeypatch: pytest.MonkeyPatch, on_main: bool, refuse: str | None, secrets: dict | None, why: str
):
    order = secrets_flow(monkeypatch, on_main=on_main, refuse=refuse)
    async with client() as c:
        r = await submit_tanuki(c, secrets)
    assert r.status_code == 422
    assert why in r.json()["problems"][0]
    assert order == []
    assert "typed-value" not in r.text


async def test_values_for_a_request_without_secrets_are_refused(monkeypatch: pytest.MonkeyPatch):
    order = secrets_flow(monkeypatch)
    async with client() as c:
        body = {**GOOD, "secrets": {"x": "y"}}
        r = await c.post("/api/janus/requests", json=body, headers=ID)
    assert r.status_code == 422
    assert order == []


@pytest.mark.parametrize(
    ("on_main", "status", "why"),
    [
        (True, 201, None),  # a restore (D106): the app keeps the values it has in OpenBao
        (False, 422, "no value for discord-token"),  # a new app has nothing to keep
    ],
)
async def test_no_values_keep_an_existing_apps_secrets_and_refuse_a_new_one(
    monkeypatch: pytest.MonkeyPatch, on_main: bool, status: int, why: str | None
):
    order = secrets_flow(monkeypatch, on_main=on_main)
    async with client() as c:
        r = await c.post("/api/janus/requests", json={"path": "apps/tanuki.yaml", "content": TANUKI}, headers=ID)
    assert r.status_code == status
    if why:
        assert why in r.json()["problems"][0]
        assert order == []
    else:
        assert order == ["pr apps/tanuki.yaml"]  # nothing written to OpenBao
