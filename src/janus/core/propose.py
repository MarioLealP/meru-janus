"""Opening a request PR on Meru/requests as Janus's bot (D93).

AGit: a push to refs/for/main, which Gitea turns into a PR. Read on the repo is enough for that; the bot can't push main
or merge (only Xolotl writes main, D82).

The token reaches git through GIT_CONFIG_* environment variables (an http.extraHeader), never the command line or a
file - nothing in /proc/<pid>/cmdline, nothing left in the clone.
"""

import asyncio
import re
import tempfile
from pathlib import Path

from pydantic import ConfigDict, Field, SecretStr

from janus.core.remove import is_removed
from janus.models.requests import Submission, Value

PR_URL = re.compile(r"(https?://\S+/pulls/(\d+))")


class ProposeError(RuntimeError):
    """Git couldn't make the PR: it failed or timed out."""


def title_for(submission: Submission, exists: bool) -> str:
    """The PR title: add, change or remove, by whether the file is on main and what it says."""
    kind, file = submission.path.split("/")
    name = file.removesuffix(".yaml")
    what = "database" if kind == "databases" else "app"
    verb = ("remove" if is_removed(submission.content) else "change") if exists else "add"
    return f"feat(request): {verb} {what} {name}"


def topic_for(submission: Submission) -> str:
    """One PR per request file: proposing the same file again updates that PR instead of opening another."""
    kind, file = submission.path.split("/")
    return f"request-{kind}-{file.removesuffix('.yaml')}"


class Proposer(Value):
    """Pushes proposals to the requests repo as the bot, one at a time."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    repo_url: str
    user: str
    token: SecretStr
    work_dir: Path
    timeout: float
    git_path: str
    # One proposal at a time: they're rare, and the clone folder stays simple.
    lock: asyncio.Lock = Field(default_factory=asyncio.Lock, repr=False, exclude=True)

    def _env(self) -> dict[str, str]:
        return {
            "HOME": str(self.work_dir),  # git wants a home; the root filesystem is read-only
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: token {self.token.get_secret_value()}",
            "PATH": self.git_path,
        }

    async def _git(self, cwd: Path, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", *args, cwd=cwd, env=self._env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), self.timeout)
        except TimeoutError:
            process.kill()
            raise ProposeError(f"git {args[0]} timed out") from None
        text = output.decode(errors="replace")
        if process.returncode != 0:
            raise ProposeError(f"git {args[0]} failed: {text.strip()[-300:]}")
        return text

    async def propose(self, submission: Submission, requested_by: str, checked_against: str) -> tuple[str, str, str]:
        """Push the file as a PR. Returns (title, git's push output - it names the PR, the PR's head commit)."""
        async with self.lock:
            with tempfile.TemporaryDirectory(dir=self.work_dir) as tmp:
                clone = Path(tmp)
                await self._git(clone, "init", "--quiet", "--initial-branch=work")
                await self._git(clone, "fetch", "--quiet", "--depth=1", self.repo_url, "main")
                await self._git(clone, "checkout", "--quiet", "FETCH_HEAD")
                target = clone / submission.path
                exists = target.exists()
                title = title_for(submission, exists)
                target.parent.mkdir(exist_ok=True)
                target.write_text(submission.content)
                await self._git(clone, "add", submission.path)
                # The body becomes the PR's description: Gitea uses the commit message when the push names none,
                # and push options can't carry newlines anyway.
                message = (
                    f"{title}\n\n"
                    f"Requested through Janus - waits for Mario's approval (D82). Checked by Shikigami against\n"
                    f"requests main {checked_against[:12]}.\n\n"
                    f"Requested-By: {requested_by}\nChecked-Against: {checked_against}\n"
                )
                author = f"{self.user} <{self.user}@clockmost.com>"
                await self._git(
                    clone, "-c", f"user.name={self.user}", "-c", f"user.email={self.user}@clockmost.com",
                    "commit", "--quiet", f"--author={author}", "-m", message,
                )  # fmt: skip
                head = (await self._git(clone, "rev-parse", "HEAD")).strip()
                output = await self._git(
                    clone, "push", "--porcelain", self.repo_url, "HEAD:refs/for/main",
                    "-o", f"topic={topic_for(submission)}", "-o", f"title={title}",
                )  # fmt: skip
                return title, output, head


def pr_from(output: str) -> tuple[int | None, str | None]:
    """The PR number and URL from git's push output, or None for both when it names none."""
    match = PR_URL.search(output)
    return (int(match.group(2)), match.group(1)) if match else (None, None)
