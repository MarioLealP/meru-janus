"""The command line for Meru's front door, for agents and people alike (D94).

    janus pending                 the requests waiting for approval
    janus submit FILE [FILE ...]  propose request files: .../apps/<name>.yaml or .../databases/<name>.yaml

A new app's request may name secrets (`spec.secrets: [{name, generate}]`, D99): for each one not generated, submit
takes the value from JANUS_SECRET_<NAME> (discord-token -> JANUS_SECRET_DISCORD_TOKEN) or asks for it (nothing shows
while typing). Janus writes the values to OpenBao before the PR opens; they never go in the file, the PR or a log.
A file already on main keeps the values its app has (a restore of a retired app, D106), except the names a change
marks `entry: new` (D123): submit asks for those, Janus writes them to a fresh entry and puts its name in the file.

It signs in as an Authentik service account (agent-claude by default): the account's app password - from KeePassXC
through the Secret Service, or JANUS_CLIENT_PASSWORD where there's no desktop - is exchanged for a Bearer JWT for
the atlas application (client credentials). The token lives in memory for this one run, never on disk. Caddy checks
it with Authentik and drops it before Janus, which only sees who asked (the same path as a browser session).

Only the shared models come from the server's code; nothing here reads Janus's own settings.
"""

import argparse
import getpass
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from functools import cached_property
from pathlib import Path

import httpx2
from pydantic import ConfigDict, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from janus.core.request_secrets import NEW, requested
from janus.models.requests import Incoming, OnMain, Pending, Rejected, Submission, Submitted, Value

# What the token must carry for Authentik's proxy outpost to know who it is: without ak_proxy (and the profile claims
# it fills the X-Authentik-* headers from) the token is valid but the outpost answers 403.
SCOPES = "openid profile email ak_proxy"


class ClientSettings(BaseSettings):
    """The command line's settings, from JANUS_CLIENT_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="JANUS_CLIENT_", extra="ignore", frozen=True)

    url: str = "https://atlas.clockmost.com/api/janus"
    token_url: str = "https://auth.clockmost.com/application/o/token/"
    client_id: str = "TbOHmPug3NjRqzNTBJKG1gPHpFGfsq3QjvJ2hFDc"  # the atlas's Authentik provider (an identifier)
    username: str = "agent-claude"
    # The KeePassXC entry (group Desktop apps) the password is looked up by, when JANUS_CLIENT_PASSWORD isn't set.
    secret_title: str = "agent-claude - Authentik app password"
    password: SecretStr | None = None
    timeout: float = 60.0  # a submit clones Meru/requests and asks Shikigami before it answers


class Token(Incoming):
    """Authentik's answer to the sign-in."""

    access_token: SecretStr
    expires_in: int


class CliError(Exception):
    """Something the person running the command should read; main() prints it and exits 1."""


