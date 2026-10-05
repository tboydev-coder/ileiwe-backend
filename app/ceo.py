"""Cross-school executive metrics and access administration."""

from datetime import date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import asc, case, desc, func, or_, select
from sqlalchemy.orm import Session

from . import models as m
from .core.database import get_db
from .core.security import current_user
from .resources import serialize

router = APIRouter(prefix="/ceo", tags=["CEO dashboard"])


def require_ceo(user):
    if user.role != "PLATFORM_SUPER_ADMIN":
        raise HTTPException(403, "CEO or system administrator access is required.")


def record_platform_audit(db, actor, school_id, action, details):
    # Platform actions are stored under the affected tenant. The actor is
    # retained in details because the tenant-consistent FK cannot cross schools.
    db.add(m.AuditLog(
        school_id=school_id,
        actor_id=None,
        action=action,
        entity="schools",
        entity_id=school_id,
        details={"actor_id": actor.id, **details},
    ))


def business_school():
    return m.School.school_type != "PLATFORM"


def count_subquery(model, *conditions):
    return select(func.count(model.id)).where(model.school_id == m.School.id, *conditions).correlate(m.School).scalar_subquery()


def school_metrics(db, start: date, end: date):
    student_count = count_subquery(m.Student, m.Student.status == "ACTIVE")
    teacher_count = select(func.count(m.User.id)).where(
        m.User.school_id == m.School.id, m.User.role == "TEACHER", m.User.active.is_(True)
    ).correlate(m.School).scalar_subquery()
    staff_count = count_subquery(m.Staff)
    parent_count = count_subquery(m.Parent)
    active_users = count_subquery(m.User, m.User.active.is_(True))
    inactive_users = count_subquery(m.User, m.User.active.is_(False))
    user_count = count_subquery(m.User)
    staff_count = count_subquery(m.Staff)
    class_count = count_subquery(m.ClassArm)
    subject_count = count_subquery(m.Subject)
    present = select(func.count(m.Attendance.id)).where(
        m.Attendance.school_id == m.School.id,
        m.Attendance.attendance_date.between(start, end),
        m.Attendance.status.in_(["PRESENT", "LATE"]),
    ).correlate(m.School).scalar_subquery()
    attendance_total = select(func.count(m.Attendance.id)).where(
        m.Attendance.school_id == m.School.id, m.Attendance.attendance_date.between(start, end)
    ).correlate(m.School).scalar_subquery()
    attendance_rate = case((attendance_total > 0, present * 100.0 / attendance_total), else_=0.0)
    collected = select(func.coalesce(func.sum(m.Payment.amount), 0)).where(
        m.Payment.school_id == m.School.id, m.Payment.reversed.is_(False), m.Payment.payment_date.between(start, end)
    ).correlate(m.School).scalar_subquery()
    charged = select(func.coalesce(func.sum(m.StudentCharge.amount - m.StudentCharge.discount), 0)).where(
        m.StudentCharge.school_id == m.School.id
    ).correlate(m.School).scalar_subquery()
    all_paid = select(func.coalesce(func.sum(m.Payment.amount), 0)).where(
        m.Payment.school_id == m.School.id, m.Payment.reversed.is_(False)
    ).correlate(m.School).scalar_subquery()
    outstanding = charged - all_paid
    session_name = select(m.AcademicSession.name).where(
        m.AcademicSession.school_id == m.School.id, m.AcademicSession.active.is_(True)
    ).order_by(m.AcademicSession.created_at.desc()).limit(1).correlate(m.School).scalar_subquery()
    term_name = select(m.Term.name).join(m.AcademicSession, m.AcademicSession.id == m.Term.session_id).where(
        m.Term.school_id == m.School.id, m.Term.active.is_(True), m.AcademicSession.active.is_(True)
    ).order_by(m.Term.created_at.desc()).limit(1).correlate(m.School).scalar_subquery()
    last_activity = select(func.max(m.AuditLog.created_at)).where(
        m.AuditLog.school_id == m.School.id
    ).correlate(m.School).scalar_subquery()
    return {
        "students": student_count,
        "teachers": teacher_count,
        "parents": parent_count,
        "active_users": active_users,
        "inactive_users": inactive_users,
        "users": user_count,
        "staff": staff_count,
        "classes": class_count,
        "subjects": subject_count,
        "attendance_rate": attendance_rate,
        "fees_collected": collected,
        "outstanding_fees": outstanding,
        "session": session_name,
        "term": term_name,
        "last_activity": last_activity,
    }


