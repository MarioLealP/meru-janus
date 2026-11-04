"""A new app's secret values (D99).

The request names them (`spec.secrets: [{name, generate}]`), the requester types the values or Janus makes them, and
Janus writes them to OpenBao - apps/meru/<app>/eso/env, one key per name - before the request's PR opens. Shikigami
wires the names into the app (its marker PR); ESO reads the entry.

Janus's OpenBao login (role meru-janus-secrets) may only *create* that entry: an app that has one already - running,
retired with its leftovers not purged yet, or submitted before - is refused by OpenBao itself. No read, list or
delete. The values pass through here once and are never logged or kept.
"""

import secrets
import ssl
from pathlib import Path
from typing import Annotated

import httpx2
import yaml
from pydantic import ConfigDict, Field, SecretStr, StringConstraints, ValidationError

from janus.models import Incoming, Value

# The request format's secret name (Shikigami's request_format.Name): it becomes an env var, NAME_UPPER.
SecretName = Annotated[str, StringConstraints(pattern=r"^[a-z]([-a-z0-9]{0,28}[a-z0-9])?$")]


class Requested(Incoming):
    """One secret a request names; `generate` means Janus makes the value."""

    name: SecretName
    generate: bool = False


class _Spec(Incoming):
    secrets: list[Requested] = Field(default_factory=list)


class _Request(Incoming):
    spec: _Spec


class NotWrittenError(Exception):
    """The values didn't go to OpenBao: the reason, said to the requester as is (it never holds a value)."""


def requested(path: str, content: str) -> list[Requested]:
    """The secrets an app's request names. Shikigami has validated the file already; anything unreadable has none."""
    if not path.startswith("apps/"):
        return []
    try:
        return _Request.model_validate(yaml.safe_load(content)).spec.secrets
    except yaml.YAMLError, ValidationError:
        return []


def values_for(wanted: list[Requested], given: dict[str, SecretStr]) -> dict[str, str]:
    """Name -> value: the given ones for the names to type, generated ones for the rest.

    NotWrittenError unless exactly the names to type were given.
    """
    typed = {s.name for s in wanted if not s.generate}
    if missing := sorted(typed - given.keys()):
        raise NotWrittenError(f"no value for {', '.join(missing)}")
    if extra := sorted(given.keys() - typed):
        raise NotWrittenError(f"values for names the request doesn't ask you to type: {', '.join(extra)}")
    out = {name: value.get_secret_value() for name, value in given.items()}
    if empty := sorted(name for name, value in out.items() if not value):
        raise NotWrittenError(f"empty value for {', '.join(empty)}")
    return out | {s.name: secrets.token_urlsafe(32) for s in wanted if s.generate}


class _Auth(Incoming):
    client_token: SecretStr


class _Login(Incoming):
    auth: _Auth


class Vault(Value):
    """Janus's own OpenBao login, fresh for each write - they're rare.

    nuwa-app openbaoLogin mounts the pod's token meant for OpenBao and OpenBao's CA. Without those files Janus takes no
    secret values.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    addr: str
    role: str
    login_dir: Path
    timeout: float
    transport: httpx2.AsyncBaseTransport | None = None  # tests only

    def _client(self) -> httpx2.AsyncClient:
        if self.transport is not None:
            return httpx2.AsyncClient(timeout=self.timeout, transport=self.transport)
        tls = ssl.create_default_context(cafile=self.login_dir / "ca.crt")
        return httpx2.AsyncClient(timeout=self.timeout, verify=tls)

    @property
    def ready(self) -> bool:
        """Whether the login files are mounted."""
        return (self.login_dir / "token").is_file() and (self.login_dir / "ca.crt").is_file()

    async def create_env(self, app: str, values: dict[str, str]) -> None:
        """Log in and create the app's env entry; NotWrittenError if OpenBao says no."""
        async with self._client() as http:
            jwt = (self.login_dir / "token").read_text().strip()
            login = await http.post(f"{self.addr}/v1/auth/kubernetes/login", json={"role": self.role, "jwt": jwt})
            if login.status_code != 200:
                raise NotWrittenError(f"Janus can't log in to OpenBao ({login.status_code})")
            token = _Login.model_validate_json(login.content).auth.client_token
            # cas 0: only if the entry has never been written. The policy allows create only, so an existing entry
            # is refused either way - 403 from the policy, 400 from the check-and-set.
            response = await http.post(
                f"{self.addr}/v1/apps/data/meru/{app}/eso/env",
                headers={"X-Vault-Token": token.get_secret_value()},
                json={"options": {"cas": 0}, "data": values},
            )
        if response.status_code in (400, 403):
            raise NotWrittenError(
                f"{app} already has secret values in OpenBao (an earlier submit, or a retired app's not purged yet) "
                "- pick another name, or ask Mario"
            )
        if response.status_code not in (200, 204):
            raise NotWrittenError(f"OpenBao answered {response.status_code}")
