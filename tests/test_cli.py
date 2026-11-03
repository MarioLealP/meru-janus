import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import SecretStr

from janus.cli import ClientSettings, Failed, run, secret_values
from janus.models import Submission

TOKEN_URL = "https://auth.test/application/o/token/"
URL = "https://atlas.test/api/janus"
APP = "apiVersion: meru.clockmost.com/v1\nkind: App\nmetadata:\n  name: golem\n"


class Fake:
    """Authentik's token endpoint and Janus behind Caddy."""

    def __init__(self, reject: bool = False, refuse_sign_in: bool = False, on_main: tuple[str, ...] = ()) -> None:
        self.reject, self.refuse_sign_in, self.on_main = reject, refuse_sign_in, on_main
        self.sign_ins: list[dict[str, list[str]]] = []
        self.calls: list[tuple[str, str, str | None, dict | None]] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        if url == TOKEN_URL:
            self.sign_ins.append(parse_qs(request.content.decode()))
            if self.refuse_sign_in:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(200, json={"access_token": "jwt", "token_type": "Bearer", "expires_in": 300})
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, url, request.headers.get("authorization"), body))
        if request.method == "GET" and url.endswith("/apps"):
            on_main = [
                {"path": path, "kind": "app", "name": path.removeprefix("apps/").removesuffix(".yaml"), "removed": True}
                for path in self.on_main
            ]
            return httpx2.Response(200, json=on_main)
        if request.method == "GET":
            pending = {
                "pr": 3,
                "title": "feat(request): add app golem",
                "url": "https://git.test/pulls/3",
                "author": "janus",
                "requested_by": "agent-claude",
                "head": "abc",
                "files": ["apps/golem.yaml"],
            }
            return httpx2.Response(200, json=[pending])
        if self.reject:
            return httpx2.Response(422, json={"problems": ["apps/golem.yaml: owner missing"], "checked_against": "c1"})
        answer = {"pr": 4, "url": "https://git.test/pulls/4", "title": "feat(request): add app golem"}
        return httpx2.Response(201, json=answer | {"checked_against": "c1"})


def settings(**kw: object) -> ClientSettings:
    return ClientSettings(url=URL, token_url=TOKEN_URL, client_id="cid", password=SecretStr("app-pw"), **kw)  # ty: ignore[invalid-argument-type]


def call(fake: Fake, *argv: str) -> int:
    with httpx2.Client(transport=httpx2.MockTransport(fake.handler)) as http:
        return run(list(argv), http, settings())


@pytest.fixture
def request_file(tmp_path: Path) -> Path:
    path = tmp_path / "apps" / "golem.yaml"
    path.parent.mkdir()
    path.write_text(APP)
    return path


def test_it_signs_in_once_with_the_proxy_scopes_and_sends_the_token(capsys: pytest.CaptureFixture[str]):
    fake = Fake()
    assert call(fake, "pending") == 0
    [form] = fake.sign_ins
    assert form["grant_type"] == ["client_credentials"] and form["username"] == ["agent-claude"]
    assert form["password"] == ["app-pw"] and form["client_id"] == ["cid"]
    assert "ak_proxy" in form["scope"][0].split()  # without it the outpost doesn't know who it is (403)
    [(method, url, auth, _)] = fake.calls
    assert (method, url, auth) == ("GET", f"{URL}/requests", "Bearer jwt")
    assert "#3 feat(request): add app golem  (agent-claude)" in capsys.readouterr().out


def test_submit_sends_the_file_under_its_request_path(request_file: Path, capsys: pytest.CaptureFixture[str]):
    fake = Fake()
    assert call(fake, "submit", str(request_file)) == 0
    [(method, _, _, body)] = fake.calls
    assert method == "POST" and body == {"path": "apps/golem.yaml", "content": APP}
    assert "https://git.test/pulls/4 - waiting for approval" in capsys.readouterr().out


def test_a_rejected_file_prints_shikigamis_problems_and_fails(request_file: Path, capsys: pytest.CaptureFixture[str]):
    assert call(Fake(reject=True), "submit", str(request_file)) == 1
    out = capsys.readouterr().out
    assert "rejected" in out and "- apps/golem.yaml: owner missing" in out


def test_json_output_is_one_model_per_line(request_file: Path, capsys: pytest.CaptureFixture[str]):
    call(Fake(), "--json", "submit", str(request_file))
    assert json.loads(capsys.readouterr().out)["pr"] == 4


def test_a_file_outside_apps_or_databases_is_refused_before_signing_in(tmp_path: Path):
    stray = tmp_path / "deploy" / "values.yaml"
    stray.parent.mkdir()
    stray.write_text("x: 1\n")
    fake = Fake()
    with pytest.raises(Failed, match="not a request file"):
        call(fake, "submit", str(stray))
    assert fake.sign_ins == [] and fake.calls == []


def test_a_refused_sign_in_says_so():
    with pytest.raises(Failed, match="Authentik refused the sign-in as agent-claude: 400"):
        call(Fake(refuse_sign_in=True), "pending")


TANUKI = (
    "kind: App\nmetadata:\n  name: tanuki\nspec:\n  owner: mario\n  secrets:\n"
    "    - name: discord-token\n    - name: session-key\n      generate: true\n"
)
ITEM = Submission(path="apps/tanuki.yaml", content=TANUKI)


def test_values_come_from_the_environment_first_then_a_hidden_prompt():
    from_env = secret_values(ITEM, ask=pytest.fail, environ={"JANUS_SECRET_DISCORD_TOKEN": "env-value"})
    assert {k: v.get_secret_value() for k, v in from_env.items()} == {"discord-token": "env-value"}
    asked: list[str] = []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return "typed"

    typed = secret_values(ITEM, ask=ask, environ={}, interactive=lambda: True)
    assert {k: v.get_secret_value() for k, v in typed.items()} == {"discord-token": "typed"}
    assert asked == ["apps/tanuki.yaml discord-token (hidden): "]  # the generated one isn't asked for


def test_without_a_terminal_a_missing_value_fails_before_anything_is_sent():
    with pytest.raises(Failed, match="JANUS_SECRET_DISCORD_TOKEN"):
        secret_values(ITEM, environ={}, interactive=lambda: False)


@pytest.fixture
def tanuki_file(tmp_path: Path) -> Path:
    path = tmp_path / "apps" / "tanuki.yaml"
    path.parent.mkdir()
    path.write_text(TANUKI)
    return path


def test_a_file_already_on_main_keeps_its_apps_secrets(tanuki_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("JANUS_SECRET_DISCORD_TOKEN", raising=False)  # none needed: a restore (D106) asks for nothing
    fake = Fake(on_main=("apps/tanuki.yaml",))
    assert call(fake, "submit", str(tanuki_file)) == 0
    [(_, apps, _, _), (_, _, _, body)] = fake.calls
    assert apps == f"{URL}/apps"
    assert body == {"path": "apps/tanuki.yaml", "content": TANUKI}  # no "secrets"


def test_a_new_files_secret_values_are_sent(tanuki_file: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JANUS_SECRET_DISCORD_TOKEN", "env-value")
    fake = Fake(on_main=("apps/golem.yaml",))
    assert call(fake, "submit", str(tanuki_file)) == 0
    [_, (_, _, _, body)] = fake.calls
    assert body is not None and body["secrets"] == {"discord-token": "env-value"}


def test_main_is_only_asked_when_a_file_names_secrets(request_file: Path):
    fake = Fake()
    assert call(fake, "submit", str(request_file)) == 0
    assert [url for _, url, _, _ in fake.calls] == [f"{URL}/requests"]
