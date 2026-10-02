from datetime import date
from decimal import Decimal
import io
import zipfile
import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from conftest import onboard, create, rows
from app.core.database import SessionLocal, engine, migrate
from app.models import Student
from app.worker import drain


def test_clean_migrations_are_repeatable(client):
    migrate()
    migrate()
    assert len(inspect(engine).get_table_names()) == 36
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from app.core.database import Base

    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    assert client.get("/ready").status_code == 200


def test_full_school_workflow(client, school):
    h, student, term = school["headers"], school["student"], school["term"]
    teacher = create(
        client,
        h,
        "users",
        name="Teacher One",
        email="teacher@example.com",
        password="Teacher-password-2026",
        role="TEACHER",
    )
    create(client, h, "staff", user_id=teacher["id"], name=teacher["name"], job_title="Mathematics teacher")
    create(
        client,
        h,
        "assignments",
        teacher_id=teacher["id"],
        class_id=school["class"]["id"],
        subject_id=school["subject"]["id"],
    )
    login = client.post("/api/v1/auth/login", json={"email": teacher["email"], "password": "Teacher-password-2026"})
    assert login.status_code == 200
    th = {"Authorization": "Bearer " + login.json()["access_token"]}
    assert student["student_code"].startswith("IW-")
    qr = client.get(f"/api/v1/students/{student['id']}/qr", headers=th).json()
    marked = client.post("/api/v1/attendance/scan", headers=th, json={"token": qr["token"], "term_id": term["id"]})
    assert marked.status_code == 200, marked.text
    assert marked.json()["record"]["status"] == "PRESENT"
    assert client.post(
        "/api/v1/attendance/scan", headers=th, json={"token": qr["token"], "term_id": term["id"]}
    ).json()["duplicate"]
    assert len(rows(client, h, "attendance")) == 1
    # SMS delivery is intentionally disabled; attendance queues email only.
    notifications = rows(client, h, "notifications")
    assert len(notifications) == 1
    assert notifications[0]["channel"] == "EMAIL"
    structure = create(
        client, h, "fee-structures", name="First term fees", term_id=term["id"], class_id=school["class"]["id"]
    )
    create(client, h, "fee-items", structure_id=structure["id"], name="Tuition", amount="150000.00")
    assert client.post(f"/api/v1/fees/{structure['id']}/apply", headers=h).json()["charges_created"] == 1
    assert client.post(f"/api/v1/fees/{structure['id']}/apply", headers=h).json()["charges_created"] == 0
    payload = {
        "student_id": student["id"],
        "term_id": term["id"],
        "amount": "50000.00",
        "payment_date": str(date.today()),
        "method": "BANK_TRANSFER",
        "reference": "BANK-001",
        "idempotency_key": "payment-request-001",
    }
    payment = client.post("/api/v1/payments", headers=h, json=payload)
    assert payment.status_code == 201, payment.text
    assert Decimal(payment.json()["new_balance"]) == Decimal("100000")
    assert client.post("/api/v1/payments", headers=h, json=payload).json()["id"] == payment.json()["id"]
    assert len(rows(client, h, "payments")) == 1
    components = rows(client, h, "assessments")
    result = client.post(
        "/api/v1/results",
        headers=th,
        json={
            "student_id": student["id"],
            "term_id": term["id"],
            "subject_id": school["subject"]["id"],
            "scores": [
                {"component_id": c["id"], "score": str(Decimal(c["max_score"]) * Decimal(".8"))} for c in components
            ],
        },
    )
    assert result.status_code == 201, result.text
    result = result.json()
    assert Decimal(result["total"]) == Decimal("80") and result["grade"] == "A"
    for action, headers in [("submit", th), ("approve", h), ("publish", h)]:
        response = client.post(f"/api/v1/results/{result['id']}/transition", headers=headers, json={"action": action})
        assert response.status_code == 200, response.text
    documents = []
    for fmt in ["pdf", "docx"]:
        response = client.post(
            "/api/v1/results/report-cards/generate",
            headers=h,
            json={"student_id": student["id"], "term_id": term["id"], "format": fmt, "email_parents": True},
        )
        assert response.status_code == 202, response.text
        documents.append(response.json())
    drain()
    all_docs = rows(client, h, "documents")
    assert len(all_docs) == 3 and all(d["status"] == "READY" for d in all_docs), all_docs
    for doc in all_docs:
        content = client.get(f"/api/v1/documents/{doc['id']}/download", headers=h)
        assert content.status_code == 200
        if doc["format"] == "pdf":
            assert content.content.startswith(b"%PDF")
        else:
            assert "word/document.xml" in zipfile.ZipFile(io.BytesIO(content.content)).namelist()
    assert all(n["status"] == "SIMULATED" for n in rows(client, h, "notifications"))
    assert any(a["action"] == "results.publish" for a in rows(client, h, "audit"))
    assert client.get("/api/v1/dashboard", headers=h).json()["stats"]["present"] == 1


