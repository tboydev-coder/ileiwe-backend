"""Shared, allowlisted CRUD for school directories and academic configuration."""

from datetime import date, time
from decimal import Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ConfigDict, Field, ValidationError, create_model, EmailStr
from sqlalchemy import select, func, or_, String, Numeric, Boolean, Date, Time, Integer
from sqlalchemy.orm import Session
from . import models as m
from .core.database import get_db
from .core.security import current_user, require, audit, passwords, assign_role

router = APIRouter(tags=["School directories"])

# The allowlist is the mutation boundary. IDs, tenant IDs and computed fields are never writable.
SPECS = {
    "sessions": (m.AcademicSession, "academics", "name start_date end_date active"),
    "terms": (m.Term, "academics", "session_id name start_date end_date active"),
    "class-levels": (m.ClassLevel, "academics", "name sort_order"),
    "classes": (m.ClassArm, "academics", "level_id name teacher_id"),
    "subjects": (m.Subject, "academics", "name code category compulsory active"),
    "assignments": (m.SubjectAssignment, "academics", "class_id subject_id teacher_id"),
    "students": (
        m.Student,
        "students",
        "admission_no first_name middle_name last_name date_of_birth gender address emergency_info admission_date class_id status",
    ),
    "parents": (m.Parent, "parents", "name phone email address notify_email user_id"),
    "student-parents": (m.StudentParent, "parents", "student_id parent_id relationship primary_contact"),
    "staff": (
        m.Staff,
        "staff",
        "user_id name first_name last_name staff_code phone job_title employment_date active gender date_of_birth department staff_type address emergency_contact",
    ),
    "users": (m.User, "users", "name email password role active"),
    "assessments": (m.AssessmentComponent, "academics", "name weight max_score active"),
    "grading-scales": (m.GradingScale, "academics", "name active"),
    "grades": (m.GradingScaleItem, "academics", "scale_id label min_score max_score remark passing point_value"),
    "timetable": (
        m.TimetableEntry,
        "academics",
        "class_id subject_id teacher_id room weekday start_time end_time published",
    ),
    "fee-structures": (m.FeeStructure, "finance", "name term_id class_id"),
    "fee-items": (m.FeeItem, "finance", "structure_id name amount"),
    "charges": (m.StudentCharge, "finance", "student_id term_id description amount discount"),
    "templates": (m.NotificationTemplate, "notifications", "name event channel body"),
    "announcements": (m.Announcement, "notifications", "title body audience class_id"),
    "calendar": (m.CalendarEvent, "academics", "title body start_date end_date audience class_id"),
    "attendance": (m.Attendance, "attendance", ""),
    "results": (m.Result, "results", ""),
    "payments": (m.Payment, "finance", ""),
    "notifications": (m.Notification, "notifications", ""),
    "documents": (m.Document, "reports", ""),
    "audit": (m.AuditLog, "audit", ""),
}
TABLE_MODELS = {model.__tablename__: model for model, _, _ in SPECS.values()}
READ_ONLY = {"attendance", "results", "payments", "notifications", "documents", "audit"}
OPTIONS = {
    "role": ["SCHOOL_ADMIN", "PRINCIPAL", "TEACHER", "ACCOUNTANT", "STAFF", "PARENT"],
    "status": ["ACTIVE", "INACTIVE", "WITHDRAWN", "GRADUATED", "TRANSFERRED"],
    "gender": ["Female", "Male", "Prefer not to say"],
    "channel": ["EMAIL"],
    "audience": ["ALL", "STAFF", "PARENTS"],
}


def serialize(record):
    hidden = {
        "password_hash",
        "token_version",
        "qr_version",
        "storage_key",
        "logo_key",
        "photo_key",
        "credential_nonce",
    }
    result = {c.name: getattr(record, c.name) for c in record.__table__.columns if c.name not in hidden}
    if isinstance(record, m.Student):
        result["name"] = " ".join(filter(None, [record.first_name, record.middle_name, record.last_name]))
        result["has_photo"] = bool(record.photo_key)
    if isinstance(record, m.School):
        result["has_logo"] = bool(record.logo_key)
    if isinstance(record, m.Staff):
        result["has_photo"] = bool(record.photo_key)
    if isinstance(record, m.Result):
        result["percentage"] = record.total
    # Password-reset links are secrets, including in notification history.
    if isinstance(record, m.Notification) and record.event_key.startswith(("reset:", "invite:")):
        result["body"] = "Password reset email (content hidden)"
    return result


