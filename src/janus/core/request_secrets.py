"""An app's secret values: a new app's (D99) and a running app's changes (D123, D124).

The request names them (`spec.secrets: [{name, generate, entry}]`), the requester types the values or Janus makes them,
and Janus writes them to OpenBao before the request's PR opens. A new app's go to apps/meru/<app>/eso/env, one key per
name; Shikigami wires the names into the app (its marker PR) and ESO reads the entry. A change to a running app marks
the names it adds or replaces `entry: new`: Janus picks a fresh entry name (env-<yyyymmdd>-<6 hex>), writes it into the
file in place of `new`, and creates that entry - the app's values.yaml moves onto it in an ordinary PR (D124).

Janus's OpenBao login (role meru-janus-secrets) may only *create* those entries: an existing one is refused by OpenBao
itself. No read, list or delete. The values stay SecretStr from the request to the one OpenBao write and are never
logged or kept.
"""

import re
import secrets
import ssl
from datetime import date
from pathlib import Path
from typing import Annotated

import httpx2
import yaml
from pydantic import ConfigDict, Field, SecretStr, StringConstraints, ValidationError

from janus.models.requests import Incoming, Value

# The request format's secret name (Shikigami's request_format.Name): it becomes an env var, NAME_UPPER.
SecretName = Annotated[str, StringConstraints(pattern=r"^[a-z]([-a-z0-9]{0,28}[a-z0-9])?$")]


NEW = "new"  # in a submitted file only: Janus puts the change's entry name there before anything else sees it
Entry = Annotated[str, StringConstraints(pattern=r"^(env(-[0-9]{8}-[0-9a-f]{6})?|new)$")]
_NEW_LINE = re.compile(r"^(?P<lead>[ \t]+(?:- )?entry:[ \t]*)(?P<q>[\"']?)new(?P=q)[ \t]*$", re.MULTILINE)


class Requested(Incoming):
    """One secret a request names; `generate` means Janus makes the value, `entry` is where it lives in OpenBao."""

    name: SecretName
    generate: bool = False
    entry: Entry = "env"


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


def new_entry(today: date) -> str:
    """A change's entry name (D123): the day, for people reading it, and 6 random hex so two changes never meet."""
    return f"env-{today:%Y%m%d}-{secrets.token_hex(3)}"


def with_entry(content: str, entry: str) -> str:
    """The request file with every `entry: new` line set to the change's entry; NotWrittenError if there is none."""
    filled, count = _NEW_LINE.subn(lambda m: f"{m['lead']}{entry}", content)
    if count == 0:
        raise NotWrittenError("no `entry: new` line to fill")
    return filled


def values_for(wanted: list[Requested], given: dict[str, SecretStr]) -> dict[str, SecretStr]:
    """Name -> value: the given ones for the names to type, generated ones for the rest.

    NotWrittenError unless exactly the names to type were given. Still SecretStr: only the OpenBao write unwraps them.
    """
    typed = {s.name for s in wanted if not s.generate}
    if missing := sorted(typed - given.keys()):
        raise NotWrittenError(f"no value for {', '.join(missing)}")
    if extra := sorted(given.keys() - typed):
        raise NotWrittenError(f"values for names the request doesn't ask you to type: {', '.join(extra)}")
    if empty := sorted(name for name, value in given.items() if not value.get_secret_value()):
        raise NotWrittenError(f"empty value for {', '.join(empty)}")
    return dict(given) | {s.name: SecretStr(secrets.token_urlsafe(32)) for s in wanted if s.generate}


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

    async def create_env(self, app: str, values: dict[str, SecretStr], entry: str = "env") -> None:
        """Log in and create one of the app's entries (env, or a change's); NotWrittenError if OpenBao says no."""
        async with self._client() as http:
            jwt = (self.login_dir / "token").read_text().strip()
            login = await http.post(f"{self.addr}/v1/auth/kubernetes/login", json={"role": self.role, "jwt": jwt})
            if login.status_code != 200:
                raise NotWrittenError(f"Janus can't log in to OpenBao ({login.status_code})")
            token = _Login.model_validate_json(login.content).auth.client_token
            # cas 0: only if the entry has never been written. The policy allows create only, so an existing entry
            # is refused either way - 403 from the policy, 400 from the check-and-set.
            response = await http.post(
                f"{self.addr}/v1/apps/data/meru/{app}/eso/{entry}",
                headers={"X-Vault-Token": token.get_secret_value()},
                json={
                    "options": {"cas": 0},
                    "data": {name: value.get_secret_value() for name, value in values.items()},
                },
            )
        if response.status_code in (400, 403) and entry == "env":
            raise NotWrittenError(
                f"{app} already has secret values in OpenBao (an earlier submit, or a retired app's not purged yet) "
                "- pick another name, or ask Mario"
            )
        if response.status_code in (400, 403):
            raise NotWrittenError(f"OpenBao refused {app}'s new entry {entry} ({response.status_code})")
        if response.status_code not in (200, 204):
            raise NotWrittenError(f"OpenBao answered {response.status_code}")
