from fastapi.testclient import TestClient


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "rajosa-api"
    assert body["adapter"] == "mock"
    assert body["execution_runner"] == "off"
    # Injected Memory store: recovery does not run at lifespan.
    assert body["startup"]["recovery_completed"] is False
    assert body["startup"]["recovery"] is None
    assert body["startup"]["reconciliation"] is None
