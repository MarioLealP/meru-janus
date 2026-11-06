import os
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from janus.config import Settings
from janus.core.propose import Proposer, pr_from, title_for, topic_for
from janus.models.requests import Submission


def bare_repo(tmp_path: Path) -> Path:
    """A stand-in for Meru/requests: a bare repo with main holding one request."""
    seed, bare = tmp_path / "seed", tmp_path / "requests.git"
    run = lambda *a, cwd=seed: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # ruff: ignore[lambda-assignment]
    seed.mkdir()
    run("init", "--quiet", "--initial-branch=main")
    (seed / "apps").mkdir()
    (seed / "apps" / "echo.yaml").write_text("kind: App\n")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "seed")
    run("clone", "--quiet", "--bare", str(seed), str(bare), cwd=tmp_path)
    run("config", "receive.advertisePushOptions", "true", cwd=bare)
    return bare


def proposer_for(bare: Path, token: str, work: Path) -> Proposer:
    return Proposer(
        repo_url=f"file://{bare}",
        user="janus",
        token=SecretStr(token),
        work_dir=work,
        timeout=30,
        git_path=os.environ["PATH"],
    )


async def test_a_proposal_lands_as_one_commit_on_refs_for_main(tmp_path: Path):
    bare = bare_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    proposer = proposer_for(bare, "secret-token", work)
    submission = Submission(path="apps/golem.yaml", content="kind: App\nname: golem\n")
    title, _, head = await proposer.propose(submission, "agent-claude", "c0ffee")
    assert title == "feat(request): add app golem"
    show = lambda *a: subprocess.run(["git", *a], cwd=bare, check=True, capture_output=True, text=True).stdout  # ruff: ignore[lambda-assignment]
    assert head == show("rev-parse", "refs/for/main").strip()  # what an approval of this PR is for
    assert show("show", "refs/for/main:apps/golem.yaml") == "kind: App\nname: golem\n"
    message = show("log", "-1", "--format=%an <%ae>%n%B", "refs/for/main")
    assert message.startswith("janus <janus@clockmost.com>\nfeat(request): add app golem\n")
    assert "Requested-By: agent-claude" in message
    assert "Checked-Against: c0ffee" in message
    assert show("rev-list", "--count", "refs/for/main") == "2\n"  # main's commit + the proposal, nothing else
    assert list(work.iterdir()) == []  # the clone is gone
    assert "secret-token" not in show("log", "--all", "-p")


async def test_changing_an_existing_request_says_change(tmp_path: Path):
    bare = bare_repo(tmp_path)
    proposer = proposer_for(bare, "t", tmp_path)
    title, _, _ = await proposer.propose(Submission(path="apps/echo.yaml", content="x\n"), "mario", "c")
    assert title == "feat(request): change app echo"


def test_names():
    db = Submission(path="databases/thoth.yaml", content="")
    assert title_for(db, exists=False) == "feat(request): add database thoth"
    assert topic_for(db) == "request-databases-thoth"
    gone = Submission(path="apps/echo.yaml", content="spec:\n  removed: true\n")
    assert title_for(gone, exists=True) == "feat(request): remove app echo"
    assert pr_from("remote: Create a PR\nremote:   https://git.clockmost.com/Meru/requests/pulls/42\n") == (
        42,
        "https://git.clockmost.com/Meru/requests/pulls/42",
    )
    assert pr_from("nothing") == (None, None)


def test_git_runs_with_the_pods_own_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", "/opt/git/bin")
    assert Settings().git_path == "/opt/git/bin"
    monkeypatch.delenv("PATH")
    assert Settings().git_path == "/usr/bin:/bin"


def test_the_token_stays_out_of_the_proposers_repr(tmp_path: Path):
    assert "secret-token" not in repr(proposer_for(tmp_path, "secret-token", tmp_path))
