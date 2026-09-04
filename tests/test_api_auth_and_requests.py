from fastapi.testclient import TestClient

from aegisforge.app import create_app
from aegisforge.config import Settings
from aegisforge.db.base import Base
from aegisforge.db.session import get_engine


def test_user_registration_login_and_request_creation() -> None:
    settings = Settings(
        app_name="aegisforge-test",
        environment="test",
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379/0",
        secret_key="test-secret-key",
        access_token_expire_minutes=60,
    )

    engine = get_engine(settings.database_url)
    Base.metadata.create_all(bind=engine)

    app = create_app(settings=settings)
    client = TestClient(app)

    register_response = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "password": "StrongPass123!", "full_name": "Alice"},
    )
    assert register_response.status_code == 201, register_response.text

    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "StrongPass123!"},
    )
    assert login_response.status_code == 200, login_response.text
    token = login_response.json()["access_token"]
    assert token

    create_request = client.post(
        "/api/v1/requests",
        json={"intent": "Find recent guidance on support escalation policies"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert create_request.status_code == 201, create_request.text
    payload = create_request.json()
    assert payload["intent"] == "Find recent guidance on support escalation policies"
    assert payload["status"] in {"created", "planning"}

    detail_response = client.get(
        f"/api/v1/requests/{payload['id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert detail_response.status_code == 200, detail_response.text
    assert detail_response.json()["id"] == payload["id"]
