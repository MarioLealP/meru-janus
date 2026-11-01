import httpx2

from janus.gitea import pending


def gitea() -> httpx2.AsyncClient:
    pulls = [
        {"number": 12, "title": "feat(request): add app golem", "html_url": "u12", "user": {"login": "janus"},
         "head": {"sha": "abc"}, "body": "Requested through Janus\n\nRequested-By: agent-claude"},
        {"number": 13, "title": "feat: my own", "html_url": "u13", "user": {"login": "MarioGLealP"},
         "head": {"sha": "def"}, "body": "Requested-By: someone-else"},
    ]  # fmt: skip

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "token tok"
        path = request.url.path
        if path.endswith("/pulls"):
            return httpx2.Response(200, json=pulls)
        number = path.split("/")[-2]
        return httpx2.Response(200, json=[{"filename": f"apps/x{number}.yaml"}])

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_requested_by_is_only_believed_on_the_bots_own_prs():
    async with gitea() as http:
        listed = await pending(http, "https://git.test", "Meru", "requests", "tok", "janus")
    assert [(p.pr, p.author, p.requested_by, p.files) for p in listed] == [
        (12, "janus", "agent-claude", ["apps/x12.yaml"]),
        (13, "MarioGLealP", None, ["apps/x13.yaml"]),  # anyone can type a Requested-By line in their own PR
    ]
