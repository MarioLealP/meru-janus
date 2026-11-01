import subprocess
from pathlib import Path

from janus.models import Submission
from janus.propose import Proposer, pr_from, title_for, topic_for


def bare_repo(tmp_path: Path) -> Path:
    """A stand-in for Meru/requests: a bare repo with main holding one request."""
    seed, bare = tmp_path / "seed", tmp_path / "requests.git"
    run = lambda *a, cwd=seed: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
    seed.mkdir()
    run("init", "--quiet", "--initial-branch=main")
    (seed / "apps").mkdir()
    (seed / "apps" / "echo.yaml").write_text("kind: App\n")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "-m", "seed")
    run("clone", "--quiet", "--bare", str(seed), str(bare), cwd=tmp_path)
    run("config", "receive.advertisePushOptions", "true", cwd=bare)
    return bare


async def test_a_proposal_lands_as_one_commit_on_refs_for_main(tmp_path: Path):
    bare = bare_repo(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    proposer = Proposer(f"file://{bare}", "janus", "secret-token", work, timeout=30)
    submission = Submission(path="apps/golem.yaml", content="kind: App\nname: golem\n")
    title, _ = await proposer.propose(submission, "agent-claude", "c0ffee")
    assert title == "feat(request): add app golem"
    show = lambda *a: subprocess.run(["git", *a], cwd=bare, check=True, capture_output=True, text=True).stdout  # noqa: E731
    assert show("show", "refs/for/main:apps/golem.yaml") == "kind: App\nname: golem\n"
    message = show("log", "-1", "--format=%an <%ae>%n%B", "refs/for/main")
    assert message.startswith("janus <janus@clockmost.com>\nfeat(request): add app golem\n")
    assert "Requested-By: agent-claude" in message and "Checked-Against: c0ffee" in message
    assert show("rev-list", "--count", "refs/for/main") == "2\n"  # main's commit + the proposal, nothing else
    assert list(work.iterdir()) == []  # the clone is gone
    assert "secret-token" not in show("log", "--all", "-p")


async def test_changing_an_existing_request_says_change(tmp_path: Path):
    bare = bare_repo(tmp_path)
    proposer = Proposer(f"file://{bare}", "janus", "t", tmp_path, timeout=30)
    title, _ = await proposer.propose(Submission(path="apps/echo.yaml", content="x\n"), "mario", "c")
    assert title == "feat(request): change app echo"


def test_names():
    db = Submission(path="databases/thoth.yaml", content="")
    assert title_for(db, exists=False) == "feat(request): add database thoth"
    assert topic_for(db) == "request-databases-thoth"
    assert pr_from("remote: Create a PR\nremote:   https://git.clockmost.com/Meru/requests/pulls/42\n") == (
        42,
        "https://git.clockmost.com/Meru/requests/pulls/42",
    )
    assert pr_from("nothing") == (None, None)