def scoped(db, model, record_id, user, lock=False):
    query = select(model).where(model.id == record_id, model.school_id == user.school_id)
    record = db.scalar(query.with_for_update() if lock else query)
    if not record:
        raise HTTPException(404, "Record was not found.")
    return record


def teacher_classes(db, user):
    assignments = select(m.SubjectAssignment.class_id).where(
        m.SubjectAssignment.school_id == user.school_id, m.SubjectAssignment.teacher_id == user.id
    )
    return set(db.scalars(assignments)) | set(
        db.scalars(
            select(m.ClassArm.id).where(m.ClassArm.school_id == user.school_id, m.ClassArm.teacher_id == user.id)
        )
    )


def class_access(db, user, class_id, subject_id=None):
    if user.role == "TEACHER":
        if subject_id:
            allowed = db.scalar(
                select(m.SubjectAssignment.id).where(
                    m.SubjectAssignment.school_id == user.school_id,
                    m.SubjectAssignment.class_id == class_id,
                    m.SubjectAssignment.subject_id == subject_id,
                    m.SubjectAssignment.teacher_id == user.id,
                )
            )
        else:
            allowed = class_id in teacher_classes(db, user)
        if not allowed:
            raise HTTPException(403, "This class or subject is not assigned to you.")


def class_teacher_ids(db, user):
    key = ("class-teacher-ids", user.school_id, user.id)
    if key not in db.info:
        db.info[key] = set(
            db.scalars(
                select(m.ClassArm.id).where(m.ClassArm.school_id == user.school_id, m.ClassArm.teacher_id == user.id)
            )
        )
    return db.info[key]


def child_ids(user):
    return (
        select(m.StudentParent.student_id)
        .join(m.Parent, m.Parent.id == m.StudentParent.parent_id)
        .where(
            m.Parent.school_id == user.school_id,
            m.StudentParent.school_id == user.school_id,
            m.Parent.user_id == user.id,
        )
    )


def child_access(db, user, student_id):
    require(user, "portal.view")
    child = db.scalar(
        select(m.Student).where(
            m.Student.school_id == user.school_id, m.Student.id == student_id, m.Student.id.in_(child_ids(user))
        )
    )
    if not child:
        raise HTTPException(
            404, {"code": "PARENT_CHILD_RELATIONSHIP_NOT_FOUND", "message": "This child is not linked to your account."}
        )
    return child


def audience_query(db, user, model):
    classes = (
        teacher_classes(db, user)
        if user.role == "TEACHER"
        else set(
            db.scalars(
                select(m.Student.class_id).where(
                    m.Student.school_id == user.school_id, m.Student.id.in_(child_ids(user))
                )
            )
        )
    )
    audience = "STAFF" if user.role == "TEACHER" else "PARENTS"
    return select(model).where(
        model.school_id == user.school_id,
        model.audience.in_(["ALL", audience]),
        or_(model.class_id.is_(None), model.class_id.in_(classes)),
    )


def serialize_for(db, user, record):
    data = serialize(record)
    if user.role == "TEACHER" and isinstance(record, m.Student) and record.class_id not in class_teacher_ids(db, user):
        for field in ("address", "emergency_info", "date_of_birth"):
            data.pop(field, None)
    return data


def schema_for(resource, partial=False):
    model, _, names = SPECS[resource]
    fields = {}
    for name in names.split():
        if name == "password":
            fields[name] = (str, Field(default=None if partial else ..., min_length=12, max_length=128))
            continue
        col = model.__table__.columns[name]
        typ: Any = str
        constraints = {}
        if isinstance(col.type, Boolean):
            typ = bool
        elif isinstance(col.type, Integer):
            typ = int
        elif isinstance(col.type, Numeric):
            typ = Decimal
            constraints = {
                "allow_inf_nan": False,
                "max_digits": col.type.precision,
                "decimal_places": col.type.scale,
                "ge": 0,
            }
        elif isinstance(col.type, Date):
            typ = date
        elif isinstance(col.type, Time):
            typ = time
        elif isinstance(col.type, String):
            constraints["max_length"] = col.type.length or 10000
            if not col.nullable and col.default is None:
                constraints["min_length"] = 1
        if name in OPTIONS:
            allowed = OPTIONS[name] + (
                [""] if col.default is not None and col.default.is_scalar and col.default.arg == "" else []
            )
            typ = Literal[tuple(allowed)]
        if name == "email" and resource == "users":
            typ = EmailStr
        default = ...
        if col.default is not None and col.default.is_scalar:
            default = col.default.arg
            if isinstance(col.type, Numeric):
                default = Decimal(str(default))
        if col.nullable or partial:
            default = None
        if col.nullable:
            typ = typ | None
        fields[name] = (typ, Field(default=default, **constraints))
    return create_model(
        resource.replace("-", "_") + ("Update" if partial else "Create"),
        __config__=ConfigDict(extra="forbid", str_strip_whitespace=True),
        **fields,
    )


