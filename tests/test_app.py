from fastapi.testclient import TestClient

from janus.app import app


def test_heartbeat():
    response = TestClient(app).get("/heartbeat")
    assert response.status_code == 200
    assert response.text == "ok"
