import json
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from janus.request_secrets import NotWritten, Requested, Vault, requested, values_for

TANUKI = """apiVersion: meru.clockmost.com/v1
kind: App
metadata:
  name: tanuki
spec:
  owner: mario
  description: Test app
  secrets:
    - name: discord-token
    - name: session-key
      generate: true
"""
WANTED = [Requested(name="discord-token"), Requested(name="session-key", generate=True)]


def test_the_requests_secrets_are_read_from_an_app_request():
    assert requested("apps/tanuki.yaml", TANUKI) == WANTED
    assert requested("databases/tanuki.yaml", TANUKI) == []  # databases get their logins from CNPG
    assert requested("apps/tanuki.yaml", "kind: App\nspec: {}\n") == []


def test_typed_values_are_kept_and_the_rest_generated():
    typed = {"discord-token": SecretStr("typed")}
    first, second = values_for(WANTED, typed), values_for(WANTED, typed)
    assert first["discord-token"] == "typed"
    assert len(first["session-key"]) >= 43 and first["session-key"] != second["session-key"]  # token_urlsafe(32)


@pytest.mark.parametrize(
    ("given", "why"),
    [
        ({}, "no value for discord-token"),
        ({"discord-token": SecretStr("x"), "session-key": SecretStr("y")}, "doesn't ask you to type: session-key"),
        ({"discord-token": SecretStr("")}, "empty value for discord-token"),
    ],
)
def test_exactly_the_names_to_type_must_come_with_values(given: dict[str, SecretStr], why: str):
    with pytest.raises(NotWritten, match=why):
        values_for(WANTED, given)


class FakeBao:
    def __init__(self, write_status: int = 200) -> None:
        self.write_status = write_status
        self.logins: list[dict] = []
        self.writes: list[tuple[str, str, dict]] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/auth/kubernetes/login":
            self.logins.append(body)
            return httpx2.Response(200, json={"auth": {"client_token": "s.janus", "lease_duration": 600}})
        self.writes.append((request.url.path, request.headers["x-vault-token"], body))
        return httpx2.Response(self.write_status, json={})


def vault(tmp_path: Path, bao: FakeBao) -> Vault:
    (tmp_path / "token").write_text("pod-jwt\n")
    (tmp_path / "ca.crt").write_text("ca")
    return Vault("https://bao.test", "meru-janus-secrets", tmp_path, 5, transport=httpx2.MockTransport(bao.handler))


async def test_values_are_created_in_the_apps_env_entry_only_if_it_never_existed(tmp_path: Path):
    bao = FakeBao()
    await vault(tmp_path, bao).create_env("tanuki", {"discord-token": "typed"})
    assert bao.logins == [{"role": "meru-janus-secrets", "jwt": "pod-jwt"}]
    [(path, token, body)] = bao.writes
    assert (path, token) == ("/v1/apps/data/meru/tanuki/eso/env", "s.janus")
    assert body == {"options": {"cas": 0}, "data": {"discord-token": "typed"}}


@pytest.mark.parametrize("status", [400, 403])  # the check-and-set, or the create-only policy
async def test_an_existing_entry_is_refused_and_said_why(tmp_path: Path, status: int):
    with pytest.raises(NotWritten, match="already has secret values"):
        await vault(tmp_path, FakeBao(write_status=status)).create_env("kitsune", {"a": "b"})


def test_without_the_mounted_login_janus_isnt_ready(tmp_path: Path):
    assert not Vault("https://bao.test", "r", tmp_path, 5).ready
