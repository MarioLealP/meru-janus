"""What the routes work with: the settings and the outside systems, built once at startup."""

import httpx2
from fastapi import HTTPException
from pydantic import ConfigDict

from janus.config import Settings
from janus.models import Value
from janus.propose import Proposer
from janus.request_secrets import Vault

NO_BOT = "Janus has no Gitea bot yet"


class Backends(Value):
    """The app's shared dependencies; `proposer` is None until Janus has its Gitea bot."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    settings: Settings
    http: httpx2.AsyncClient
    proposer: Proposer | None
    vault: Vault

    def bot(self) -> str:
        """The Gitea bot's token; a 503 until there is one."""
        if not self.settings.gitea_token:
            raise HTTPException(503, NO_BOT)
        return self.settings.gitea_token.get_secret_value()

    def ready_proposer(self) -> Proposer:
        """The proposer; a 503 until there is one."""
        if self.proposer is None:
            raise HTTPException(503, NO_BOT)
        return self.proposer


def backends_for(s: Settings, http: httpx2.AsyncClient) -> Backends:
    """Wire the outside systems from the settings. Nothing connects yet."""
    repo_url = f"{s.gitea_url}/{s.org}/{s.requests_repo}.git"
    proposer = None
    if s.gitea_token:
        proposer = Proposer(
            repo_url=repo_url,
            user=s.gitea_user,
            token=s.gitea_token,
            work_dir=s.work_dir,
            timeout=s.git_timeout,
            git_path=s.git_path,
        )
    vault = Vault(addr=s.openbao_addr, role=s.openbao_role, login_dir=s.openbao_dir, timeout=s.http_timeout)
    return Backends(settings=s, http=http, proposer=proposer, vault=vault)
