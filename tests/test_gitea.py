import httpx2

from janus.gitea import on_main, pending


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


async def test_on_main_lists_apps_and_databases_with_their_removed_state():
    files = {
        "apps/kitsune.yaml": "spec:\n  owner: mario\n",
        "apps/golem.yaml": "spec:\n  owner: mario\n  removed: true\n",
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.headers["authorization"] == "token tok"
        assert request.url.params["ref"] == "main"
        tail = request.url.path.removeprefix("/api/v1/repos/Meru/requests")
        if tail == "/contents/apps":
            listing = [{"name": "kitsune.yaml", "path": "apps/kitsune.yaml", "type": "file"},
                       {"name": "golem.yaml", "path": "apps/golem.yaml", "type": "file"},
                       {"name": "README.md", "path": "apps/README.md", "type": "file"}]  # fmt: skip
            return httpx2.Response(200, json=listing)
        if tail == "/contents/databases":
            return httpx2.Response(404)
        return httpx2.Response(200, text=files[tail.removeprefix("/raw/")])

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        listed = await on_main(http, "https://git.test", "Meru", "requests", "tok")
    assert [(a.path, a.kind, a.name, a.removed) for a in listed] == [
        ("apps/golem.yaml", "app", "golem", True),
        ("apps/kitsune.yaml", "app", "kitsune", False),
    ]
