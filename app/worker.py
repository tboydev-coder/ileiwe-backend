"""Durable SQL outbox worker backed by PostgreSQL."""

import argparse
import logging
import signal
import time
from datetime import timedelta, timezone
from types import SimpleNamespace
from sqlalchemy import select
from .core.config import get_settings
from .core.database import SessionLocal, migrate
from .core.models import now
from .models import Document, Notification, Student, Payment, User, School
from .documents import generate, MIMES
from .notifications import queue_parents
from .providers import deliver
from .storage import Storage

logger = logging.getLogger("ile-iwe.worker")


def process_one(model):
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(model)
            .where(model.status == "QUEUED", model.available_at <= now())
            .order_by(model.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return False
        job.attempts += 1
        try:
            if model is Document:
                job.storage_key = generate(db, job)
                job.status = "READY"
                student = db.get(Student, job.student_id)
                queue_parents(
                    db,
                    SimpleNamespace(school_id=job.school_id),
                    student,
                    "document_ready",
                    f"document:{job.id}",
                    "The "
                    + job.kind.lower().replace("_", " ")
                    + " for {{student_name}} is ready in your school portal.",
                    document_id=job.id,
                    send_external=job.email_parents,
                )
            else:
                attachment = None
                invitation_user = None
                delivery_job = job
                if job.event_key.startswith("invite:"):
                    from .accounts import temporary_password
                    from .core.security import audit

                    _, account_id, version = job.event_key.split(":")
                    invitation_user = db.scalar(
                        select(User).where(User.id == account_id, User.school_id == job.school_id).with_for_update()
                    )
                    if (
                        not invitation_user
                        or not invitation_user.active
                        or not invitation_user.must_change_password
                        or not invitation_user.credential_nonce
                        or str(invitation_user.token_version) != version
                        or invitation_user.temporary_password_expires_at.replace(tzinfo=timezone.utc) <= now()
                    ):
                        job.status = "CANCELLED"
                        return True
                    school = db.get(School, job.school_id)
                    delivery_job = SimpleNamespace(
                        id=job.id,
                        school_id=job.school_id,
                        channel="EMAIL",
                        recipient=job.recipient,
                        subject=f"Welcome to {school.name}",
                        body=f"Hello {invitation_user.name},\n\nSchool: {school.name}\nUsername: {invitation_user.username}\nTemporary password: {temporary_password(invitation_user.credential_nonce)}\nSign in: {get_settings().frontend_url}/login\n\nYou must change your password before accessing your dashboard. This password expires at {invitation_user.temporary_password_expires_at.isoformat()} UTC.\nSupport: {school.email} {school.phone}",
                    )
                    if school.logo_key:
                        delivery_job.school_logo = Storage().get(school.logo_key)
                if job.document_id:
                    doc = db.get(Document, job.document_id)
                    if doc.status != "READY" or (doc.payment_id and db.get(Payment, doc.payment_id).reversed):
                        job.status = "CANCELLED"
                        return True
                    attachment = (
                        Storage().get(doc.storage_key),
                        f"ile-iwe-{doc.kind.lower()}.{doc.format}",
                        MIMES[doc.format],
                    )
                job.status, job.provider_id = deliver(delivery_job, attachment)
                if invitation_user:
                    invitation_user.credential_nonce = None
                    audit(
                        db,
                        invitation_user,
                        "CREDENTIAL_INVITATION_SENT",
                        "users",
                        invitation_user.id,
                        {"delivery_status": job.status},
                    )
            job.last_error = None
        except Exception as exc:
            # Exception messages from providers can contain request URLs or credentials.
            job.last_error = f"{type(exc).__name__}: delivery or generation failed. Check provider configuration."
            job.status = "FAILED" if job.attempts >= 5 else "QUEUED"
            job.available_at = now() + timedelta(seconds=min(3600, 30 * 2**job.attempts))
            logger.warning("job_failed", extra={"job_id": job.id, "school_id": job.school_id, "attempts": job.attempts})
        return True


def drain(limit=100):
    from .notifications import queue_calendar_reminders

    with SessionLocal.begin() as db:
        # One short transaction prevents duplicate reminder jobs across workers.
        from .models import Role

        db.scalar(select(Role).where(Role.name == "PARENT").with_for_update())
        queue_calendar_reminders(db)
    processed = 0
    for _ in range(limit):
        found = process_one(Document)
        found = process_one(Notification) or found
        if not found:
            break
        processed += 1
    return processed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    migrate()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while running:
        drain()
        if args.once:
            break
        time.sleep(3)


if __name__ == "__main__":
    main()
