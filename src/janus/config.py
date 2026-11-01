"""Janus's settings, from JANUS_* environment variables (Nüwa's env + secretEnv)."""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # extra="ignore": Kubernetes adds JANUS_PORT, JANUS_SERVICE_HOST, ... for the Service named janus.
    model_config = SettingsConfigDict(env_prefix="JANUS_", extra="ignore")

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

    work_dir: Path = Path("/tmp")  # the root filesystem is read-only; each proposal clones into its own folder here
    http_timeout: float = 20.0
    git_timeout: float = 60.0
