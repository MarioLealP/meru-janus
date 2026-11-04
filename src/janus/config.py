"""Janus's settings, from JANUS_* environment variables (Nüwa's env + secretEnv)."""

import tempfile
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything Janus reads from the environment."""

    # extra="ignore": Kubernetes adds JANUS_PORT, JANUS_SERVICE_HOST, ... for the Service named janus.
    model_config = SettingsConfigDict(env_prefix="JANUS_", extra="ignore", frozen=True)

    gitea_url: str = "https://git.clockmost.com"
    org: str = "Meru"
    requests_repo: str = "requests"
    # Janus's Gitea bot (D93): Read on Meru/requests - enough to read the private repo and open AGit PRs. Until the
    # admin run makes it, Janus lists and validates nothing and opens nothing (503).
    gitea_user: str = "janus"
    gitea_token: SecretStr | None = None

    shikigami_validate_url: str = "http://shikigami.shikigami.svc/validate"  # D96: the running format decides
    # Caddy's forward-auth sets it after Authentik let the caller in (a person's session or an agent's Bearer JWT,
    # D94); requests without it are refused - nothing reaches Janus around Caddy (its NetworkPolicy).
    identity_header: str = "X-Authentik-Username"

    # Approvals (D98): Janus starts Authentik's silent check for the xolotl-approvals app and hands the code to Xolotl,
    # which alone can redeem it. No secret here.
    authorize_url: str = "https://auth.clockmost.com/application/o/authorize/"
    approvals_client_id: str = "xolotl-approvals"
    approvals_redirect_uri: str = "https://atlas.clockmost.com/api/janus/approve/callback"
    atlas_origin: str = "https://atlas.clockmost.com"
    xolotl_approve_url: str = "http://xolotl.xolotl.svc/approve"
    approvers_group: str = "meru-approvers"  # only to fail early with a clear page - Xolotl checks the token itself
    groups_header: str = "X-Authentik-Groups"  # forward-auth's list, "|"-separated

    # A new app's secret values (D99): Janus's own OpenBao login, create-only on apps/meru/<app>/eso/env.
    openbao_addr: str = "https://openbao.clockmost.internal:8204"
    openbao_role: str = "meru-janus-secrets"
    openbao_dir: Path = Path("/var/run/meru/openbao")  # token, ca.crt (nuwa-app openbaoLogin)

    # The root filesystem is read-only; each proposal clones into its own folder here.
    work_dir: Path = Path(tempfile.gettempdir())
    http_timeout: float = 20.0
    git_timeout: float = 60.0
    git_path: str = Field(default="/usr/bin:/bin", validation_alias="PATH")  # the pod's own PATH, for git
