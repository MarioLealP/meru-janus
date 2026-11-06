import json
import logging
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from janus.core.request_secrets import NotWrittenError, Requested, Vault, requested, values_for

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
    assert all(isinstance(v, SecretStr) for v in first.values())  # unwrapped only at the OpenBao write
    assert first["discord-token"].get_secret_value() == "typed"
    assert len(first["session-key"].get_secret_value()) >= 43  # token_urlsafe(32)
    assert first["session-key"] != second["session-key"]


@pytest.mark.parametrize(
    ("given", "why"),
    [
        ({}, "no value for discord-token"),
        ({"discord-token": SecretStr("x"), "session-key": SecretStr("y")}, "doesn't ask you to type: session-key"),
        ({"discord-token": SecretStr("")}, "empty value for discord-token"),
    ],
)
def test_exactly_the_names_to_type_must_come_with_values(given: dict[str, SecretStr], why: str):
    with pytest.raises(NotWrittenError, match=why):
        values_for(WANTED, given)


class FakeBao(BaseModel):
    model_config = ConfigDict(frozen=True)

    write_status: int = 200
    logins: list[dict] = Field(default_factory=list)
    writes: list[tuple[str, str, dict]] = Field(default_factory=list)

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
    return Vault(
        addr="https://bao.test",
        role="meru-janus-secrets",
        login_dir=tmp_path,
        timeout=5,
        transport=httpx2.MockTransport(bao.handler),
    )


async def test_values_are_created_in_the_apps_env_entry_only_if_it_never_existed(tmp_path: Path):
    bao = FakeBao()
    await vault(tmp_path, bao).create_env("tanuki", {"discord-token": SecretStr("typed")})
    assert bao.logins == [{"role": "meru-janus-secrets", "jwt": "pod-jwt"}]
    [(path, token, body)] = bao.writes
    assert (path, token) == ("/v1/apps/data/meru/tanuki/eso/env", "s.janus")
    assert body == {"options": {"cas": 0}, "data": {"discord-token": "typed"}}


@pytest.mark.parametrize("status", [400, 403])  # the check-and-set, or the create-only policy
async def test_an_existing_entry_is_refused_and_said_why(tmp_path: Path, status: int):
    with pytest.raises(NotWrittenError, match="already has secret values"):
        await vault(tmp_path, FakeBao(write_status=status)).create_env("kitsune", {"a": SecretStr("b")})


def test_without_the_mounted_login_janus_isnt_ready(tmp_path: Path):
    assert not Vault(addr="https://bao.test", role="r", login_dir=tmp_path, timeout=5).ready


def test_a_change_entry_is_the_day_and_six_hex():
    from datetime import date

    from janus.core.request_secrets import new_entry

    one, two = new_entry(date(2026, 11, 5)), new_entry(date(2026, 11, 5))
    assert one.startswith("env-20261105-")
    assert len(one) == len("env-20261105-a1b2c3")
    assert one != two


@pytest.mark.parametrize("line", ["    entry: new", "    entry: 'new'", '    entry: "new"  ', "  - entry: new"])
def test_entry_new_is_replaced_in_place(line: str):
    from janus.core.request_secrets import with_entry

    filled = with_entry(f"spec:\n  secrets:\n    - name: a\n{line}\n    - name: b\n", "env-20261105-a1b2c3")
    assert "new" not in filled.replace("name", "")
    assert "entry: env-20261105-a1b2c3\n" in filled


def test_a_file_without_entry_new_is_not_a_change():
    from janus.core.request_secrets import with_entry

    with pytest.raises(NotWrittenError):
        with_entry("spec:\n  secrets:\n    - name: a\n      entry: env\n    - name: renew\n", "env-20261105-a1b2c3")


@pytest.mark.parametrize("status", [200, 403, 500])
async def test_the_real_write_puts_a_change_in_its_own_entry_and_logs_no_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, status: int
):
    caplog.set_level(logging.DEBUG)  # every logger, httpx2's included
    bao = FakeBao(write_status=status)
    write = vault(tmp_path, bao).create_env(
        "shikigami", {"glitchtip-token": SecretStr("leak-me")}, "env-20261105-a1b2c3"
    )
    if status == 200:
        await write
    else:
        with pytest.raises(NotWrittenError) as refused:
            await write
        assert "leak-me" not in str(refused.value)
    [(path, _, body)] = bao.writes
    assert path == "/v1/apps/data/meru/shikigami/eso/env-20261105-a1b2c3"
    assert body["data"] == {"glitchtip-token": "leak-me"}  # it reaches OpenBao ...
    assert "leak-me" not in caplog.text + "".join(repr(r.args) for r in caplog.records)  # ... and nothing else
