"""janus: Meru's front door - takes requests from the atlas and agents, hands approvals to Xolotl"""

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

app = FastAPI(title="janus")


@app.get("/heartbeat", response_class=PlainTextResponse)
def heartbeat() -> str:
    """Meru's liveness and readiness probes (nuwa-app's heartbeat.path) - add a /ready once there are dependencies."""
    return "ok"