def validate_references(db, user, model, values):
    for name, value in values.items():
        if name not in model.__table__.columns or value is None:
            continue
        for fk in model.__table__.columns[name].foreign_keys:
            target = TABLE_MODELS.get(fk.column.table.name)
            if target:
                record = scoped(db, target, value, user)
                if name == "teacher_id" and (record.role != "TEACHER" or not record.active):
                    raise HTTPException(422, "Select an active teacher account.")
                if model is m.Parent and name == "user_id" and record.role != "PARENT":
                    raise HTTPException(422, "Select a parent account.")


def mutation_permission(resource, creating):
    _, domain, _ = SPECS[resource]
    if domain == "students":
        return "students.create" if creating else "students.update"
    if domain == "finance":
        return "finance.manage_fees"
    if domain == "notifications":
        return "notifications.send_email"
    return domain + ".manage"


def validate_business(db, user, resource, values, record=None):
    model = SPECS[resource][0]
    validate_references(db, user, model, values)
    merged = serialize(record) if record else {}
    merged.update(values)
    if (
        resource in {"sessions", "terms", "calendar"}
        and merged.get("start_date")
        and merged.get("end_date")
        and merged["end_date"] < merged["start_date"]
    ):
        raise HTTPException(422, "End date must follow start date.")
    if resource == "students":
        if merged.get("date_of_birth") and merged["date_of_birth"] > date.today():
            raise HTTPException(422, "Date of birth cannot be in the future.")
        if record and values.get("status", record.status) != record.status:
            require(user, "students.archive")
        if values.get("admission_no") == "":
            values["admission_no"] = None
    if resource == "users":
        if record and record.role in {"SCHOOL_OWNER", "PLATFORM_SUPER_ADMIN"} and record.id != user.id:
            raise HTTPException(403, "This account is protected.")
        if record and record.id == user.id and ("role" in values or values.get("active") is False):
            raise HTTPException(422, "You cannot change your own role or deactivate yourself.")
        if "password" in values:
            values["password_hash"] = passwords.hash(values.pop("password"))
        if "email" in values:
            values["email"] = str(values["email"]).lower()
        if record and set(values) & {"password_hash", "active", "role"}:
            record.token_version += 1
            for token in db.scalars(select(m.AuthToken).where(m.AuthToken.user_id == record.id)):
                token.used = True
            if "password_hash" in values:
                record.must_change_password = True
                from datetime import timedelta
                from .core.models import now
                from .core.config import get_settings

                record.temporary_password_expires_at = now() + timedelta(
                    hours=get_settings().temporary_password_expire_hours
                )
                record.credential_nonce = None
        if record and "active" in values:
            staff = db.scalar(select(m.Staff).where(m.Staff.school_id == user.school_id, m.Staff.user_id == record.id))
            if staff:
                staff.active = values["active"]
    if resource == "assessments" and record:
        if db.scalar(select(m.ResultScore.id).where(m.ResultScore.component_id == record.id)) and set(values) & {
            "weight",
            "max_score",
        }:
            raise HTTPException(409, "This component has scores. Deactivate it and create a new component instead.")
    if resource == "grades":
        if merged["min_score"] > merged["max_score"]:
            raise HTTPException(422, "Minimum score must not exceed maximum score.")
        overlap = select(m.GradingScaleItem).where(
            m.GradingScaleItem.school_id == user.school_id,
            m.GradingScaleItem.scale_id == merged["scale_id"],
            m.GradingScaleItem.min_score <= merged["max_score"],
            m.GradingScaleItem.max_score >= merged["min_score"],
        )
        if record:
            overlap = overlap.where(m.GradingScaleItem.id != record.id)
        if db.scalar(overlap):
            raise HTTPException(409, "Grade ranges must not overlap.")
    if resource == "timetable":
        # Serialize schedule edits per school to prevent concurrent conflict checks from racing.
        db.scalar(select(m.School).where(m.School.id == user.school_id).with_for_update())
        if merged["start_time"] >= merged["end_time"]:
            raise HTTPException(422, "End time must follow start time.")
        conflict = select(m.TimetableEntry).where(
            m.TimetableEntry.school_id == user.school_id,
            m.TimetableEntry.weekday == merged["weekday"],
            m.TimetableEntry.start_time < merged["end_time"],
            m.TimetableEntry.end_time > merged["start_time"],
            or_(
                m.TimetableEntry.teacher_id == merged["teacher_id"],
                m.TimetableEntry.class_id == merged["class_id"],
                m.TimetableEntry.room == merged["room"],
            ),
        )
        if record:
            conflict = conflict.where(m.TimetableEntry.id != record.id)
        if db.scalar(conflict):
            raise HTTPException(409, "This time overlaps an existing teacher, class, or room booking.")
    if resource == "charges" and record:
        raise HTTPException(409, "Charges are immutable. Use a documented finance correction.")
    if resource == "staff" and record and "active" in values:
        account = scoped(db, m.User, record.user_id, user)
        if account.id == user.id or account.role in {"SCHOOL_OWNER", "PLATFORM_SUPER_ADMIN"}:
            raise HTTPException(403, "This account is protected.")
        account.active = values["active"]
        account.token_version += 1
        for token in db.scalars(select(m.AuthToken).where(m.AuthToken.user_id == account.id)):
            token.used = True
    if resource == "grading-scales" and values.get("active"):
        for scale in db.scalars(select(m.GradingScale).where(m.GradingScale.school_id == user.school_id)):
            scale.active = False


