from concurrent.futures import ThreadPoolExecutor
from datetime import date
import pytest
from conftest import create, rows
from app.core.database import engine

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="Requires PostgreSQL row locks.")


def test_concurrent_payments_cannot_overpay(client, school):
    h, student, term = school["headers"], school["student"], school["term"]
    create(
        client,
        h,
        "charges",
        student_id=student["id"],
        term_id=term["id"],
        description="Concurrent payment test",
        amount="1000",
    )

    def pay(key):
        return client.post(
            "/api/v1/payments",
            headers=h,
            json={
                "student_id": student["id"],
                "term_id": term["id"],
                "amount": "700",
                "payment_date": str(date.today()),
                "method": "CASH",
                "idempotency_key": key,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pay, ["concurrent-payment-1", "concurrent-payment-2"]))
    assert sorted(r.status_code for r in results) == [201, 422]
    assert len(rows(client, h, "payments")) == 1
    assert float(client.get(f"/api/v1/students/{student['id']}/ledger", headers=h).json()["balance"]) == 300


def test_concurrent_attendance_deduplicates(client, school):
    h, student, term = school["headers"], school["student"], school["term"]
    token = client.get(f"/api/v1/students/{student['id']}/qr", headers=h).json()["token"]

    def scan(_):
        return client.post("/api/v1/attendance/scan", headers=h, json={"token": token, "term_id": term["id"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(scan, range(2)))
    assert all(r.status_code == 200 for r in results)
    assert sorted(r.json()["duplicate"] for r in results) == [False, True]
    assert len(rows(client, h, "attendance")) == 1
    assert len(rows(client, h, "notifications")) == 2
