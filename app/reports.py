import csv
import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .core.database import get_db
from .core.security import current_user, require, permission_names, audit
from .core.models import now
from . import models as m
from .resources import serialize, serialize_for, audience_query, base_query, SPECS, read_permission, scoped
from .finance import ledger

router = APIRouter(tags=["Dashboards and reports"])


@router.get("/dashboard")
def dashboard(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    if user.role == "PARENT":
        return portal(user, db)
    school = db.get(m.School, user.school_id)
    today = datetime.now(ZoneInfo(school.timezone)).date()
    permissions = permission_names(user.role)

    def count(model, *conditions):
        return db.scalar(select(func.count()).select_from(base_query(db, user, model).where(*conditions).subquery()))

    stats = {"students": count(m.Student, m.Student.status == "ACTIVE"), "classes": count(m.ClassArm)}
    if "staff.view" in permissions:
        stats["staff"] = count(m.Staff, m.Staff.active.is_(True))
    if "parents.view" in permissions:
        stats["parents"] = count(m.Parent)
    if "attendance.view" in permissions:
        stats["present"] = count(
            m.Attendance, m.Attendance.attendance_date == today, m.Attendance.status.in_(["PRESENT", "LATE"])
        )
        stats["absent"] = count(m.Attendance, m.Attendance.attendance_date == today, m.Attendance.status == "ABSENT")
        stats["attendance_marked"] = count(m.Attendance, m.Attendance.attendance_date == today)
    if "finance.view" in permissions:
        charged = db.scalar(
            select(func.coalesce(func.sum(m.StudentCharge.amount - m.StudentCharge.discount), 0)).where(
                m.StudentCharge.school_id == user.school_id
            )
        )
        paid = db.scalar(
            select(func.coalesce(func.sum(m.Payment.amount), 0)).where(
                m.Payment.school_id == user.school_id, m.Payment.reversed.is_(False)
            )
        )
        stats["collected"], stats["outstanding"] = paid, charged - paid
        stats["today_collected"] = db.scalar(
            select(func.coalesce(func.sum(m.Payment.amount), 0)).where(
                m.Payment.school_id == user.school_id, m.Payment.payment_date == today, m.Payment.reversed.is_(False)
            )
        )
    if "results.view" in permissions:
        stats["pending_results"] = count(m.Result, m.Result.status != "PUBLISHED")
    trend = []
    if "attendance.view" in permissions:
        for offset in range(6, -1, -1):
            day = today - timedelta(days=offset)
            trend.append(
                {
                    "date": str(day),
                    "present": count(
                        m.Attendance, m.Attendance.attendance_date == day, m.Attendance.status.in_(["PRESENT", "LATE"])
                    ),
                    "absent": count(m.Attendance, m.Attendance.attendance_date == day, m.Attendance.status == "ABSENT"),
                }
            )
    recent = (
        [
            serialize(r)
            for r in db.scalars(
                select(m.AuditLog)
                .where(m.AuditLog.school_id == user.school_id)
                .order_by(m.AuditLog.created_at.desc())
                .limit(8)
            )
        ]
        if "audit.view" in permissions
        else []
    )
    timetable = [
        serialize(r)
        for r in db.scalars(
            base_query(db, user, m.TimetableEntry)
            .where(m.TimetableEntry.weekday == today.weekday(), m.TimetableEntry.published.is_(True))
            .order_by(m.TimetableEntry.start_time)
        )
    ]
    announcements = [
        serialize(r)
        for r in db.scalars(base_query(db, user, m.Announcement).order_by(m.Announcement.created_at.desc()).limit(5))
    ]
    return {
        "stats": stats,
        "attendance_trend": trend,
        "recent_activity": recent,
        "timetable": timetable,
        "announcements": announcements,
        "date": str(today),
    }


@router.get("/portal")
def portal(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "portal.view")
    ids = (
        select(m.StudentParent.student_id)
        .join(m.Parent, m.Parent.id == m.StudentParent.parent_id)
        .where(
            m.Parent.school_id == user.school_id,
            m.Parent.user_id == user.id,
            m.StudentParent.school_id == user.school_id,
        )
    )
    children = db.scalars(select(m.Student).where(m.Student.school_id == user.school_id, m.Student.id.in_(ids))).all()
    result = []
    for child in children:
        attendance = db.scalars(
            select(m.Attendance)
            .where(m.Attendance.school_id == user.school_id, m.Attendance.student_id == child.id)
            .order_by(m.Attendance.attendance_date.desc())
            .limit(30)
        ).all()
        grades = db.scalars(
            select(m.Result).where(
                m.Result.school_id == user.school_id, m.Result.student_id == child.id, m.Result.status == "PUBLISHED"
            )
        ).all()
        docs = db.scalars(
            select(m.Document).where(
                m.Document.school_id == user.school_id, m.Document.student_id == child.id, m.Document.status == "READY"
            )
        ).all()
        grade_rows = [
            {
                **serialize(r),
                "subject_name": db.get(m.Subject, r.subject_id).name,
                "term_name": db.get(m.Term, r.term_id).name,
            }
            for r in grades
        ]
        document_rows = [
            {**serialize(d), "term_name": db.get(m.Term, d.term_id).name}
            for d in docs
            if not d.payment_id or not db.get(m.Payment, d.payment_id).reversed
        ]
        result.append(
            {
                "student": serialize(child),
                "ledger": ledger(db, user.school_id, child.id),
                "attendance": [serialize(a) for a in attendance],
                "results": grade_rows,
                "documents": document_rows,
            }
        )
    return {
        "children": result,
        "announcements": [
            serialize(a)
            for a in db.scalars(
                audience_query(db, user, m.Announcement).order_by(m.Announcement.created_at.desc()).limit(20)
            )
        ],
    }


@router.get("/reports/{resource}.csv")
def export(resource: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "reports.view")
    if resource not in {"students", "parents", "attendance", "results", "payments", "charges"}:
        raise HTTPException(404, "Report was not found.")
    model, domain, _ = SPECS[resource]
    read_permission(user, domain)
    records = db.scalars(base_query(db, user, model).order_by(model.created_at).limit(10000)).all()
    rows = [serialize_for(db, user, r) for r in records]
    output = io.StringIO()
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        # Neutralize spreadsheet formulas in user-entered text.
        for row in rows:
            writer.writerow(
                {
                    k: "'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v
                    for k, v in row.items()
                }
            )
    audit(db, user, "report.exported", resource, user.school_id, {"rows": len(rows)})
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ile-iwe-{resource}.csv"'},
    )


@router.post("/jobs/{kind}/{job_id}/retry")
def retry(kind: str, job_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "school.manage_settings")
    model = {"notifications": m.Notification, "documents": m.Document}.get(kind)
    if not model:
        raise HTTPException(404, "Job was not found.")
    job = scoped(db, model, job_id, user, lock=True)
    if job.status != "FAILED":
        raise HTTPException(409, "Only failed jobs can be retried.")
    job.status, job.attempts, job.available_at = "QUEUED", 0, now()
    audit(db, user, "job.retried", kind, job.id)
    return serialize(job)