@router.get("/catalog")
def catalog(user=Depends(current_user)):
    from .core.security import permission_names

    result = {}
    for name, (model, domain, fields) in SPECS.items():
        can_read = (domain + ".view" in permission_names(user.role)) or (
            domain == "users" and "users.manage" in permission_names(user.role)
        )
        if not can_read:
            continue
        result[name] = {
            "fields": [],
            "can_create": bool(fields) and mutation_permission(name, True) in permission_names(user.role),
            "can_edit": bool(fields)
            and name != "charges"
            and mutation_permission(name, False) in permission_names(user.role),
        }
        for field in fields.split():
            if field == "password":
                result[name]["fields"].append({"name": field, "type": "password", "required": True})
                continue
            col = model.__table__.columns[field]
            target = next(
                (
                    next((k for k, (v, _, _) in SPECS.items() if v.__tablename__ == fk.column.table.name), None)
                    for fk in col.foreign_keys
                ),
                None,
            )
            kind = (
                "boolean"
                if isinstance(col.type, Boolean)
                else "number"
                if isinstance(col.type, (Integer, Numeric))
                else "date"
                if isinstance(col.type, Date)
                else "time"
                if isinstance(col.type, Time)
                else "text"
            )
            result[name]["fields"].append(
                {
                    "name": field,
                    "type": kind,
                    "required": not col.nullable and col.default is None,
                    "default": col.default.arg if col.default is not None and col.default.is_scalar else None,
                    "resource": target,
                    "options": OPTIONS.get(field),
                    "max_length": getattr(col.type, "length", None),
                }
            )
    return result


def resource_spec(resource):
    if resource not in SPECS:
        raise HTTPException(404, "Resource was not found.")
    return SPECS[resource]


def read_permission(user, domain):
    require(user, "users.manage" if domain == "users" else domain + ".view")


def base_query(db, user, model):
    query = select(model).where(model.school_id == user.school_id)
    if user.role == "TEACHER":
        classes = teacher_classes(db, user)
        if model is m.ClassArm:
            query = query.where(m.ClassArm.id.in_(classes))
        elif hasattr(model, "class_id"):
            query = query.where(model.class_id.in_(classes))
        if model is m.Result:
            assigned = (
                select(m.SubjectAssignment.id)
                .where(
                    m.SubjectAssignment.school_id == user.school_id,
                    m.SubjectAssignment.teacher_id == user.id,
                    m.SubjectAssignment.class_id == m.Result.class_id,
                    m.SubjectAssignment.subject_id == m.Result.subject_id,
                )
                .exists()
            )
            query = query.where(or_(m.Result.class_id.in_(class_teacher_ids(db, user)), assigned))
        if model is m.SubjectAssignment or model is m.TimetableEntry:
            query = query.where(model.teacher_id == user.id)
        if model is m.Subject:
            query = query.where(
                m.Subject.id.in_(
                    select(m.SubjectAssignment.subject_id).where(
                        m.SubjectAssignment.school_id == user.school_id, m.SubjectAssignment.teacher_id == user.id
                    )
                )
            )
        if model is m.User:
            query = query.where(m.User.id == user.id)
        if model is m.Document:
            query = query.where(
                m.Document.kind == "REPORT_CARD",
                m.Document.student_id.in_(
                    select(m.Student.id).where(
                        m.Student.school_id == user.school_id, m.Student.class_id.in_(class_teacher_ids(db, user))
                    )
                ),
            )
        if model in {m.Announcement, m.CalendarEvent}:
            query = audience_query(db, user, model)
    return query