def password(s: ClientSettings) -> SecretStr:
    """The app password: JANUS_CLIENT_PASSWORD, else the Secret Service entry."""
    if s.password is not None:
        return s.password
    try:
        found = subprocess.run(
            ["secret-tool", "lookup", "Title", s.secret_title], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError as e:
        raise CliError("no secret-tool here: set JANUS_CLIENT_PASSWORD instead") from e
    if found.returncode != 0 or not found.stdout:
        raise CliError(f"no {s.secret_title!r} in the Secret Service - is KeePassXC unlocked?")
    return SecretStr(found.stdout.rstrip("\n"))


def sign_in(http: httpx2.Client, s: ClientSettings) -> SecretStr:
    """The app password -> a Bearer JWT for the atlas (Authentik's client credentials grant)."""
    form = {
        "grant_type": "client_credentials",
        "client_id": s.client_id,
        "username": s.username,
        "password": password(s).get_secret_value(),
        "scope": SCOPES,
    }
    response = http.post(s.token_url, data=form)
    if response.status_code != 200:
        raise CliError(f"Authentik refused the sign-in as {s.username}: {response.status_code} {response.text[:200]}")
    return Token.model_validate_json(response.content).access_token


def submission(file: Path) -> Submission:
    """The file's request path is its folder and name, so it can be submitted from any checkout of Meru/requests."""
    try:
        content = file.read_text()
    except OSError as e:
        raise CliError(f"{file}: {e.strerror}") from e
    try:
        return Submission(path=f"{file.parent.name}/{file.name}", content=content)
    except ValidationError as e:
        raise CliError(f"{file}: not a request file (apps/<name>.yaml or databases/<name>.yaml, at most 64 KiB)") from e


def env_name(secret: str) -> str:
    """The environment variable a secret's value comes from: discord-token -> JANUS_SECRET_DISCORD_TOKEN."""
    return "JANUS_SECRET_" + secret.upper().replace("-", "_")


def secret_values(
    item: Submission,
    ask: Callable[[str], str] = getpass.getpass,
    environ: Mapping[str, str] = os.environ,
    interactive: Callable[[], bool] = sys.stdin.isatty,
    only_new: bool = False,
) -> dict[str, SecretStr]:
    """The values of the secrets the request names and doesn't generate: from the environment, else asked for.

    `only_new` for an app already on main: just the names a change marks `entry: new` (D123) - the rest keep theirs.
    `environ` is read directly, not through settings: the variable names come from the request file, so no settings
    class can declare them.
    """
    values = {}
    for secret in requested(item.path, item.content):
        if secret.generate or (only_new and secret.entry != NEW):
            continue
        value = environ.get(env_name(secret.name))
        if value is None:
            if not interactive():
                raise CliError(f"{item.path}: no value for {secret.name} - set {env_name(secret.name)}")
            value = ask(f"{item.path} {secret.name} (hidden): ")
        if not value:
            raise CliError(f"{item.path}: empty value for {secret.name}")
        values[secret.name] = SecretStr(value)
    return values


class Janus(Value):
    """Janus's API as the signed-in service account; signs in at the first call."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    http: httpx2.Client
    settings: ClientSettings

    @cached_property
    def token(self) -> SecretStr:
        """The Bearer JWT, from one sign-in at the first call: nothing is signed in for a file that fails its check."""
        return sign_in(self.http, self.settings)

    def _call(self, method: str, path: str, body: dict | None = None) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {self.token.get_secret_value()}"}
        response = self.http.request(method, f"{self.settings.url}{path}", headers=headers, json=body)
        if response.status_code in (302, 401, 403):
            raise CliError(f"the atlas didn't let {self.settings.username} in ({response.status_code})")
        return response

    def pending(self) -> list[Pending]:
        """The requests waiting for approval."""
        response = self._call("GET", "/requests")
        if response.status_code != 200:
            raise CliError(f"Janus: {response.status_code} {response.text[:200]}")
        return [Pending.model_validate(item) for item in response.json()]

    def on_main(self) -> set[str]:
        """The request files on Meru/requests main, by path."""
        response = self._call("GET", "/apps")
        if response.status_code != 200:
            raise CliError(f"Janus: {response.status_code} {response.text[:200]}")
        return {OnMain.model_validate(item).path for item in response.json()}

    def submit(self, item: Submission, values: Mapping[str, SecretStr] | None = None) -> Submitted | Rejected:
        """Propose one request file with its secret values; a Rejected is Shikigami's verdict, not an error."""
        body = item.model_dump()
        if values:
            body["secrets"] = {name: value.get_secret_value() for name, value in values.items()}
        response = self._call("POST", "/requests", body)
        if response.status_code == 201:
            return Submitted.model_validate_json(response.content)
        if response.status_code == 422:
            try:
                return Rejected.model_validate_json(response.content)
            except ValidationError:
                pass  # FastAPI's own 422: the body didn't fit Submission - shouldn't happen, it was checked here
        raise CliError(f"Janus: {response.status_code} {response.text[:200]}")


def show_pending(items: list[Pending]) -> None:
    """Print the pending requests for a person, one line each."""
    if not items:
        print("nothing waiting for approval")
    for p in items:
        print(f"#{p.pr} {p.title}  ({p.requested_by or p.author})  {p.url}")


def run(argv: Sequence[str], http: httpx2.Client, s: ClientSettings) -> int:
    """Parse the arguments and run the command; returns the exit status."""
    parser = argparse.ArgumentParser(prog="janus", description="Meru's front door: request apps and databases.")
    parser.add_argument("--json", action="store_true", help="print the answers as JSON lines (for scripts)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending", help="the requests waiting for approval")
    submit = commands.add_parser("submit", help="propose request files as PRs on Meru/requests")
    submit.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)

    janus = Janus(http=http, settings=s)
    if args.command == "pending":
        items = janus.pending()
        if args.json:
            for p in items:
                print(p.model_dump_json())
        else:
            show_pending(items)
        return 0

    items = [submission(f) for f in args.files]  # every file checked before anything is sent
    # and every value asked for: all of a new app's; for one already on main only a change's `entry: new` names (D123) -
    # without one it keeps its app's values (a restore, D106)
    asking = [item for item in items if any(not secret.generate for secret in requested(item.path, item.content))]
    known = janus.on_main() if asking else set()
    values = [secret_values(item, only_new=item.path in known) for item in items]
    status = 0
    for item, given in zip(items, values, strict=True):
        answer = janus.submit(item, given)
        if args.json:
            print(answer.model_dump_json())
        elif isinstance(answer, Submitted):
            print(f"{item.path}: {answer.url or answer.title} - waiting for approval")
        else:
            print(f"{item.path}: rejected (checked against {answer.checked_against[:12]})")
            for problem in answer.problems:
                print(f"  - {problem}")
        if isinstance(answer, Rejected):
            status = 1
    return status


def main() -> None:
    """The `janus` command: exits 1 with a readable line on any expected failure."""
    s = ClientSettings()
    try:
        with httpx2.Client(timeout=s.timeout) as http:
            sys.exit(run(sys.argv[1:], http, s))
    except CliError as e:
        print(f"janus: {e}", file=sys.stderr)
        sys.exit(1)
    except httpx2.HTTPError as e:
        print(f"janus: can't reach {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
