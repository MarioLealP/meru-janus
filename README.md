# janus

Meru's front door - takes requests from the atlas and agents, hands approvals to Xolotl

A Meru app, made from the [Meru/nuwa-python](https://git.clockmost.com/Meru/nuwa-python) template: a FastAPI service
with one endpoint, `GET /heartbeat` -> `ok`, to grow from.

## Run locally

```sh
uv sync
uv run uvicorn --app-dir src janus.app:app --port 8080
curl localhost:8080/heartbeat
```

## Development

```sh
scripts/check.sh   # lint, format, types, lockfile, tests - exactly what CI runs
```

## Deploy

`deploy/meru.yaml` makes this repo a Meru app: [Meru/meru](https://git.clockmost.com/Meru/meru) finds it and deploys it
into namespace `janus`. Shikigami adds it once the app's fence, signing key and build credentials exist,
from `deploy/meru.yaml.in` (the template's chart release and check image) with the owner from the request.
`deploy/values.yaml` is the app's whole deployment, rendered with the `nuwa-app` chart from
[Meru/nuwa](https://git.clockmost.com/Meru/nuwa). Merging to `main` runs the checks, builds the image, signs it and rolls
it out; builds run in namespace `janus-build` on the runner from [Meru/sindri](https://git.clockmost.com/Meru/sindri).
No secrets live in this repo: they come from OpenBao, through the External Secrets Operator or the build's own login.

## License

MIT - see `LICENSE`.