@router.get("/records/{resource}")
def list_records(
    resource: str,
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    search: str = Query("", max_length=200),
    sort: str = "created_at",
    direction: Literal["asc", "desc"] = "desc",
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    model, domain, _ = resource_spec(resource)
    read_permission(user, domain)
    query = base_query(db, user, model)
    if search:
        searchable = [
            getattr(model, c.name).ilike("%" + search.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\")
            for c in model.__table__.columns
            if isinstance(c.type, String)
            and c.name
            in {
                "name",
                "first_name",
                "last_name",
                "middle_name",
                "student_code",
                "admission_no",
                "email",
                "phone",
                "receipt_number",
                "reference",
                "title",
                "description",
                "action",
                "status",
                "method",
            }
        ]
        if searchable:
            query = query.where(or_(*searchable))
    for field in (
        "class_id",
        "student_id",
        "parent_id",
        "term_id",
        "session_id",
        "subject_id",
        "status",
        "attendance_date",
        "payment_date",
        "teacher_id",
        "weekday",
        "room",
    ):
        if field in request.query_params and hasattr(model, field):
            val: Any = request.query_params[field]
            try:
                if field.endswith("_date"):
                    val = date.fromisoformat(val)
                elif field == "weekday":
                    val = int(val)
            except ValueError:
                raise HTTPException(422, f"Invalid {field}.")
            query = query.where(getattr(model, field) == val)
    if sort not in {c.name for c in model.__table__.columns} or sort in {"password_hash", "token_version"}:
        raise HTTPException(422, "Invalid sort field.")
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    order = getattr(model, sort)
    records = db.scalars(
        query.order_by(order.asc() if direction == "asc" else order.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [serialize_for(db, user, r) for r in records],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get("/lookups/{resource}")
def lookups(
    resource: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=100),
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    model, domain, _ = resource_spec(resource)
    if resource in READ_ONLY:
        raise HTTPException(404, "Lookup is not available.")
    if resource == "users":
        require(user, "academics.view")
    else:
        read_permission(user, domain)
    query = base_query(db, user, model)
    if resource == "users" and user.role not in {"SCHOOL_OWNER", "SCHOOL_ADMIN", "PLATFORM_SUPER_ADMIN"}:
        query = query.where(m.User.role == "TEACHER", m.User.active.is_(True))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = []
    for record in db.scalars(
        query.order_by(model.created_at, model.id).offset((page - 1) * page_size).limit(page_size)
    ):
        name = (
            getattr(record, "name", None)
            or getattr(record, "label", None)
            or getattr(record, "description", None)
            or record.id[:8]
        )
        if isinstance(record, m.Student):
            name = f"{record.first_name} {record.last_name} · {record.student_code}"
        elif isinstance(record, m.ClassArm):
            name = f"{db.get(m.ClassLevel, record.level_id).name} {record.name}"
        elif isinstance(record, m.Term):
            name = f"{db.get(m.AcademicSession, record.session_id).name} · {record.name}"
        item = {"id": record.id, "label": name}
        if isinstance(record, m.User):
            item["role"] = record.role
        items.append(item)
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.get("/records/{resource}/{record_id}")
def get_record(
    resource: str, record_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")
):
    model, domain, _ = resource_spec(resource)
    read_permission(user, domain)
    record = db.scalar(base_query(db, user, model).where(model.id == record_id))
    if not record:
        raise HTTPException(404, "Record was not found.")
    return serialize_for(db, user, record)


@router.post("/records/{resource}", status_code=201)
def create_record(
    resource: str, data: dict, user=Depends(current_user), db: Session = Depends(get_db, scope="function")
):
    model, _, fields = resource_spec(resource)
    if not fields:
        raise HTTPException(405, "Use the dedicated workflow for this resource.")
    require(user, mutation_permission(resource, True))
    try:
        values = schema_for(resource).model_validate(data).model_dump()
    except ValidationError as exc:
        raise HTTPException(422, "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors()))
    validate_business(db, user, resource, values)
    if resource == "students":
        from .students import generate_student_id

        values["student_code"] = generate_student_id()
    if resource == "announcements":
        values["created_by"] = user.id
    record = model(school_id=user.school_id, **values)
    db.add(record)
    db.flush()
    if resource == "users":
        assign_role(db, record)
    if resource == "announcements":
        from .notifications import broadcast_announcement

        broadcast_announcement(db, user, record)
    audit(db, user, resource + ".created", model.__tablename__, record.id)
    return serialize(record)


@router.patch("/records/{resource}/{record_id}")
def update_record(
    resource: str,
    record_id: str,
    data: dict,
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    model, _, fields = resource_spec(resource)
    if not fields:
        raise HTTPException(405, "Use the dedicated workflow for this resource.")
    require(user, mutation_permission(resource, False))
    record = scoped(db, model, record_id, user, lock=True)
    try:
        values = schema_for(resource, partial=True).model_validate(data).model_dump(exclude_unset=True)
    except ValidationError as exc:
        raise HTTPException(422, "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors()))
    validate_business(db, user, resource, values, record)
    before = {k: str(getattr(record, k, "")) for k in values if k != "password_hash"}
    for key, value in values.items():
        setattr(record, key, value)
    if resource == "users" and "role" in values:
        assign_role(db, record)
    audit(
        db,
        user,
        resource + ".updated",
        model.__tablename__,
        record.id,
        {"before": before, "fields": [k for k in values if k != "password_hash"]},
    )
    db.flush()
    return serialize(record)


@router.patch("/school")
def update_school(data: dict, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "school.manage_settings")
    from .auth import Onboard

    allowed = {
        "name",
        "email",
        "phone",
        "address",
        "website",
        "registration_number",
        "school_type",
        "primary_color",
        "secondary_color",
        "accent_color",
        "mandatory_notifications",
        "timezone",
    }
    if set(data) - allowed:
        raise HTTPException(422, "Unknown school setting.")
    validators = {
        name: (
            Onboard.model_fields[{"name": "school_name", "email": "school_email"}.get(name, name)].annotation,
            Onboard.model_fields[{"name": "school_name", "email": "school_email"}.get(name, name)],
        )
        for name in data
        if name not in {"timezone", "mandatory_notifications"}
    }
    try:
        validated = (
            create_model("SchoolSettings", **validators)
            .model_validate({k: v for k, v in data.items() if k != "timezone"})
            .model_dump()
        )
        if "timezone" in data:
            ZoneInfo(data["timezone"])
            validated["timezone"] = data["timezone"]
        if "mandatory_notifications" in data:
            prefs = data["mandatory_notifications"]
            if (
                not isinstance(prefs, dict)
                or set(prefs) - {"attendance", "results", "finance", "announcements", "email"}
                or any(type(v) is not bool for v in prefs.values())
            ):
                raise ValueError()
            validated["mandatory_notifications"] = prefs
    except (ValidationError, ZoneInfoNotFoundError, ValueError, TypeError):
        raise HTTPException(422, "Invalid school setting.")
    school = db.get(m.School, user.school_id)
    for key, value in validated.items():
        setattr(school, key, value)
    audit(db, user, "school.settings_updated", "schools", school.id, {"fields": list(data)})
    return serialize(school)


@router.get("/platform/schools")
def platform_schools(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    if user.role != "PLATFORM_SUPER_ADMIN":
        raise HTTPException(403, "Platform administrator access is required.")
    return [serialize(s) for s in db.scalars(select(m.School).where(m.School.school_type != "PLATFORM").order_by(m.School.created_at.desc()))]


@router.post("/platform/schools/{school_id}/{action}")
def platform_status(
    school_id: str,
    action: Literal["approve", "suspend"],
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    if user.role != "PLATFORM_SUPER_ADMIN":
        raise HTTPException(403, "Platform administrator access is required.")
    school = db.get(m.School, school_id)
    if not school:
        raise HTTPException(404, "School was not found.")
    if school.id == user.school_id and action == "suspend":
        raise HTTPException(422, "Cannot suspend the platform administrator school.")
    school.status = "ACTIVE" if action == "approve" else "SUSPENDED"
    audit(db, user, f"platform.{action}", "schools", school.id)
    return serialize(school)
