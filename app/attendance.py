import io
from datetime import date, timedelta, datetime
from typing import Literal
from zoneinfo import ZoneInfo
import jwt
import qrcode
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import StrictModel
from .core.config import get_settings
from .core.database import get_db
from .core.models import now
from .core.security import current_user, require, audit
from .models import Student, Attendance, Term, School
from .resources import scoped, class_access, serialize
from .notifications import queue_parents

router = APIRouter(tags=["Attendance"])
Status = Literal["PRESENT", "ABSENT", "LATE", "EXCUSED"]


class AttendanceInput(StrictModel):
    student_id: str
    term_id: str
    attendance_date: date | None = None
    status: Status = "PRESENT"
    note: str = Field(default="", max_length=500)


class BulkInput(StrictModel):
    records: list[AttendanceInput] = Field(min_length=1, max_length=200)


class Correction(StrictModel):
    status: Status
    reason: str = Field(min_length=5, max_length=500)


class Scan(StrictModel):
    token: str = Field(min_length=20, max_length=2048)
    term_id: str


def mark(db, user, data, source="MANUAL"):
    student = scoped(db, Student, data.student_id, user, lock=True)
    term = scoped(db, Term, data.term_id, user)
    if student.status != "ACTIVE" or not student.class_id:
        raise HTTPException(422, "Attendance requires an active student assigned to a class.")
    class_access(db, user, student.class_id)
    school = db.get(School, user.school_id)
    today = datetime.now(ZoneInfo(school.timezone)).date()
    day = data.attendance_date or today
    if day > today:
        raise HTTPException(422, "Attendance cannot be marked for a future date.")
    if (term.start_date and day < term.start_date) or (term.end_date and day > term.end_date):
        raise HTTPException(422, "Attendance date must be within the selected term.")
    if day != today:
        require(user, "attendance.correct")
    existing = db.scalar(
        select(Attendance).where(
            Attendance.student_id == student.id, Attendance.attendance_date == day, Attendance.term_id == term.id
        )
    )
    if existing:
        if source == "QR" or existing.status == data.status:
            return {"record": serialize(existing), "duplicate": True}
        raise HTTPException(409, "Attendance already exists. Use the correction workflow.")
    record = Attendance(
        school_id=user.school_id,
        student_id=student.id,
        class_id=student.class_id,
        term_id=term.id,
        attendance_date=day,
        status=data.status,
        marked_by=user.id,
        source=source,
        note=data.note,
    )
    db.add(record)
    db.flush()
    audit(db, user, "attendance.marked", "attendance", record.id, {"status": data.status, "source": source})
    queue_parents(
        db,
        user,
        student,
        "attendance",
        f"attendance:{record.id}",
        "{{student_name}} was marked " + data.status.lower() + " at {{school_name}} on {{date}}.",
        {"date": day.isoformat(), "term": term.name},
    )
    return {"record": serialize(record), "duplicate": False}


@router.post("/attendance/mark", status_code=201)
def manual(data: AttendanceInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "attendance.mark")
    return mark(db, user, data)


@router.post("/attendance/bulk")
def bulk(data: BulkInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "attendance.mark")
    # Stable lock order avoids deadlocks for overlapping class submissions.
    return {"items": [mark(db, user, record) for record in sorted(data.records, key=lambda r: r.student_id)]}


@router.patch("/attendance/{record_id}")
def correct(
    record_id: str, data: Correction, user=Depends(current_user), db: Session = Depends(get_db, scope="function")
):
    require(user, "attendance.correct")
    record = scoped(db, Attendance, record_id, user, lock=True)
    class_access(db, user, record.class_id)
    old = record.status
    record.status, record.note, record.marked_by = data.status, data.reason, user.id
    audit(
        db,
        user,
        "attendance.corrected",
        "attendance",
        record.id,
        {"previous": old, "status": data.status, "reason": data.reason},
    )
    student = scoped(db, Student, record.student_id, user)
    queue_parents(
        db,
        user,
        student,
        "attendance",
        f"attendance-corrected:{record.id}:{record.updated_at.isoformat()}",
        "Attendance for {{student_name}} has been corrected to " + data.status.lower() + ".",
    )
    return serialize(record)


def qr_token(student):
    return jwt.encode(
        {
            "sub": student.id,
            "school": student.school_id,
            "version": student.qr_version,
            "kind": "attendance",
            "aud": "ile-iwe-qr",
            "exp": now() + timedelta(days=365),
        },
        get_settings().jwt_secret_key,
        algorithm="HS256",
    )


@router.get("/students/{student_id}/qr")
def get_qr(
    student_id: str, image: bool = False, user=Depends(current_user), db: Session = Depends(get_db, scope="function")
):
    require(user, "attendance.mark")
    student = scoped(db, Student, student_id, user)
    class_access(db, user, student.class_id)
    token = qr_token(student)
    url = get_settings().frontend_url + "/attendance/qr?token=" + token
    if image:
        buffer = io.BytesIO()
        qrcode.make(url).save(buffer, format="PNG")
        return Response(buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})
    return {"token": token, "url": url, "expires_in_days": 365}


@router.post("/students/{student_id}/qr/rotate")
def rotate(student_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "students.update")
    student = scoped(db, Student, student_id, user, lock=True)
    student.qr_version += 1
    audit(db, user, "student.qr_rotated", "students", student.id)
    return {"token": qr_token(student)}


@router.post("/attendance/scan")
def scan(data: Scan, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "attendance.mark")
    try:
        payload = jwt.decode(data.token, get_settings().jwt_secret_key, algorithms=["HS256"], audience="ile-iwe-qr")
        if payload.get("kind") != "attendance" or payload["school"] != user.school_id:
            raise ValueError()
        student = scoped(db, Student, payload["sub"], user, lock=True)
        if payload["version"] != student.qr_version:
            raise ValueError()
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(400, "QR code is invalid, expired, or belongs to another school.")
    return mark(db, user, AttendanceInput(student_id=student.id, term_id=data.term_id), source="QR")
