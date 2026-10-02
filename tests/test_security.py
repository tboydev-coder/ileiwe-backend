from datetime import date, timedelta
from io import BytesIO
import re
from sqlalchemy import select
from PIL import Image
from conftest import create, rows, onboard
from app.core.database import SessionLocal
from app.models import Notification
from app.core.config import Settings
import pytest


def test_password_reset_is_single_use_and_content_is_hidden(client, school):
    h = school["headers"]
    response = client.post("/api/v1/auth/forgot-password", json={"email": "owner.one@example.com"})
    assert response.status_code == 200
    listed = rows(client, h, "notifications")
    assert listed[0]["body"] == "Password reset email (content hidden)"
    with SessionLocal() as db:
        notification = db.scalar(select(Notification).where(Notification.event_key.like("reset:%")))
        token = re.search(r"token=([^\s]+)", notification.body)[1]
    data = {"token": token, "password": "New-password-with-2026"}
    assert client.post("/api/v1/auth/reset-password", json=data).status_code == 200
    assert client.post("/api/v1/auth/reset-password", json=data).status_code == 400
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "owner.one@example.com", "password": data["password"]}
        ).status_code
        == 200
    )


def test_student_identity_is_immutable_and_unique(client, school):
    h = school["headers"]
    sid = school["student"]["id"]
    assert (
        client.patch(f"/api/v1/records/students/{sid}", headers=h, json={"student_code": "FORGED"}).status_code == 422
    )
    assert (
        client.post(
            "/api/v1/records/students",
            headers=h,
            json={"first_name": "Other", "last_name": "Child", "admission_no": "ADM-001"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/records/students",
            headers=h,
            json={"first_name": "Other", "last_name": "Child", "date_of_birth": str(date.today() + timedelta(days=1))},
        ).status_code
        == 422
    )


def test_branding_upload_and_settings(client, school):
    h = school["headers"]
    response = client.patch(
        "/api/v1/school",
        headers=h,
        json={"primary_color": "#ffcc00", "name": "Updated Academy", "registration_number": ""},
    )
    assert response.status_code == 200, response.text
    assert response.json()["primary_color"] == "#ffcc00"
    assert client.patch("/api/v1/school", headers=h, json={"primary_color": "red"}).status_code == 422
    image = BytesIO()
    Image.new("RGB", (20, 20), "green").save(image, "PNG")
    assert (
        client.post(
            "/api/v1/school/logo", headers=h, files={"file": ("logo.png", image.getvalue(), "image/png")}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/school/logo", headers=h).content.startswith(b"\x89PNG")
    assert (
        client.post(
            "/api/v1/school/logo",
            headers=h,
            files={"file": ("script.svg", b"<svg><script>alert(1)</script></svg>", "image/svg+xml")},
        ).status_code
        == 422
    )
    photo_path = "/api/v1/students/" + school["student"]["id"] + "/photo"
    assert (
        client.post(photo_path, headers=h, files={"file": ("photo.png", image.getvalue(), "image/png")}).status_code
        == 200
    )
    assert client.get(photo_path, headers=h).headers["content-type"] == "image/jpeg"
    other = onboard(client, "two")
    assert client.get(photo_path, headers=other).status_code == 404


def test_corrections_bulk_atomicity_and_tenant_access(client, school):
    h = school["headers"]
    data = {"student_id": school["student"]["id"], "term_id": school["term"]["id"], "status": "PRESENT"}
    response = client.post(
        "/api/v1/attendance/bulk", headers=h, json={"records": [data, {**data, "student_id": "missing"}]}
    )
    assert response.status_code == 404
    assert rows(client, h, "attendance") == []
    record = client.post("/api/v1/attendance/mark", headers=h, json=data).json()["record"]
    other = onboard(client, "two")
    assert (
        client.patch(
            "/api/v1/attendance/" + record["id"],
            headers=other,
            json={"status": "ABSENT", "reason": "Mistaken attendance"},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/api/v1/attendance/" + record["id"], headers=h, json={"status": "LATE", "reason": "Arrived after register"}
        ).status_code
        == 200
    )
    assert any(a["action"] == "attendance.corrected" for a in rows(client, h, "audit"))


def test_role_escalation_blocked_and_deactivation_revokes(client, school):
    h = school["headers"]
    assert (
        client.post(
            "/api/v1/records/users",
            headers=h,
            json={
                "name": "Attacker",
                "email": "attacker@example.com",
                "password": "Long-password-2026",
                "role": "PLATFORM_SUPER_ADMIN",
            },
        ).status_code
        == 422
    )
    teacher = create(
        client,
        h,
        "users",
        name="Teacher",
        email="teacher@example.com",
        password="Teacher-password-2026",
        role="TEACHER",
    )
    staff = create(client, h, "staff", name="Teacher", user_id=teacher["id"])
    tokens = client.post(
        "/api/v1/auth/login", json={"email": teacher["email"], "password": "Teacher-password-2026"}
    ).json()
    th = {"Authorization": "Bearer " + tokens["access_token"]}
    assert client.get("/api/v1/platform/schools", headers=th).status_code == 403
    assert client.patch("/api/v1/records/staff/" + staff["id"], headers=h, json={"active": False}).status_code == 200
    assert client.get("/api/v1/auth/me", headers=th).status_code == 401
    assert client.patch("/api/v1/records/staff/" + staff["id"], headers=h, json={"active": True}).status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"token": tokens["refresh_token"]}).status_code == 401


def test_production_configuration_fails_closed():
    with pytest.raises(ValueError):
        Settings(app_env="production", database_url="sqlite:///test.db")


def test_message_preview_respects_preferences_and_tenants(client, school):
    h = school["headers"]
    message = {"channel": "EMAIL", "body": "Hello {{parent_name}} from {{school_name}}", "audience": "school"}
    preview = client.post("/api/v1/messages/preview", headers=h, json=message)
    assert preview.status_code == 200 and preview.json()["recipient_count"] == 1
    assert "Ngozi Okafor" in preview.json()["preview"]
    assert client.post("/api/v1/messages/send", headers=h, json=message).json()["queued"] == 1
    other = onboard(client, "two")
    assert (
        client.post(
            "/api/v1/messages/preview",
            headers=other,
            json={**message, "audience": "parents", "parent_ids": [school["parent"]["id"]]},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            "/api/v1/records/parents/" + school["parent"]["id"], headers=h, json={"notify_email": False}
        ).status_code
        == 200
    )
    assert client.post("/api/v1/messages/preview", headers=h, json=message).json()["recipient_count"] == 0