def test_tenant_isolation_and_database_constraints(client, school):
    other = onboard(client, "two")
    sid = school["student"]["id"]
    for path in [f"/records/students/{sid}", f"/students/{sid}/ledger", f"/students/{sid}/qr"]:
        assert client.get("/api/v1" + path, headers=other).status_code == 404
    assert rows(client, other, "students") == []
    response = client.post(
        "/api/v1/records/students",
        headers=other,
        json={"first_name": "Other", "last_name": "Child", "class_id": school["class"]["id"]},
    )
    assert response.status_code == 404
    response = client.post(
        "/api/v1/records/students",
        headers=school["headers"],
        json={"first_name": "Other", "last_name": "Child", "school_id": "forged"},
    )
    assert response.status_code == 422
    other_school_id = client.get("/api/v1/auth/me", headers=other).json()["school"]["id"]
    with SessionLocal() as db:
        db.add(
            Student(
                school_id=other_school_id,
                student_code="FOREIGN",
                first_name="Test",
                last_name="Test",
                class_id=school["class"]["id"],
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_auth_refresh_logout_and_permissions(client, school):
    h = school["headers"]
    assert client.get("/api/v1/records/students").status_code == 401
    assert (
        client.post("/api/v1/auth/login", json={"email": "owner.one@example.com", "password": "wrong"}).status_code
        == 401
    )
    tokens = client.post(
        "/api/v1/auth/login", json={"email": "owner.one@example.com", "password": "Testing-password-2026!"}
    ).json()
    refresh = client.post("/api/v1/auth/refresh", json={"token": tokens["refresh_token"]})
    assert refresh.status_code == 200
    assert client.post("/api/v1/auth/refresh", json={"token": tokens["refresh_token"]}).status_code == 401
    assert client.post("/api/v1/auth/logout", headers=h).status_code == 200
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401
    assert client.post("/api/v1/auth/refresh", json={"token": refresh.json()["refresh_token"]}).status_code == 401


def test_teacher_assignment_qr_rotation_and_result_lock(client, school):
    h = school["headers"]
    teacher = create(
        client,
        h,
        "users",
        name="Unassigned Teacher",
        email="unassigned@example.com",
        password="Teacher-password-2026",
        role="TEACHER",
    )
    login = client.post(
        "/api/v1/auth/login", json={"email": teacher["email"], "password": "Teacher-password-2026"}
    ).json()
    th = {"Authorization": "Bearer " + login["access_token"]}
    assert rows(client, th, "students") == []
    assert (
        client.post(
            "/api/v1/payments",
            headers=th,
            json={
                "student_id": school["student"]["id"],
                "term_id": school["term"]["id"],
                "amount": 1,
                "payment_date": str(date.today()),
                "method": "CASH",
                "idempotency_key": "teacher-test",
            },
        ).status_code
        == 403
    )
    qr = client.get(f"/api/v1/students/{school['student']['id']}/qr", headers=h).json()["token"]
    scan_data = {"token": qr, "term_id": school["term"]["id"]}
    assert client.post("/api/v1/attendance/scan", json=scan_data).status_code == 401
    assert client.post("/api/v1/attendance/scan", headers=th, json=scan_data).status_code == 403
    client.post(f"/api/v1/students/{school['student']['id']}/qr/rotate", headers=h)
    assert client.post("/api/v1/attendance/scan", headers=h, json=scan_data).status_code == 400
    other = onboard(client, "two")
    assert client.post("/api/v1/attendance/scan", headers=other, json=scan_data).status_code == 400


def test_finance_reversal_and_overpayment(client, school):
    h, student, term = school["headers"], school["student"], school["term"]
    create(
        client,
        h,
        "charges",
        student_id=student["id"],
        term_id=term["id"],
        description="Tuition",
        amount="1000",
        discount="100",
    )
    payload = {
        "student_id": student["id"],
        "term_id": term["id"],
        "amount": "1000",
        "payment_date": str(date.today()),
        "method": "CASH",
        "idempotency_key": "payment-test-01",
    }
    assert client.post("/api/v1/payments", headers=h, json=payload).status_code == 422
    payload["amount"] = "500"
    payment = client.post("/api/v1/payments", headers=h, json=payload).json()
    assert Decimal(payment["new_balance"]) == Decimal("400")
    assert client.post("/api/v1/payments", headers=h, json={**payload, "amount": "200"}).status_code == 409
    assert (
        client.post(
            f"/api/v1/payments/{payment['id']}/reverse", headers=h, json={"reason": "Wrong bank reference"}
        ).status_code
        == 200
    )
    assert Decimal(client.get(f"/api/v1/students/{student['id']}/ledger", headers=h).json()["balance"]) == Decimal(
        "900"
    )
    assert client.patch(f"/api/v1/records/payments/{payment['id']}", headers=h, json={"amount": 1}).status_code == 405


def test_timetable_conflicts_and_grading_validation(client, school):
    h = school["headers"]
    teacher = create(
        client,
        h,
        "users",
        name="Teacher",
        email="teacher@example.com",
        password="Teacher-password-2026",
        role="TEACHER",
    )
    payload = {
        "class_id": school["class"]["id"],
        "teacher_id": teacher["id"],
        "subject_id": school["subject"]["id"],
        "room": "Room 1",
        "weekday": 0,
        "start_time": "09:00",
        "end_time": "10:00",
    }
    create(client, h, "timetable", **payload)
    assert (
        client.post("/api/v1/records/timetable", headers=h, json={**payload, "start_time": "09:30"}).status_code == 409
    )
    create(client, h, "timetable", **{**payload, "start_time": "10:00", "end_time": "11:00"})
    components = rows(client, h, "assessments")
    data = {
        "student_id": school["student"]["id"],
        "term_id": school["term"]["id"],
        "subject_id": school["subject"]["id"],
        "scores": [{"component_id": c["id"], "score": str(c["max_score"])} for c in components],
    }
    assert client.post("/api/v1/results", headers=h, json={**data, "scores": data["scores"][:1]}).status_code == 422
    result = client.post("/api/v1/results", headers=h, json=data).json()
    assert (
        client.post(f"/api/v1/results/{result['id']}/transition", headers=h, json={"action": "publish"}).status_code
        == 409
    )
    for action in ["submit", "approve", "publish"]:
        assert (
            client.post(f"/api/v1/results/{result['id']}/transition", headers=h, json={"action": action}).status_code
            == 200
        )
    assert client.post("/api/v1/results", headers=h, json=data).status_code == 409
    assert (
        client.post(f"/api/v1/results/{result['id']}/transition", headers=h, json={"action": "reopen"}).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/results/{result['id']}/transition",
            headers=h,
            json={"action": "reopen", "reason": "Correct exam score"},
        ).status_code
        == 200
    )
    assert client.post("/api/v1/results", headers=h, json=data).status_code == 201


def test_notifications_failure_does_not_rollback_attendance(client, school, monkeypatch):
    from app import worker

    response = client.post(
        "/api/v1/attendance/mark",
        headers=school["headers"],
        json={"student_id": school["student"]["id"], "term_id": school["term"]["id"], "status": "ABSENT"},
    )
    assert response.status_code == 201

    def offline(*args):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(worker, "deliver", offline)
    drain()
    notifications = rows(client, school["headers"], "notifications")
    assert all(n["attempts"] == 1 and n["status"] == "QUEUED" for n in notifications)
    assert len(rows(client, school["headers"], "attendance")) == 1


def test_parent_only_sees_linked_children(client, school):
    h = school["headers"]
    user = create(
        client, h, "users", name="Parent", email="parent@example.com", password="Parent-password-2026", role="PARENT"
    )
    assert (
        client.patch(
            f"/api/v1/records/parents/{school['parent']['id']}", headers=h, json={"user_id": user["id"]}
        ).status_code
        == 200
    )
    tokens = client.post("/api/v1/auth/login", json={"email": user["email"], "password": "Parent-password-2026"}).json()
    ph = {"Authorization": "Bearer " + tokens["access_token"]}
    assert client.get("/api/v1/records/students", headers=ph).status_code == 403
    portal = client.get("/api/v1/portal", headers=ph).json()
    assert len(portal["children"]) == 1 and portal["children"][0]["student"]["id"] == school["student"]["id"]