def serialize_school(row):
    data = dict(row._mapping)
    data["access_status"] = str(data.pop("status") or "").lower()
    return data


class AccessAction(BaseModel):
    confirmation: bool = False
    reason: str = Field(default="", max_length=500)


class AccountAccessAction(BaseModel):
    active: bool


def range_dates(from_date: date | None, to_date: date | None):
    end = to_date or datetime.now().date()
    start = from_date or (end - timedelta(days=30))
    if start > end:
        raise HTTPException(422, "from_date must be before to_date.")
    if (end - start).days > 366:
        raise HTTPException(422, "Date range cannot exceed one year.")
    return start, end


@router.get("/dashboard")
def dashboard(
    from_date: date | None = None,
    to_date: date | None = None,
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    require_ceo(user)
    start, end = range_dates(from_date, to_date)
    schools = select(m.School.id).where(business_school()).subquery()
    def count(model, *conditions):
        return db.scalar(select(func.count(model.id)).where(model.school_id.in_(select(schools.c.id)), *conditions)) or 0

    status_counts = dict(db.execute(
        select(m.School.status, func.count(m.School.id)).where(business_school()).group_by(m.School.status)
    ).all())
    attendance_total = db.scalar(select(func.count(m.Attendance.id)).where(
        m.Attendance.school_id.in_(select(schools.c.id)), m.Attendance.attendance_date.between(start, end)
    )) or 0
    attendance_present = db.scalar(select(func.count(m.Attendance.id)).where(
        m.Attendance.school_id.in_(select(schools.c.id)), m.Attendance.attendance_date.between(start, end),
        m.Attendance.status.in_(["PRESENT", "LATE"])
    )) or 0
    charged = db.scalar(select(func.coalesce(func.sum(m.StudentCharge.amount - m.StudentCharge.discount), 0)).where(
        m.StudentCharge.school_id.in_(select(schools.c.id))
    )) or 0
    collected = db.scalar(select(func.coalesce(func.sum(m.Payment.amount), 0)).where(
        m.Payment.school_id.in_(select(schools.c.id)), m.Payment.reversed.is_(False),
        m.Payment.payment_date.between(start, end)
    )) or 0
    all_paid = db.scalar(select(func.coalesce(func.sum(m.Payment.amount), 0)).where(
        m.Payment.school_id.in_(select(schools.c.id)), m.Payment.reversed.is_(False)
    )) or 0
    pending_notifications = db.scalar(select(func.count(m.Notification.id)).where(
        m.Notification.school_id.in_(select(schools.c.id)), m.Notification.status == "QUEUED"
    )) or 0
    failed_notifications = db.scalar(select(func.count(m.Notification.id)).where(
        m.Notification.school_id.in_(select(schools.c.id)), m.Notification.status == "FAILED"
    )) or 0
    pending_documents = db.scalar(select(func.count(m.Document.id)).where(
        m.Document.school_id.in_(select(schools.c.id)), m.Document.status == "QUEUED"
    )) or 0
    failed_documents = db.scalar(select(func.count(m.Document.id)).where(
        m.Document.school_id.in_(select(schools.c.id)), m.Document.status == "FAILED"
    )) or 0
    sessions = [r[0] for r in db.execute(select(m.AcademicSession.name).where(
        m.AcademicSession.school_id.in_(select(schools.c.id)), m.AcademicSession.active.is_(True)
    ).distinct().order_by(m.AcademicSession.name)).all()]
    terms = [r[0] for r in db.execute(select(m.Term.name).where(
        m.Term.school_id.in_(select(schools.c.id)), m.Term.active.is_(True)
    ).distinct().order_by(m.Term.name)).all()]
    activity = db.execute(
        select(m.AuditLog, m.School.name.label("school_name"))
        .join(m.School, m.School.id == m.AuditLog.school_id)
        .where(business_school())
        .order_by(m.AuditLog.created_at.desc()).limit(15)
    ).all()
    breakdown_metrics = school_metrics(db, start, end)
    breakdown = db.execute(select(
        m.School.name,
        breakdown_metrics["students"].label("students"),
        breakdown_metrics["staff"].label("staff"),
        breakdown_metrics["users"].label("users"),
        breakdown_metrics["attendance_rate"].label("attendance_rate"),
        breakdown_metrics["fees_collected"].label("fees_collected"),
    ).where(business_school()).order_by(desc(breakdown_metrics["students"])).limit(12)).all()
    return {
        "range": {"from": str(start), "to": str(end)},
        "metrics": {
            "schools": sum(status_counts.values()),
            "active_schools": status_counts.get("ACTIVE", 0),
            "suspended_schools": status_counts.get("SUSPENDED", 0),
            "revoked_schools": status_counts.get("REVOKED", 0),
            "pending_schools": status_counts.get("PENDING", 0),
            "students": count(m.Student, m.Student.status == "ACTIVE"),
            "teachers": count(m.User, m.User.role == "TEACHER", m.User.active.is_(True)),
            "staff": count(m.Staff, m.Staff.active.is_(True)),
            "parents": count(m.Parent),
            "active_users": count(m.User, m.User.active.is_(True)),
            "inactive_users": count(m.User, m.User.active.is_(False)),
            "classes": count(m.ClassArm),
            "subjects": count(m.Subject),
            "attendance_rate": round(attendance_present * 100 / attendance_total, 2) if attendance_total else 0,
            "fees_collected": collected,
            "outstanding_fees": charged - all_paid,
            "pending_notifications": pending_notifications,
            "pending_jobs": pending_notifications + pending_documents,
            "failed_jobs": failed_notifications + failed_documents,
        },
        "current_sessions": sessions,
        "current_terms": terms,
        "health": {"api": "healthy", "database": "ready", "worker": "sql-outbox"},
        "school_breakdown": [dict(row._mapping) for row in breakdown],
        "recent_activity": [
            {**serialize(log), "school_name": school_name} for log, school_name in activity
        ],
    }


@router.get("/schools")
def schools(
    search: str = "",
    status: str | None = None,
    sort: Literal["name", "students", "attendance", "fees_collected", "created_at", "last_activity"] = "name",
    direction: Literal["asc", "desc"] = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: date | None = None,
    to_date: date | None = None,
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    require_ceo(user)
    start, end = range_dates(from_date, to_date)
    metrics = school_metrics(db, start, end)
    query = select(
        m.School.id, m.School.name, m.School.email, m.School.address, m.School.school_type,
        m.School.status, m.School.created_at,
        *[expression.label(name) for name, expression in metrics.items()],
    ).where(business_school())
    if search.strip():
        term = f"%{search.strip()}%"
        query = query.where(or_(m.School.name.ilike(term), m.School.id.ilike(term)))
    if status:
        query = query.where(m.School.status == status.upper())
    sort_expr = {
        "name": m.School.name, "students": metrics["students"], "attendance": metrics["attendance_rate"],
        "fees_collected": metrics["fees_collected"], "created_at": m.School.created_at,
        "last_activity": metrics["last_activity"],
    }[sort]
    query = query.order_by((desc if direction == "desc" else asc)(sort_expr), m.School.id)
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    rows = db.execute(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [serialize_school(row) for row in rows], "page": page, "page_size": page_size, "total": total,
            "range": {"from": str(start), "to": str(end)}}


@router.get("/schools/{school_id}")
def school_detail(school_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require_ceo(user)
    school = db.get(m.School, school_id)
    if not school or school.school_type == "PLATFORM":
        raise HTTPException(404, "School was not found.")
    users = db.scalars(select(m.User).where(m.User.school_id == school_id).order_by(m.User.name)).all()
    return {
        "school": serialize(school),
        "counts": {
            "users": len(users),
            "students": db.scalar(select(func.count(m.Student.id)).where(m.Student.school_id == school_id)) or 0,
            "staff": db.scalar(select(func.count(m.Staff.id)).where(m.Staff.school_id == school_id)) or 0,
        },
        "users": [serialize(u) for u in users],
    }


@router.patch("/schools/{school_id}/users/{user_id}")
def update_school_user(
    school_id: str,
    user_id: str,
    data: AccountAccessAction,
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    require_ceo(user)
    school = db.get(m.School, school_id)
    account = db.scalar(select(m.User).where(m.User.id == user_id, m.User.school_id == school_id).with_for_update())
    if not school or school.school_type == "PLATFORM" or not account:
        raise HTTPException(404, "School user was not found.")
    if school.status == "REVOKED" and data.active:
        raise HTTPException(409, "Users cannot be activated while the school is revoked.")
    if account.role == "SCHOOL_OWNER" and not data.active:
        raise HTTPException(422, "The school owner account cannot be deactivated.")
    if account.active != data.active:
        account.active = data.active
        account.token_version += 1
        for token in db.scalars(select(m.AuthToken).where(m.AuthToken.user_id == account.id, m.AuthToken.used.is_(False))):
            token.used = True
    record_platform_audit(
        db,
        user,
        school.id,
        "platform.school_user_access_changed",
        {"user_id": account.id, "active": account.active},
    )
    return {"user": serialize(account)}


@router.post("/schools/{school_id}/revoke")
def revoke_school(school_id: str, data: AccessAction, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require_ceo(user)
    if not data.confirmation:
        raise HTTPException(422, "Confirm that you want to revoke this school.")
    school = db.scalar(select(m.School).where(m.School.id == school_id).with_for_update())
    if not school or school.school_type == "PLATFORM":
        raise HTTPException(404, "School was not found.")
    accounts = db.scalars(select(m.User).where(m.User.school_id == school_id).with_for_update()).all()
    for account in accounts:
        account.active = False
        account.token_version += 1
    for student in db.scalars(select(m.Student).where(m.Student.school_id == school_id).with_for_update()):
        student.qr_version += 1
    for token in db.scalars(select(m.AuthToken).where(m.AuthToken.school_id == school_id, m.AuthToken.used.is_(False))):
        token.used = True
    for job in db.scalars(select(m.Notification).where(m.Notification.school_id == school_id, m.Notification.status == "QUEUED")):
        job.status = "CANCELLED"
    for job in db.scalars(select(m.Document).where(m.Document.school_id == school_id, m.Document.status == "QUEUED")):
        job.status = "CANCELLED"
    school.status = "REVOKED"
    record_platform_audit(db, user, school.id, "platform.school_revoked", {"affected_users": len(accounts), "reason": data.reason})
    return {"status": "revoked", "affected_users": len(accounts)}


@router.post("/schools/{school_id}/restore")
def restore_school(school_id: str, data: AccessAction, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require_ceo(user)
    if not data.confirmation:
        raise HTTPException(422, "Confirm that you want to restore this school.")
    school = db.scalar(select(m.School).where(m.School.id == school_id).with_for_update())
    if not school or school.school_type == "PLATFORM":
        raise HTTPException(404, "School was not found.")
    accounts = db.scalars(select(m.User).where(m.User.school_id == school_id).with_for_update()).all()
    for account in accounts:
        account.active = True
    school.status = "ACTIVE"
    record_platform_audit(db, user, school.id, "platform.school_restored", {"reason": data.reason})
    return {"status": "active", "affected_users": len(accounts), "users_must_sign_in_again": True}
