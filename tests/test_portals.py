from datetime import timedelta, date
from sqlalchemy import select
from conftest import create, rows, onboard
from app import models as m
from app.accounts import temporary_password
from app.core.database import SessionLocal
from app.core.models import now
from app.worker import drain
from app.storage import Storage


def invite_staff(client, school, **extra):
    data = {
        "first_name": "Ada",
        "last_name": "Teacher",
        "email": "teacher.portal@example.com",
        "subjects": [{"class_id": school["class"]["id"], "subject_id": school["subject"]["id"]}],
        **extra,
    }
    response = client.post("/api/v1/accounts/staff", headers=school["headers"], json=data)
    assert response.status_code == 201, response.text
    return response.json()


def credentials(account):
    with SessionLocal() as db:
        user = db.get(m.User, account["id"])
        return temporary_password(user.credential_nonce)


def login(client, account, password):
    response = client.post("/api/v1/auth/login", json={"email": account["username"], "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def activate(client, account):
    password = credentials(account)
    headers = login(client, account, password)
    response = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": password, "password": "Portal-permanent-password-2026!"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_invitation_first_login_and_password_reset(client, school):
    account = invite_staff(client, school)["user"]
    password = credentials(account)
    assert "credential_nonce" not in account and "password_hash" not in account
    with SessionLocal() as db:
        job = db.scalar(select(m.Notification).where(m.Notification.event_key.like("invite:%")))
        assert password not in job.body
        key = f"{job.school_id}/outbox/{job.id}.eml"
    drain()
    assert password.encode() in Storage().get(key)
    assert all(password not in str(r) for r in rows(client, school["headers"], "notifications"))
    headers = login(client, account, password)
    for path in ["/dashboard", "/teacher/dashboard", "/records/students", "/school/logo", "/profile"]:
        response = client.get("/api/v1" + path, headers=headers)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "ACCOUNT_MUST_CHANGE_PASSWORD"
    assert client.get("/api/v1/auth/me", headers=headers).json()["user"]["must_change_password"]
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=headers,
        json={"current_password": password, "password": "My-new-secure-password-2026!"},
    )
    assert changed.status_code == 200
    assert client.get("/api/v1/teacher/dashboard", headers=headers).status_code == 401
    assert (
        client.post("/api/v1/auth/login", json={"email": account["username"], "password": password}).status_code == 401
    )
    fresh = {"Authorization": "Bearer " + changed.json()["access_token"]}
    assert client.get("/api/v1/teacher/dashboard", headers=fresh).status_code == 200
    assert client.post(f"/api/v1/accounts/{account['id']}/invite", headers=school["headers"]).status_code == 202
    assert client.get("/api/v1/teacher/dashboard", headers=fresh).status_code == 401
    with SessionLocal.begin() as db:
        user = db.get(m.User, account["id"])
        expired_password = temporary_password(user.credential_nonce)
        user.temporary_password_expires_at = now() - timedelta(hours=1)
    response = client.post("/api/v1/auth/login", json={"email": account["username"], "password": expired_password})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TEMPORARY_PASSWORD"


def test_teacher_scope_multiclass_results_qr_and_profile(client, school):
    h = school["headers"]
    second = create(client, h, "classes", level_id=school["level"]["id"], name="B")
    unrelated = create(client, h, "classes", level_id=school["level"]["id"], name="C")
    student2 = create(client, h, "students", first_name="Second", last_name="Child", class_id=second["id"])
    stranger = create(client, h, "students", first_name="Other", last_name="Child", class_id=unrelated["id"])
    account = invite_staff(
        client,
        school,
        class_teacher_ids=[school["class"]["id"], second["id"]],
        subjects=[
            {"class_id": c, "subject_id": school["subject"]["id"]} for c in [school["class"]["id"], second["id"]]
        ],
    )["user"]
    teacher = activate(client, account)
    assigned = client.get("/api/v1/teacher/classes", headers=teacher).json()
    assert len(assigned) == 2 and all(c["is_class_teacher"] for c in assigned)
    assert len(client.get("/api/v1/teacher/students", headers=teacher).json()["items"]) == 2
    assert client.get(f"/api/v1/teacher/students/{stranger['id']}", headers=teacher).status_code == 403
    profile = client.get(f"/api/v1/teacher/students/{school['student']['id']}", headers=teacher).json()
    assert len(profile["guardians"]) == 1
    assert client.get(f"/api/v1/students/{student2['id']}/ledger", headers=teacher).status_code == 403
    qr = client.get(f"/api/v1/students/{student2['id']}/qr", headers=h).json()["token"]
    for expected in [False, True]:
        scan = client.post(
            "/api/v1/attendance/scan", headers=teacher, json={"token": qr, "term_id": school["term"]["id"]}
        )
        assert scan.status_code == 200 and scan.json()["duplicate"] is expected
    other_qr = client.get(f"/api/v1/students/{stranger['id']}/qr", headers=h).json()["token"]
    assert (
        client.post(
            "/api/v1/attendance/scan", headers=teacher, json={"token": other_qr, "term_id": school["term"]["id"]}
        ).status_code
        == 403
    )
    components = rows(client, h, "assessments")
    result_data = {
        "student_id": student2["id"],
        "subject_id": school["subject"]["id"],
        "term_id": school["term"]["id"],
        "scores": [{"component_id": c["id"], "score": c["max_score"]} for c in components],
    }
    saved = client.post("/api/v1/results", headers=teacher, json=result_data)
    assert saved.status_code == 201, saved.text
    assert float(saved.json()["total"]) == 100
    result_id = saved.json()["id"]
    for action, actor in [("submit", teacher), ("review", h), ("approve", h), ("publish", h)]:
        response = client.post(f"/api/v1/results/{result_id}/transition", headers=actor, json={"action": action})
        assert response.status_code == 200, response.text
    assert client.post("/api/v1/results", headers=teacher, json=result_data).status_code == 409
    physics = create(client, h, "subjects", name="Physics", code="PHY")
    assert (
        client.post("/api/v1/results", headers=teacher, json={**result_data, "subject_id": physics["id"]}).status_code
        == 403
    )
    assert client.patch("/api/v1/profile", headers=teacher, json={"role": "SCHOOL_ADMIN"}).status_code == 422
    assert (
        client.patch("/api/v1/profile", headers=teacher, json={"phone": "123", "address": "My address"}).status_code
        == 200
    )
    assert client.get("/api/v1/teacher/performance", headers=teacher).status_code == 200
    insight = client.get("/api/v1/teacher/insights", headers=teacher)
    assert insight.status_code == 200, insight.text
    assert insight.json()["top_students"][0]["id"] == student2["id"]
    assert not insight.json()["needs_support"]
    assert client.get("/api/v1/teacher/students?academic_status=PASSING", headers=teacher).json()["total"] == 1
    assert client.get("/api/v1/teacher/students?attendance_status=PRESENT", headers=teacher).json()["total"] == 1


def test_subject_teacher_cannot_read_other_subject_or_full_reports(client, school):
    h = school["headers"]
    teacher = activate(client, invite_staff(client, school)["user"])
    physics = create(client, h, "subjects", name="Physics", code="PHY")
    components = rows(client, h, "assessments")
    payload = {
        "student_id": school["student"]["id"],
        "subject_id": physics["id"],
        "term_id": school["term"]["id"],
        "scores": [{"component_id": c["id"], "score": 0} for c in components],
    }
    result = client.post("/api/v1/results", headers=h, json=payload).json()
    assert not rows(client, teacher, "results")
    assert client.get(f"/api/v1/results/{result['id']}/scores", headers=teacher).status_code == 404
    detail = client.get(f"/api/v1/teacher/students/{school['student']['id']}", headers=teacher).json()
    assert detail["guardians"] == [] and "address" not in detail["student"]
    assert (
        client.post(
            "/api/v1/results/report-cards/generate",
            headers=teacher,
            json={"student_id": school["student"]["id"], "term_id": school["term"]["id"]},
        ).status_code
        == 403
    )
    # A different teacher's lesson in the same class is excluded.
    other = create(
        client,
        h,
        "users",
        name="Other Teacher",
        email="other.teacher@example.com",
        password="Other-teacher-password",
        role="TEACHER",
    )
    create(
        client,
        h,
        "timetable",
        class_id=school["class"]["id"],
        subject_id=physics["id"],
        teacher_id=other["id"],
        room="2",
        weekday=0,
        start_time="08:00",
        end_time="09:00",
        published=True,
    )
    assert not rows(client, teacher, "timetable")


def test_parent_multiple_children_tenant_isolation_notifications_and_finance(client, school):
    h = school["headers"]
    second = create(client, h, "students", first_name="Second", last_name="Child", class_id=school["class"]["id"])
    stranger = create(client, h, "students", first_name="Unrelated", last_name="Child", class_id=school["class"]["id"])
    response = client.post(
        "/api/v1/accounts/parents",
        headers=h,
        json={
            "name": "Parent Person",
            "email": "parent.portal@example.com",
            "children": [{"student_id": s} for s in [school["student"]["id"], second["id"]]],
        },
    )
    assert response.status_code == 201, response.text
    account = response.json()["user"]
    restricted = login(client, account, credentials(account))
    assert client.get("/api/v1/parent/children", headers=restricted).status_code == 403
    parent = activate(client, account)
    assert len(client.get("/api/v1/parent/children", headers=parent).json()) == 2
    for resource in ["dashboard", "attendance", "results", "report-cards", "fees", "payments", "receipts", "timetable"]:
        assert client.get(f"/api/v1/parent/children/{second['id']}/{resource}", headers=parent).status_code == 200
        assert client.get(f"/api/v1/parent/children/{stranger['id']}/{resource}", headers=parent).status_code == 404
    other_h = onboard(client, "other")
    other_student = create(client, other_h, "students", first_name="Other", last_name="School")
    assert client.get(f"/api/v1/parent/children/{other_student['id']}/dashboard", headers=parent).status_code == 404
    assert (
        client.post(
            "/api/v1/attendance/mark",
            headers=parent,
            json={"student_id": second["id"], "term_id": school["term"]["id"]},
        ).status_code
        == 403
    )
    create(
        client,
        h,
        "charges",
        student_id=second["id"],
        term_id=school["term"]["id"],
        description="Tuition",
        amount=1000,
        discount=100,
    )
    payment = client.post(
        "/api/v1/payments",
        headers=h,
        json={
            "student_id": second["id"],
            "term_id": school["term"]["id"],
            "amount": 500,
            "payment_date": date.today().isoformat(),
            "method": "CASH",
            "idempotency_key": "parent-test-payment",
        },
    )
    assert payment.status_code == 201, payment.text
    fees = client.get(f"/api/v1/parent/children/{second['id']}/fees", headers=parent).json()
    assert float(fees["balance"]) == 400 and len(fees["items"]) == 1
    drain()
    receipts = client.get(f"/api/v1/parent/children/{second['id']}/receipts", headers=parent).json()["items"]
    assert receipts
    assert client.get(f"/api/v1/documents/{receipts[0]['id']}/download", headers=parent).status_code == 200
    notifications = client.get("/api/v1/portal/notifications", headers=parent).json()["items"]
    assert notifications
    assert client.post(f"/api/v1/portal/notifications/{notifications[0]['id']}/read", headers=parent).json()["read_at"]
    create(client, h, "announcements", title="Staff confidential", body="Staff only", audience="STAFF")
    assert not client.get("/api/v1/portal/announcements", headers=parent).json()["items"]
    create(
        client,
        h,
        "calendar",
        title="PTA meeting",
        start_date=date.today().isoformat(),
        end_date=date.today().isoformat(),
        audience="PARENTS",
    )
    assert client.get("/api/v1/portal/calendar", headers=parent).json()["items"][0]["title"] == "PTA meeting"
    link = next(
        r
        for r in rows(client, h, "student-parents")
        if r["student_id"] == second["id"] and r["parent_id"] == response.json()["parent"]["id"]
    )
    assert client.post(f"/api/v1/accounts/relationships/{link['id']}/unlink", headers=h).status_code == 200
    assert client.get(f"/api/v1/documents/{receipts[0]['id']}/download", headers=parent).status_code == 404
    assert not client.get("/api/v1/portal/notifications", headers=parent).json()["items"]

    assert (
        client.patch(
            "/api/v1/profile", headers=parent, json={"notification_preferences": {"announcements": False}}
        ).status_code
        == 200
    )
    create(client, h, "announcements", title="Optional update", body="School news", audience="PARENTS")
    assert not client.get("/api/v1/portal/notifications", headers=parent).json()["items"]
    assert (
        client.patch("/api/v1/school", headers=h, json={"mandatory_notifications": {"announcements": True}}).status_code
        == 200
    )
    create(client, h, "announcements", title="Required update", body="School news", audience="PARENTS")
    notices = client.get("/api/v1/portal/notifications", headers=parent).json()["items"]
    assert [notice["title"] for notice in notices] == ["Required update"]


def test_school_palette_validation_role_position_and_deactivation(client, school):
    h = school["headers"]
    palette = {"primary_color": "#000000", "secondary_color": "#ffffff", "accent_color": "#abcdef"}
    assert client.patch("/api/v1/school", headers=h, json=palette).status_code == 200
    settings = client.get("/api/v1/auth/me", headers=h).json()["school"]
    assert all(settings[k] == v for k, v in palette.items())
    assert client.patch("/api/v1/school", headers=h, json={"secondary_color": "red; invalid"}).status_code == 422
    admin = invite_staff(client, school, role="SCHOOL_ADMIN", job_title="Headmistress", subjects=[])["user"]
    account_h = activate(client, admin)
    assert client.get("/api/v1/auth/me", headers=account_h).json()["user"]["role"] == "SCHOOL_ADMIN"
    assert client.patch(f"/api/v1/records/users/{admin['id']}", headers=h, json={"active": False}).status_code == 200
    assert client.get("/api/v1/dashboard", headers=account_h).status_code == 401
    other = invite_staff(client, school, email="second.teacher@example.com")["user"]
    assert other["username"] != admin["username"]
