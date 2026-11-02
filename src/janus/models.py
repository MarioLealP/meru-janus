"""What Janus takes in and hands back. Incoming: what other services answer (extra fields ignored)."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

# The same shape Shikigami's /validate accepts (D96): a request file, never anything else in the repo.
RequestPath = Annotated[str, StringConstraints(pattern=r"^(apps|databases)/[a-z0-9]([-a-z0-9]*[a-z0-9])?\.yaml$")]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Incoming(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Submission(Value):
    """A request file to propose: apps/<name>.yaml or databases/<name>.yaml, as it should read on main."""

    path: RequestPath
    content: Annotated[str, StringConstraints(max_length=65536)]


class Submit(Value):
    """What the submit endpoint takes: the request file, plus the values of the secrets it names that aren't generated
    (D99). The values go to OpenBao only - never to Shikigami, the PR or a log."""

    path: RequestPath
    content: Annotated[str, StringConstraints(max_length=65536)]
    secrets: dict[Annotated[str, StringConstraints(max_length=30)], SecretStr] = Field(default_factory=dict)

    def submission(self) -> Submission:
        return Submission(path=self.path, content=self.content)


class Verdict(Incoming):
    """Shikigami's answer: what the file would add to main's problems."""

    commit: str
    problems: list[str]


class Submitted(Value):
    """A proposal that went in: the PR waiting for Mario's approval in the atlas (D93)."""

    pr: int | None  # None when Gitea's push answer named no PR (shouldn't happen; the push itself went through)
    url: str | None
    title: str
    checked_against: str  # the requests main commit Shikigami checked it on


class Rejected(Value):
    problems: list[str]
    checked_against: str


class Pending(Value):
    """An open PR on Meru/requests - a request waiting for approval."""

    pr: int
    title: str
    url: str
    author: str  # the Gitea user that opened it (janus for everything through here, else a person)
    requested_by: str | None  # who asked, from Janus's Requested-By line
    head: str  # the commit an approval would be for (J2)
    files: list[str]


class OnMain(Value):
    """A request on Meru/requests main - an app or database Meru runs (or has retired)."""

    path: RequestPath
    kind: str  # app | database
    name: str
    removed: bool
