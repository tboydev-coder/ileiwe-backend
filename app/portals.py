"""Purpose-built portals. All record scopes originate from authenticated relationships."""

from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import select, func, or_, case
from sqlalchemy.orm import Session
from . import models as m
from .auth import StrictModel
from .core.database import get_db
from .core.models import now
from .core.security import current_user, require, audit
from .resources import (
    serialize,
    serialize_for,
    scoped,
    teacher_classes,
    class_teacher_ids,
    child_ids,
    child_access,
    audience_query,
    base_query,
    class_access,
)
from .finance import ledger

router = APIRouter(tags=["Teacher and parent portals"])


def teacher_only(user):
    require(user, "students.view")
    if user.role != "TEACHER":
        raise HTTPException(403, "A teacher account is required.")


def page_rows(db, query, page, size):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {
        "items": [serialize(r) for r in db.scalars(query.offset((page - 1) * size).limit(size))],
        "total": total,
        "page": page,
        "page_size": size,
    }


def timetable_rows(db, query):
    rows = db.execute(
        query.add_columns(m.Subject.name, m.User.name, m.ClassLevel.name, m.ClassArm.name)
        .join(m.Subject, m.Subject.id == m.TimetableEntry.subject_id)
        .join(m.User, m.User.id == m.TimetableEntry.teacher_id)
        .join(m.ClassArm, m.ClassArm.id == m.TimetableEntry.class_id)
        .join(m.ClassLevel, m.ClassLevel.id == m.ClassArm.level_id)
        .order_by(m.TimetableEntry.weekday, m.TimetableEntry.start_time)
    ).all()
    return [
        {**serialize(r), "subject_name": subject, "teacher_name": teacher, "class_name": f"{level} {arm}"}
        for r, subject, teacher, level, arm in rows
    ]


