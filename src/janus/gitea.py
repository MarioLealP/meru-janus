"""Reading Meru/requests: the open PRs (the pending requests, D93) and what's on main.

Read-only; the bot's token, the repo is private.
"""

import re

import httpx2
from pydantic import TypeAdapter

from janus.models import Incoming, OnMain, Pending
from janus.remove import is_removed

REQUESTED_BY = re.compile(r"^Requested-By: (\S+)$", re.MULTILINE)


class _User(Incoming):
    login: str


class _Head(Incoming):
    sha: str


class _Pull(Incoming):
    number: int
    title: str
    html_url: str
    body: str | None = None
    user: _User
    head: _Head


class _File(Incoming):
    filename: str


class _Entry(Incoming):
    name: str
    path: str
    type: str


_PULLS = TypeAdapter(list[_Pull])
_ENTRIES = TypeAdapter(list[_Entry])
_FILES = TypeAdapter(list[_File])


async def pending(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str, token: str, bot: str) -> list[Pending]:
    """Open PRs, oldest first as Gitea lists them.

    requested_by only on the bot's own PRs: anyone can write a Requested-By line in a PR they open themselves.
    """
    headers = {"Authorization": f"token {token}"}
    api = f"{gitea_url}/api/v1/repos/{org}/{repo}"
    response = await http.get(f"{api}/pulls", params={"state": "open", "limit": 50}, headers=headers)
    response.raise_for_status()
    out = []
    for pull in _PULLS.validate_json(response.content):
        files = await http.get(f"{api}/pulls/{pull.number}/files", headers=headers)
        files.raise_for_status()
        asked = REQUESTED_BY.search(pull.body or "")
        out.append(
            Pending(
                pr=pull.number,
                title=pull.title,
                url=pull.html_url,
                author=pull.user.login,
                requested_by=asked.group(1) if asked and pull.user.login == bot else None,
                head=pull.head.sha,
                files=[f.filename for f in _FILES.validate_json(files.content)],
            )
        )
    return out


class NotOnMainError(LookupError):
    """The path isn't on Meru/requests main."""


async def read_main(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str, token: str, path: str) -> str:
    """A request file's text on main; NotOnMainError when Gitea has no such file."""
    response = await http.get(
        f"{gitea_url}/api/v1/repos/{org}/{repo}/raw/{path}",
        params={"ref": "main"},
        headers={"Authorization": f"token {token}"},
    )
    if response.status_code == 404:
        raise NotOnMainError(path)
    response.raise_for_status()
    return response.text


async def on_main(http: httpx2.AsyncClient, gitea_url: str, org: str, repo: str, token: str) -> list[OnMain]:
    """Every request file on main, apps then databases, by name."""
    headers = {"Authorization": f"token {token}"}
    api = f"{gitea_url}/api/v1/repos/{org}/{repo}"
    out = []
    for folder, kind in (("apps", "app"), ("databases", "database")):
        response = await http.get(f"{api}/contents/{folder}", params={"ref": "main"}, headers=headers)
        if response.status_code == 404:
            continue  # no databases yet
        response.raise_for_status()
        for entry in sorted(_ENTRIES.validate_json(response.content), key=lambda e: e.name):
            if entry.type != "file" or not entry.name.endswith(".yaml"):
                continue
            text = await read_main(http, gitea_url, org, repo, token, entry.path)
            out.append(
                OnMain(path=entry.path, kind=kind, name=entry.name.removesuffix(".yaml"), removed=is_removed(text))
            )
    return out
