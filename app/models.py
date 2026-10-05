"""Relational domain models; tenant references are additionally checked by services."""

from datetime import date, datetime, time
from decimal import Decimal
from sqlalchemy import String, Text, ForeignKey, UniqueConstraint, CheckConstraint, Numeric, JSON, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from .core.models import Record, TenantRecord


class School(Record):
    __tablename__ = "schools"
    name: Mapped[str] = mapped_column(String(180))
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(40), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    website: Mapped[str] = mapped_column(String(300), default="")
    registration_number: Mapped[str] = mapped_column(String(80), default="")
    school_type: Mapped[str] = mapped_column(String(20), default="COMBINED")
    primary_color: Mapped[str] = mapped_column(String(7), default="#156b55")
    secondary_color: Mapped[str] = mapped_column(String(7), default="#d4a843")
    accent_color: Mapped[str] = mapped_column(String(7), default="#5279b8")
    mandatory_notifications: Mapped[dict] = mapped_column(JSON, default=dict)
    logo_key: Mapped[str | None] = mapped_column(String(500))
    timezone: Mapped[str] = mapped_column(String(80), default="Africa/Lagos")
    currency: Mapped[str] = mapped_column(String(3), default="NGN")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")


class User(TenantRecord):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(180))
    password_hash: Mapped[str] = mapped_column(String(300))
    role: Mapped[str] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(default=True)
    token_version: Mapped[int] = mapped_column(default=0)
    username: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    must_change_password: Mapped[bool] = mapped_column(default=False)
    temporary_password_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credential_nonce: Mapped[str | None] = mapped_column(String(100))


class Role(Record):
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(String(40), unique=True)


class Permission(Record):
    __tablename__ = "permissions"
    name: Mapped[str] = mapped_column(String(80), unique=True)


class RolePermission(Record):
    __tablename__ = "role_permissions"
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    permission_id: Mapped[str] = mapped_column(ForeignKey("permissions.id"))
    __table_args__ = (UniqueConstraint("role_id", "permission_id"),)


class UserRole(TenantRecord):
    __tablename__ = "user_roles"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role_id: Mapped[str] = mapped_column(ForeignKey("roles.id"))
    __table_args__ = (UniqueConstraint("user_id", "role_id"),)


class AuthToken(TenantRecord):
    __tablename__ = "auth_tokens"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(20))
    expires_at: Mapped[datetime]
    used: Mapped[bool] = mapped_column(default=False)


class AcademicSession(TenantRecord):
    __tablename__ = "academic_sessions"
    name: Mapped[str] = mapped_column(String(80))
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (UniqueConstraint("school_id", "name"),)


class Term(TenantRecord):
    __tablename__ = "terms"
    session_id: Mapped[str] = mapped_column(ForeignKey("academic_sessions.id"))
    name: Mapped[str] = mapped_column(String(80))
    start_date: Mapped[date | None]
    end_date: Mapped[date | None]
    active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (UniqueConstraint("session_id", "name"),)


class ClassLevel(TenantRecord):
    __tablename__ = "class_levels"
    name: Mapped[str] = mapped_column(String(80))
    sort_order: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("school_id", "name"),)


class ClassArm(TenantRecord):
    __tablename__ = "class_arms"
    level_id: Mapped[str] = mapped_column(ForeignKey("class_levels.id"))
    name: Mapped[str] = mapped_column(String(80))
    teacher_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (UniqueConstraint("school_id", "level_id", "name"),)


class Subject(TenantRecord):
    __tablename__ = "subjects"
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(30))
    category: Mapped[str] = mapped_column(String(80), default="General")
    school_level: Mapped[str] = mapped_column(String(20), default="PRIMARY")
    compulsory: Mapped[bool] = mapped_column(default=True)
    active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (UniqueConstraint("school_id", "code"),)


class SubjectAssignment(TenantRecord):
    __tablename__ = "subject_assignments"
    class_id: Mapped[str] = mapped_column(ForeignKey("class_arms.id"))
    subject_id: Mapped[str] = mapped_column(ForeignKey("subjects.id"))
    teacher_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (UniqueConstraint("school_id", "class_id", "subject_id"),)


class Student(TenantRecord):
    __tablename__ = "students"
    student_code: Mapped[str] = mapped_column(String(60))
    admission_no: Mapped[str | None] = mapped_column(String(80))
    first_name: Mapped[str] = mapped_column(String(80))
    middle_name: Mapped[str] = mapped_column(String(80), default="")
    last_name: Mapped[str] = mapped_column(String(80))
    date_of_birth: Mapped[date | None]
    gender: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    emergency_info: Mapped[str] = mapped_column(Text, default="")
    admission_date: Mapped[date | None]
    class_id: Mapped[str | None] = mapped_column(ForeignKey("class_arms.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    photo_key: Mapped[str | None] = mapped_column(String(500))
    qr_version: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        UniqueConstraint("school_id", "student_code"),
        UniqueConstraint("school_id", "admission_no"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE','WITHDRAWN','GRADUATED','TRANSFERRED')"),
    )


class Parent(TenantRecord):
    __tablename__ = "parents"
    name: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    notify_email: Mapped[bool] = mapped_column(default=True)
    notify_sms: Mapped[bool] = mapped_column(default=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), unique=True)
    notification_preferences: Mapped[dict] = mapped_column(JSON, default=dict)


class StudentParent(TenantRecord):
    __tablename__ = "student_parents"
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"))
    parent_id: Mapped[str] = mapped_column(ForeignKey("parents.id"))
    relationship: Mapped[str] = mapped_column(String(50), default="Guardian")
    primary_contact: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("student_id", "parent_id"),)