@router.get("/teacher/classes")
def classes(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    teacher_only(user)
    school = db.get(m.School, user.school_id)
    today = datetime.now(ZoneInfo(school.timezone)).date()
    ids = teacher_classes(db, user)
    counts = dict(
        db.execute(
            select(m.Student.class_id, func.count())
            .where(m.Student.school_id == user.school_id, m.Student.class_id.in_(ids), m.Student.status == "ACTIVE")
            .group_by(m.Student.class_id)
        ).all()
    )
    attendance = dict(
        db.execute(
            select(m.Attendance.class_id, func.count(func.distinct(m.Attendance.student_id)))
            .where(
                m.Attendance.school_id == user.school_id,
                m.Attendance.class_id.in_(ids),
                m.Attendance.attendance_date == today,
            )
            .group_by(m.Attendance.class_id)
        ).all()
    )
    averages = dict(
        db.execute(
            base_query(db, user, m.Result)
            .with_only_columns(m.Result.class_id, func.avg(m.Result.total))
            .group_by(m.Result.class_id)
        ).all()
    )
    subjects = db.execute(
        select(m.SubjectAssignment.class_id, m.Subject.name)
        .join(m.Subject, m.Subject.id == m.SubjectAssignment.subject_id)
        .where(m.SubjectAssignment.school_id == user.school_id, m.SubjectAssignment.teacher_id == user.id)
    ).all()
    return [
        {
            **serialize(arm),
            "class_name": f"{level} {arm.name}",
            "is_class_teacher": arm.teacher_id == user.id,
            "student_count": counts.get(arm.id, 0),
            "attendance_marked": attendance.get(arm.id, 0),
            "average": averages.get(arm.id),
            "subjects": [name for c, name in subjects if c == arm.id],
        }
        for arm, level in db.execute(
            select(m.ClassArm, m.ClassLevel.name)
            .join(m.ClassLevel, m.ClassLevel.id == m.ClassArm.level_id)
            .where(m.ClassArm.school_id == user.school_id, m.ClassArm.id.in_(ids))
            .order_by(m.ClassLevel.sort_order, m.ClassArm.name)
        )
    ]


@router.get("/teacher/dashboard")
def teacher_dashboard(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    teacher_only(user)
    school = db.get(m.School, user.school_id)
    today = datetime.now(ZoneInfo(school.timezone)).date()
    assigned = classes(user, db)
    lessons = timetable_rows(
        db,
        base_query(db, user, m.TimetableEntry).where(
            m.TimetableEntry.weekday == today.weekday(), m.TimetableEntry.published.is_(True)
        ),
    )
    return {
        "date": today,
        "classes": assigned,
        "timetable": lessons,
        "stats": {
            "classes": len(assigned),
            "subjects": db.scalar(
                select(func.count(func.distinct(m.SubjectAssignment.subject_id))).where(
                    m.SubjectAssignment.school_id == user.school_id, m.SubjectAssignment.teacher_id == user.id
                )
            ),
            "lessons": len(lessons),
            "attendance_pending": sum(max(0, c["student_count"] - c["attendance_marked"]) for c in assigned),
            "results_pending": db.scalar(
                select(func.count()).select_from(
                    base_query(db, user, m.Result)
                    .where(m.Result.entered_by == user.id, m.Result.status == "DRAFT")
                    .subquery()
                )
            ),
        },
        "announcements": [
            serialize(r)
            for r in db.scalars(
                audience_query(db, user, m.Announcement).order_by(m.Announcement.created_at.desc()).limit(5)
            )
        ],
        "events": [
            serialize(r)
            for r in db.scalars(
                audience_query(db, user, m.CalendarEvent)
                .where(m.CalendarEvent.end_date >= today)
                .order_by(m.CalendarEvent.start_date)
                .limit(5)
            )
        ],
    }


@router.get("/teacher/timetable")
def teacher_timetable(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    teacher_only(user)
    return timetable_rows(db, base_query(db, user, m.TimetableEntry).where(m.TimetableEntry.published.is_(True)))


@router.get("/teacher/students/{student_id}")
def teacher_student(student_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    teacher_only(user)
    student = scoped(db, m.Student, student_id, user)
    class_access(db, user, student.class_id)
    is_class_teacher = student.class_id in class_teacher_ids(db, user)
    guardians = []
    if is_class_teacher:
        guardians = [
            {
                "name": parent.name,
                "phone": parent.phone,
                "email": parent.email,
                "relationship": link.relationship,
                "primary_contact": link.primary_contact,
            }
            for parent, link in db.execute(
                select(m.Parent, m.StudentParent)
                .join(m.StudentParent, m.StudentParent.parent_id == m.Parent.id)
                .where(m.Parent.school_id == user.school_id, m.StudentParent.student_id == student.id)
            )
        ]
    return {"student": serialize_for(db, user, student), "guardians": guardians, "is_class_teacher": is_class_teacher}


@router.get("/teacher/students")
def students(
    class_id: str = "",
    subject_id: str = "",
    search: str = Query("", max_length=200),
    status: str = "",
    gender: str = "",
    attendance_status: Literal["", "PRESENT", "ABSENT", "LATE", "EXCUSED", "UNMARKED"] = "",
    academic_status: Literal["", "NEEDS_SUPPORT", "PASSING"] = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    teacher_only(user)
    query = base_query(db, user, m.Student)
    if class_id:
        class_access(db, user, class_id)
        query = query.where(m.Student.class_id == class_id)
    if subject_id:
        query = query.where(
            m.Student.class_id.in_(
                select(m.SubjectAssignment.class_id).where(
                    m.SubjectAssignment.school_id == user.school_id,
                    m.SubjectAssignment.teacher_id == user.id,
                    m.SubjectAssignment.subject_id == subject_id,
                )
            )
        )
    if status:
        query = query.where(m.Student.status == status)
    if gender:
        query = query.where(m.Student.gender == gender)
    if attendance_status:
        today = datetime.now(ZoneInfo(db.get(m.School, user.school_id).timezone)).date()
        marks = select(m.Attendance.student_id).where(
            m.Attendance.school_id == user.school_id, m.Attendance.attendance_date == today
        )
        if attendance_status == "UNMARKED":
            query = query.where(m.Student.id.not_in(marks))
        else:
            query = query.where(m.Student.id.in_(marks.where(m.Attendance.status == attendance_status)))
    if academic_status:
        scores = base_query(db, user, m.Result)
        if subject_id:
            scores = scores.where(m.Result.subject_id == subject_id)
        failing = scores.where(m.Result.passing.is_(False)).with_only_columns(m.Result.student_id)
        query = (
            query.where(m.Student.id.in_(failing))
            if academic_status == "NEEDS_SUPPORT"
            else query.where(
                m.Student.id.in_(scores.with_only_columns(m.Result.student_id)), m.Student.id.not_in(failing)
            )
        )
    if search:
        pattern = "%" + search.replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(
            or_(
                *(
                    c.ilike(pattern, escape="\\")
                    for c in [m.Student.first_name, m.Student.last_name, m.Student.student_code, m.Student.admission_no]
                )
            )
        )
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    return {
        "items": [
            serialize_for(db, user, r)
            for r in db.scalars(
                query.order_by(m.Student.last_name, m.Student.id).offset((page - 1) * page_size).limit(page_size)
            )
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/teacher/performance")
def performance(
    class_id: str | None = None,
    term_id: str | None = None,
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    teacher_only(user)
    query = base_query(db, user, m.Result)
    if class_id:
        class_access(db, user, class_id)
        query = query.where(m.Result.class_id == class_id)
    if term_id:
        scoped(db, m.Term, term_id, user)
        query = query.where(m.Result.term_id == term_id)
    data = query.subquery()
    return [
        {
            "class_name": f"{level} {arm}",
            "subject_name": subject,
            "term_name": term,
            "average": average,
            "highest": highest,
            "lowest": lowest,
            "results": count,
            "pass_rate": round(100 * passed / count, 1) if count else None,
            "fail_rate": round(100 * (count - passed) / count, 1) if count else None,
        }
        for level, arm, subject, term, average, highest, lowest, count, passed in db.execute(
            select(
                m.ClassLevel.name,
                m.ClassArm.name,
                m.Subject.name,
                m.Term.name,
                func.avg(data.c.total),
                func.max(data.c.total),
                func.min(data.c.total),
                func.count(),
                func.sum(case((data.c.passing.is_(True), 1), else_=0)),
            )
            .select_from(data)
            .join(m.ClassArm, m.ClassArm.id == data.c.class_id)
            .join(m.ClassLevel, m.ClassLevel.id == m.ClassArm.level_id)
            .join(m.Subject, m.Subject.id == data.c.subject_id)
            .join(m.Term, m.Term.id == data.c.term_id)
            .group_by(m.ClassLevel.name, m.ClassArm.name, m.Subject.name, m.Term.name)
        )
    ]


@router.get("/teacher/insights")
def insights(
    class_id: str | None = None,
    term_id: str | None = None,
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    teacher_only(user)
    query = base_query(db, user, m.Result)
    if class_id:
        class_access(db, user, class_id)
        query = query.where(m.Result.class_id == class_id)
    if term_id:
        scoped(db, m.Term, term_id, user)
        query = query.where(m.Result.term_id == term_id)
    data = query.subquery()
    averages = (
        select(
            data.c.student_id,
            func.avg(data.c.total).label("average"),
            func.sum(case((data.c.passing.is_(False), 1), else_=0)).label("failed_subjects"),
        )
        .group_by(data.c.student_id)
        .subquery()
    )
    ranked = (
        select(m.Student.id, m.Student.first_name, m.Student.last_name, averages.c.average, averages.c.failed_subjects)
        .join(averages, averages.c.student_id == m.Student.id)
        .where(m.Student.school_id == user.school_id)
    )

    def learners(q):
        return [
            {"id": id, "name": f"{first} {last}", "average": round(float(average), 1), "failed_subjects": failed}
            for id, first, last, average, failed in db.execute(q.limit(10))
        ]

    return {
        "top_students": learners(ranked.order_by(averages.c.average.desc(), m.Student.id)),
        "needs_support": learners(
            ranked.where(averages.c.failed_subjects > 0).order_by(averages.c.average, m.Student.id)
        ),
        "grade_distribution": [
            {"grade": grade, "results": count}
            for grade, count in db.execute(
                select(data.c.grade, func.count()).group_by(data.c.grade).order_by(data.c.grade)
            )
        ],
        "trends": [
            {"term": name, "session": session, "average": round(float(average), 1)}
            for name, session, average in db.execute(
                select(m.Term.name, m.AcademicSession.name, func.avg(data.c.total))
                .select_from(data)
                .join(m.Term, m.Term.id == data.c.term_id)
                .join(m.AcademicSession, m.AcademicSession.id == m.Term.session_id)
                .group_by(m.Term.id, m.Term.name, m.Term.created_at, m.AcademicSession.name)
                .order_by(m.Term.created_at)
            )
        ],
    }


@router.get("/parent/children")
def children(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "portal.view")
    return [
        {**serialize(child), "class_name": f"{level} {arm}" if level else "Unassigned"}
        for child, level, arm in db.execute(
            select(m.Student, m.ClassLevel.name, m.ClassArm.name)
            .outerjoin(m.ClassArm, m.ClassArm.id == m.Student.class_id)
            .outerjoin(m.ClassLevel, m.ClassLevel.id == m.ClassArm.level_id)
            .where(m.Student.school_id == user.school_id, m.Student.id.in_(child_ids(user)))
            .order_by(m.Student.first_name)
        )
    ]


@router.get("/parent/children/{student_id}/dashboard")
def parent_dashboard(student_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    child = child_access(db, user, student_id)
    attendance = dict(
        db.execute(
            select(m.Attendance.status, func.count())
            .where(m.Attendance.school_id == user.school_id, m.Attendance.student_id == child.id)
            .group_by(m.Attendance.status)
        ).all()
    )
    total = sum(attendance.values())
    school = db.get(m.School, user.school_id)
    today = datetime.now(ZoneInfo(school.timezone)).date()
    return {
        "student": serialize(child),
        "date": today,
        "attendance": attendance,
        "attendance_rate": round(100 * (attendance.get("PRESENT", 0) + attendance.get("LATE", 0)) / total, 1)
        if total
        else None,
        "ledger": ledger(db, user.school_id, child.id),
        "published_results": db.scalar(
            select(func.count())
            .select_from(m.Result)
            .where(
                m.Result.school_id == user.school_id, m.Result.student_id == child.id, m.Result.status == "PUBLISHED"
            )
        ),
        "announcements": [
            serialize(r)
            for r in db.scalars(
                audience_query(db, user, m.Announcement)
                .where(or_(m.Announcement.class_id.is_(None), m.Announcement.class_id == child.class_id))
                .order_by(m.Announcement.created_at.desc())
                .limit(5)
            )
        ],
        "events": [
            serialize(r)
            for r in db.scalars(
                audience_query(db, user, m.CalendarEvent)
                .where(
                    m.CalendarEvent.end_date >= today,
                    or_(m.CalendarEvent.class_id.is_(None), m.CalendarEvent.class_id == child.class_id),
                )
                .order_by(m.CalendarEvent.start_date)
                .limit(5)
            )
        ],
    }


@router.get("/parent/children/{student_id}/{resource}")
def child_records(
    student_id: str,
    resource: Literal["timetable", "attendance", "results", "report-cards", "fees", "payments", "receipts"],
    term_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    child = child_access(db, user, student_id)
    if term_id:
        scoped(db, m.Term, term_id, user)
    if resource == "timetable":
        return {
            "items": timetable_rows(
                db,
                select(m.TimetableEntry).where(
                    m.TimetableEntry.school_id == user.school_id,
                    m.TimetableEntry.class_id == child.class_id,
                    m.TimetableEntry.published.is_(True),
                ),
            )
        }
    if resource == "fees":
        query = select(m.StudentCharge).where(
            m.StudentCharge.school_id == user.school_id, m.StudentCharge.student_id == child.id
        )
        if term_id:
            query = query.where(m.StudentCharge.term_id == term_id)
        return {
            **ledger(db, user.school_id, child.id, term_id),
            **page_rows(db, query.order_by(m.StudentCharge.created_at.desc()), page, page_size),
        }
    model = {
        "attendance": m.Attendance,
        "results": m.Result,
        "report-cards": m.Document,
        "payments": m.Payment,
        "receipts": m.Document,
    }[resource]
    query = select(model).where(model.school_id == user.school_id, model.student_id == child.id)
    if term_id:
        query = query.where(model.term_id == term_id)
    if model is m.Result:
        query = query.where(m.Result.status == "PUBLISHED")
    if model is m.Document:
        query = query.where(
            m.Document.status == "READY",
            m.Document.kind == ("RECEIPT" if resource == "receipts" else "REPORT_CARD"),
            or_(
                m.Document.payment_id.is_(None),
                m.Document.payment_id.in_(
                    select(m.Payment.id).where(m.Payment.school_id == user.school_id, m.Payment.reversed.is_(False))
                ),
            ),
        )
    result = page_rows(db, query.order_by(model.created_at.desc(), model.id), page, page_size)
    terms = dict(db.execute(select(m.Term.id, m.Term.name).where(m.Term.school_id == user.school_id)).all())
    for row in result["items"]:
        row["term_name"] = terms.get(row["term_id"], "")
    if model is m.Result:
        subjects = dict(
            db.execute(select(m.Subject.id, m.Subject.name).where(m.Subject.school_id == user.school_id)).all()
        )
        ids = [r["id"] for r in result["items"]]
        scores = db.execute(
            select(m.ResultScore, m.AssessmentComponent.name)
            .join(m.AssessmentComponent, m.AssessmentComponent.id == m.ResultScore.component_id)
            .where(m.ResultScore.school_id == user.school_id, m.ResultScore.result_id.in_(ids))
        ).all()
        for row in result["items"]:
            row["subject_name"] = subjects.get(row["subject_id"], "")
            row["scores"] = [
                {"name": name, "score": score.score, "maximum": score.max_score}
                for score, name in scores
                if score.result_id == row["id"]
            ]
    if resource == "receipts":
        ids = [r["payment_id"] for r in result["items"]]
        payments = {
            p.id: p
            for p in db.scalars(select(m.Payment).where(m.Payment.school_id == user.school_id, m.Payment.id.in_(ids)))
        }
        for row in result["items"]:
            payment = payments[row["payment_id"]]
            row.update(
                {
                    "receipt_number": payment.receipt_number,
                    "amount": payment.amount,
                    "payment_date": payment.payment_date,
                    "method": payment.method,
                    "reference": payment.reference,
                }
            )
    return result


@router.get("/portal/terms")
def terms(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    if user.role == "PARENT":
        require(user, "portal.view")
    else:
        teacher_only(user)
    return [
        {**serialize(term), "session_name": name}
        for term, name in db.execute(
            select(m.Term, m.AcademicSession.name)
            .join(m.AcademicSession, m.AcademicSession.id == m.Term.session_id)
            .where(m.Term.school_id == user.school_id)
            .order_by(m.Term.created_at.desc())
        )
    ]


@router.get("/portal/{resource}")
def shared_records(
    resource: Literal["calendar", "announcements", "notifications"],
    student_id: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    if user.role not in {"TEACHER", "PARENT"}:
        raise HTTPException(403, "A portal account is required.")
    child = child_access(db, user, student_id) if student_id and user.role == "PARENT" else None
    if resource == "notifications":
        query = select(m.InboxItem).where(m.InboxItem.school_id == user.school_id, m.InboxItem.user_id == user.id)
        if user.role == "PARENT":
            query = query.where(or_(m.InboxItem.student_id.is_(None), m.InboxItem.student_id.in_(child_ids(user))))
        if child:
            query = query.where(or_(m.InboxItem.student_id.is_(None), m.InboxItem.student_id == child.id))
        query = query.order_by(m.InboxItem.created_at.desc())
    else:
        model = m.CalendarEvent if resource == "calendar" else m.Announcement
        query = audience_query(db, user, model)
        if child:
            query = query.where(or_(model.class_id.is_(None), model.class_id == child.class_id))
        query = query.order_by(model.start_date if resource == "calendar" else model.created_at.desc())
    return page_rows(db, query, page, page_size)


@router.post("/portal/notifications/{notification_id}/read")
def mark_read(notification_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    item = db.scalar(
        select(m.InboxItem).where(
            m.InboxItem.school_id == user.school_id, m.InboxItem.user_id == user.id, m.InboxItem.id == notification_id
        )
    )
    if not item:
        raise HTTPException(404, "Notification was not found.")
    if user.role == "PARENT" and item.student_id:
        child_access(db, user, item.student_id)
    item.read_at = now()
    return serialize(item)


@router.post("/teacher/announcements", status_code=201)
def announce(data: dict, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    teacher_only(user)

    class AnnouncementInput(StrictModel):
        class_id: str
        title: str = Field(min_length=1, max_length=180)
        body: str = Field(min_length=1, max_length=5000)

    from pydantic import ValidationError

    try:
        values = AnnouncementInput.model_validate(data)
    except ValidationError:
        raise HTTPException(422, "Enter a class, title, and message.")
    if values.class_id not in class_teacher_ids(db, user):
        raise HTTPException(403, "Only class teachers may post class announcements.")
    record = m.Announcement(school_id=user.school_id, created_by=user.id, audience="ALL", **values.model_dump())
    db.add(record)
    db.flush()
    from .notifications import broadcast_announcement

    broadcast_announcement(db, user, record)
    audit(db, user, "announcement.created", "announcements", record.id)
    return serialize(record)


@router.get("/profile")
def profile(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    model = m.Parent if user.role == "PARENT" else m.Staff
    record = db.scalar(select(model).where(model.school_id == user.school_id, model.user_id == user.id))
    return {
        "user": serialize(user),
        "profile": serialize(record) if record else {},
        "mandatory_notifications": db.get(m.School, user.school_id).mandatory_notifications,
        "classes": classes(user, db) if user.role == "TEACHER" else [],
        "children": children(user, db) if user.role == "PARENT" else [],
    }


class ProfileInput(StrictModel):
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=2000)
    emergency_contact: str = Field(default="", max_length=300)
    notify_email: bool = True
    notification_preferences: dict[str, bool] = Field(default_factory=dict)


@router.patch("/profile")
def update_profile(data: ProfileInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    if set(data.notification_preferences) - {"attendance", "results", "finance", "announcements"}:
        raise HTTPException(422, "Unknown notification preference.")
    model = m.Parent if user.role == "PARENT" else m.Staff
    record = db.scalar(select(model).where(model.school_id == user.school_id, model.user_id == user.id))
    if not record:
        raise HTTPException(404, "Ask your administrator to create your profile.")
    fields = {"phone", "address"} | (
        {"notify_email", "notification_preferences"} if model is m.Parent else {"emergency_contact"}
    )
    for key, value in data.model_dump(exclude_unset=True).items():
        if key in fields:
            setattr(record, key, value)
    audit(
        db, user, "profile.updated", model.__tablename__, record.id, {"fields": sorted(data.model_fields_set & fields)}
    )
    return serialize(record)
