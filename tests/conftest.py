import os
import tempfile
from pathlib import Path
import pytest

TEST_ROOT = Path(tempfile.mkdtemp(prefix="ile-iwe-tests-"))
os.environ["DATABASE_URL"] = os.environ.get(
    "ILE_TEST_DATABASE_URL", "sqlite:///" + str(TEST_ROOT / "test.db").replace("\\", "/")
)
os.environ["APP_ENV"] = "test"
os.environ["LOCAL_STORAGE_PATH"] = str(TEST_ROOT / "storage")
os.environ["EMAIL_ENABLED"] = "false"
os.environ["REQUIRE_SCHOOL_APPROVAL"] = "false"
os.environ["ALLOW_SCHOOL_REGISTRATION"] = "true"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app, limits  # noqa: E402
from app.core.database import engine, Base, migrate  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def schema():
    migrate()


@pytest.fixture
def client():
    limits.clear()
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    with TestClient(app) as client:
        yield client


def onboard(client, suffix="one"):
    response = client.post(
        "/api/v1/auth/onboard",
        json={
            "name": "School Owner",
            "email": f"owner.{suffix}@example.com",
            "password": "Testing-password-2026!",
            "school_name": f"School {suffix}",
            "school_email": f"office.{suffix}@example.com",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def create(client, headers, resource, **data):
    response = client.post("/api/v1/records/" + resource, headers=headers, json=data)
    assert response.status_code == 201, response.text
    return response.json()


def rows(client, headers, resource):
    response = client.get("/api/v1/records/" + resource + "?page_size=100", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


@pytest.fixture
def school(client):
    headers = onboard(client)
    term = rows(client, headers, "terms")[0]
    level = create(client, headers, "class-levels", name="JS1")
    arm = create(client, headers, "classes", level_id=level["id"], name="A")
    student = create(
        client, headers, "students", first_name="Ada", last_name="Okafor", class_id=arm["id"], admission_no="ADM-001"
    )
    subject = create(client, headers, "subjects", name="Mathematics", code="MTH")
    parent = create(client, headers, "parents", name="Ngozi Okafor", email="ngozi@example.com", phone="+2348000000000")
    create(
        client,
        headers,
        "student-parents",
        student_id=student["id"],
        parent_id=parent["id"],
        relationship="Mother",
        primary_contact=True,
    )
    return {
        "headers": headers,
        "term": term,
        "level": level,
        "class": arm,
        "student": student,
        "subject": subject,
        "parent": parent,
    }