class Staff(TenantRecord):
    __tablename__ = "staff"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), unique=True)
    name: Mapped[str] = mapped_column(String(180))
    phone: Mapped[str] = mapped_column(String(40), default="")
    job_title: Mapped[str] = mapped_column(String(100), default="Teacher")
    employment_date: Mapped[date | None]
    active: Mapped[bool] = mapped_column(default=True)
    staff_code: Mapped[str | None] = mapped_column(String(80))
    first_name: Mapped[str] = mapped_column(String(80), default="")
    last_name: Mapped[str] = mapped_column(String(80), default="")
    gender: Mapped[str] = mapped_column(String(30), default="")
    date_of_birth: Mapped[date | None]
    department: Mapped[str] = mapped_column(String(100), default="")
    staff_type: Mapped[str] = mapped_column(String(80), default="Teaching")
    address: Mapped[str] = mapped_column(Text, default="")
    emergency_contact: Mapped[str] = mapped_column(String(300), default="")
    photo_key: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (UniqueConstraint("school_id", "staff_code"),)


class Attendance(TenantRecord):
    __tablename__ = "attendance"
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    class_id: Mapped[str] = mapped_column(ForeignKey("class_arms.id"))
    term_id: Mapped[str] = mapped_column(ForeignKey("terms.id"))
    attendance_date: Mapped[date] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(20))
    marked_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source: Mapped[str] = mapped_column(String(20), default="MANUAL")
    note: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        UniqueConstraint("student_id", "attendance_date", "term_id"),
        CheckConstraint("status IN ('PRESENT','ABSENT','LATE','EXCUSED')"),
    )


class AssessmentComponent(TenantRecord):
    __tablename__ = "assessment_components"
    name: Mapped[str] = mapped_column(String(80))
    weight: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    max_score: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    active: Mapped[bool] = mapped_column(default=True)
    __table_args__ = (
        UniqueConstraint("school_id", "name"),
        CheckConstraint("weight > 0 AND weight <= 100 AND max_score > 0"),
    )


class GradingScale(TenantRecord):
    __tablename__ = "grading_scales"
    name: Mapped[str] = mapped_column(String(80))
    active: Mapped[bool] = mapped_column(default=True)


class GradingScaleItem(TenantRecord):
    __tablename__ = "grading_scale_items"
    scale_id: Mapped[str] = mapped_column(ForeignKey("grading_scales.id"))
    label: Mapped[str] = mapped_column(String(20))
    min_score: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    max_score: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    remark: Mapped[str] = mapped_column(String(180), default="")
    passing: Mapped[bool] = mapped_column(default=True)
    point_value: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    __table_args__ = (CheckConstraint("min_score >= 0 AND max_score <= 100 AND min_score <= max_score"),)


class Result(TenantRecord):
    __tablename__ = "results"
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    class_id: Mapped[str] = mapped_column(ForeignKey("class_arms.id"))
    subject_id: Mapped[str] = mapped_column(ForeignKey("subjects.id"))
    term_id: Mapped[str] = mapped_column(ForeignKey("terms.id"))
    total: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    passing: Mapped[bool] = mapped_column(default=False)
    grade: Mapped[str] = mapped_column(String(20))
    remark: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    entered_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    __table_args__ = (
        UniqueConstraint("student_id", "subject_id", "term_id"),
        CheckConstraint("total >= 0 AND total <= 100"),
        CheckConstraint("status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','APPROVED','PUBLISHED')"),
    )


class ResultScore(TenantRecord):
    __tablename__ = "result_scores"
    result_id: Mapped[str] = mapped_column(ForeignKey("results.id"))
    component_id: Mapped[str] = mapped_column(ForeignKey("assessment_components.id"))
    score: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    max_score: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    weight: Mapped[Decimal] = mapped_column(Numeric(6, 2))
    __table_args__ = (
        UniqueConstraint("result_id", "component_id"),
        CheckConstraint("score >= 0 AND score <= max_score"),
    )


class TimetableEntry(TenantRecord):
    __tablename__ = "timetable_entries"
    class_id: Mapped[str] = mapped_column(ForeignKey("class_arms.id"))
    subject_id: Mapped[str] = mapped_column(ForeignKey("subjects.id"))
    teacher_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    room: Mapped[str] = mapped_column(String(80))
    weekday: Mapped[int]
    start_time: Mapped[time]
    end_time: Mapped[time]
    published: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (CheckConstraint("weekday >= 0 AND weekday <= 6"), CheckConstraint("start_time < end_time"))


class FeeStructure(TenantRecord):
    __tablename__ = "fee_structures"
    name: Mapped[str] = mapped_column(String(120))
    term_id: Mapped[str] = mapped_column(ForeignKey("terms.id"))
    class_id: Mapped[str] = mapped_column(ForeignKey("class_arms.id"))


class FeeItem(TenantRecord):
    __tablename__ = "fee_items"
    structure_id: Mapped[str] = mapped_column(ForeignKey("fee_structures.id"))
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    __table_args__ = (CheckConstraint("amount > 0"), UniqueConstraint("structure_id", "name"))


class StudentCharge(TenantRecord):
    __tablename__ = "student_charges"
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    term_id: Mapped[str] = mapped_column(ForeignKey("terms.id"))
    fee_item_id: Mapped[str | None] = mapped_column(ForeignKey("fee_items.id"))
    description: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=0)
    __table_args__ = (
        CheckConstraint("amount > 0 AND discount >= 0 AND discount <= amount"),
        UniqueConstraint("student_id", "term_id", "fee_item_id"),
    )


class Payment(TenantRecord):
    __tablename__ = "payments"
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    term_id: Mapped[str] = mapped_column(ForeignKey("terms.id"))
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    payment_date: Mapped[date]
    method: Mapped[str] = mapped_column(String(40))
    reference: Mapped[str] = mapped_column(String(120), default="")
    receipt_number: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str] = mapped_column(String(120))
    recorded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    previous_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    new_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    reversed: Mapped[bool] = mapped_column(default=False)
    reversal_reason: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        UniqueConstraint("school_id", "receipt_number"),
        UniqueConstraint("school_id", "idempotency_key"),
        CheckConstraint("amount > 0"),
    )


class NotificationTemplate(TenantRecord):
    __tablename__ = "notification_templates"
    name: Mapped[str] = mapped_column(String(120))
    event: Mapped[str] = mapped_column(String(50))
    channel: Mapped[str] = mapped_column(String(10))
    body: Mapped[str] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("school_id", "event", "channel"),)


class Notification(TenantRecord):
    __tablename__ = "notifications"
    channel: Mapped[str] = mapped_column(String(10))
    recipient: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(200), default="ile-iwe update")
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    event_key: Mapped[str] = mapped_column(String(250), unique=True)
    provider_id: Mapped[str | None] = mapped_column(String(200))
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"))
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(String(300))


class Document(TenantRecord):
    __tablename__ = "documents"
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"))
    term_id: Mapped[str] = mapped_column(ForeignKey("terms.id"))
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"))
    kind: Mapped[str] = mapped_column(String(30))
    format: Mapped[str] = mapped_column(String(10), default="pdf")
    status: Mapped[str] = mapped_column(String(20), default="QUEUED", index=True)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    email_parents: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime]
    last_error: Mapped[str | None] = mapped_column(String(300))


class Announcement(TenantRecord):
    __tablename__ = "announcements"
    title: Mapped[str] = mapped_column(String(180))
    body: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    audience: Mapped[str] = mapped_column(String(20), default="ALL")
    class_id: Mapped[str | None] = mapped_column(ForeignKey("class_arms.id"))


class CalendarEvent(TenantRecord):
    __tablename__ = "calendar_events"
    title: Mapped[str] = mapped_column(String(180))
    body: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[date] = mapped_column(index=True)
    end_date: Mapped[date]
    audience: Mapped[str] = mapped_column(String(20), default="ALL")
    class_id: Mapped[str | None] = mapped_column(ForeignKey("class_arms.id"))
    __table_args__ = (CheckConstraint("end_date >= start_date"),)


class InboxItem(TenantRecord):
    __tablename__ = "inbox_items"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    student_id: Mapped[str | None] = mapped_column(ForeignKey("students.id"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(String(50))
    event_key: Mapped[str] = mapped_column(String(250), unique=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(TenantRecord):
    __tablename__ = "audit_logs"
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[str] = mapped_column(String(36))
    details: Mapped[dict] = mapped_column(JSON, default=dict)


# Enforce tenant consistency in PostgreSQL/SQLite as well as in every API service.
# This also protects imports and future background tasks that bypass HTTP validation.
from sqlalchemy import ForeignKeyConstraint  # noqa: E402
from .core.database import Base  # noqa: E402

for _table in Base.metadata.tables.values():
    if "school_id" not in _table.c:
        continue
    if not any(
        isinstance(c, UniqueConstraint) and [x.name for x in c.columns] == ["school_id", "id"]
        for c in _table.constraints
    ):
        _table.append_constraint(UniqueConstraint("school_id", "id", name=f"uq_{_table.name}_tenant_id"))
for _table in Base.metadata.tables.values():
    if "school_id" not in _table.c:
        continue
    for _fk in list(_table.foreign_keys):
        _target = _fk.column.table
        if _fk.parent.name != "school_id" and "school_id" in _target.c:
            _table.append_constraint(
                ForeignKeyConstraint(
                    ["school_id", _fk.parent.name],
                    [f"{_target.name}.school_id", f"{_target.name}.id"],
                    name=f"fk_{_table.name}_{_fk.parent.name}_tenant",
                )
            )
