"""Atomic staff/parent onboarding using the existing identity and durable mail outbox."""

import hashlib
import hmac
import re
import secrets
import unicodedata
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from . import models as m
from .auth import StrictModel
from .core.config import get_settings
from .core.database import get_db
from .core.models import now
from .core.security import current_user, require, passwords, assign_role, audit
from .resources import scoped, serialize

router = APIRouter(prefix="/accounts", tags=["Account onboarding"])


def temporary_password(nonce):
    # Random 256-bit nonce + a server-held secret. Neither the password nor a
    # decryptable password is persisted in the mail queue or returned by the API.
    return (
        hmac.new(get_settings().jwt_secret_key.encode(), ("invitation:" + nonce).encode(), hashlib.sha256).hexdigest()[
            :32
        ]
        + "!aA"
    )


def generate_username(db, name):
    # Serialize creation globally because login identity is globally unique.
    db.scalar(select(m.Role).where(m.Role.name == "PARENT").with_for_update())
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", ".", ascii_name).strip(".")[:70] or "member"
    username, suffix = base, 1
    while db.scalar(select(m.User.id).where(m.User.username == username)):
        suffix += 1
        username = f"{base}{suffix}"
    return username


def invite(db, actor, account):
    if not account.active:
        raise HTTPException(409, "Activate this account before sending an invitation.")
    account.credential_nonce = secrets.token_hex(32)
    account.password_hash = passwords.hash(temporary_password(account.credential_nonce))
    account.must_change_password = True
    account.temporary_password_expires_at = now() + timedelta(hours=get_settings().temporary_password_expire_hours)
    account.token_version += 1
    for token in db.scalars(select(m.AuthToken).where(m.AuthToken.user_id == account.id)):
        token.used = True
    for job in db.scalars(
        select(m.Notification).where(
            m.Notification.school_id == account.school_id,
            m.Notification.event_key.like(f"invite:{account.id}:%"),
            m.Notification.status.in_(["QUEUED", "FAILED"]),
        )
    ):
        job.status = "CANCELLED"
    if not account.username:
        account.username = generate_username(db, account.name)
    db.add(
        m.Notification(
            school_id=account.school_id,
            channel="EMAIL",
            recipient=account.email,
            subject="Your school account invitation",
            body="Account invitation (content hidden)",
            event_key=f"invite:{account.id}:{account.token_version}",
            available_at=now(),
        )
    )
    audit(db, actor, "CREDENTIAL_INVITATION_QUEUED", "users", account.id)


class AssignmentInput(StrictModel):
    class_id: str
    subject_id: str


class AssignmentsInput(StrictModel):
    subjects: list[AssignmentInput] = Field(default_factory=list, max_length=200)
    class_teacher_ids: list[str] = Field(default_factory=list, max_length=100)


class StaffInput(AssignmentsInput):
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    phone: str = Field(default="", max_length=40)
    staff_code: str | None = Field(default=None, max_length=80)
    gender: Literal["", "Female", "Male", "Prefer not to say"] = ""
    date_of_birth: date | None = None
    employment_date: date | None = None
    department: str = Field(default="", max_length=100)
    job_title: str = Field(default="Teacher", min_length=1, max_length=100)
    staff_type: str = Field(default="Teaching", max_length=80)
    role: Literal["SCHOOL_ADMIN", "TEACHER", "ACCOUNTANT", "STAFF"] = "TEACHER"
    active: bool = True


class ChildLink(StrictModel):
    student_id: str
    relationship: str = Field(default="Guardian", min_length=1, max_length=50)
    primary_contact: bool = False


class ParentInput(StrictModel):
    name: str = Field(min_length=2, max_length=180)
    email: EmailStr
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=2000)
    notify_email: bool = True
    children: list[ChildLink] = Field(min_length=1, max_length=100)


def new_account(db, actor, name, email, role, active=True):
    if db.scalar(select(m.User.id).where(m.User.email == str(email).lower())):
        raise HTTPException(409, "An account with this email already exists. Link the existing account instead.")
    account = m.User(
        school_id=actor.school_id,
        name=name,
        email=str(email).lower(),
        role=role,
        username=generate_username(db, name),
        password_hash=passwords.hash(secrets.token_urlsafe(48)),
        active=active,
    )
    db.add(account)
    db.flush()
    assign_role(db, account)
    if active:
        invite(db, actor, account)
    else:
        account.must_change_password = True
    return account


def save_assignments(db, actor, teacher, data):
    if teacher.role != "TEACHER" or not teacher.active:
        if data.subjects or data.class_teacher_ids:
            raise HTTPException(422, "Assignments require an active teacher account.")
    db.scalar(select(m.School).where(m.School.id == actor.school_id).with_for_update())
    pairs = {(s.class_id, s.subject_id) for s in data.subjects}
    if len(pairs) != len(data.subjects):
        raise HTTPException(422, "Duplicate class/subject assignment.")
    for class_id in {s.class_id for s in data.subjects} | set(data.class_teacher_ids):
        arm = scoped(db, m.ClassArm, class_id, actor)
        if class_id in data.class_teacher_ids and arm.teacher_id not in {None, teacher.id}:
            raise HTTPException(409, "A selected class already has a class teacher. Reassign it under Academics first.")
    for item in data.subjects:
        scoped(db, m.Subject, item.subject_id, actor)
        other = db.scalar(
            select(m.SubjectAssignment).where(
                m.SubjectAssignment.school_id == actor.school_id,
                m.SubjectAssignment.class_id == item.class_id,
                m.SubjectAssignment.subject_id == item.subject_id,
            )
        )
        if other and other.teacher_id != teacher.id:
            raise HTTPException(409, "A selected subject/class is already assigned to another teacher.")
    for old in db.scalars(
        select(m.SubjectAssignment).where(
            m.SubjectAssignment.school_id == actor.school_id, m.SubjectAssignment.teacher_id == teacher.id
        )
    ):
        if (old.class_id, old.subject_id) not in pairs:
            db.delete(old)
        else:
            pairs.remove((old.class_id, old.subject_id))
    db.add_all(
        m.SubjectAssignment(school_id=actor.school_id, teacher_id=teacher.id, class_id=c, subject_id=s)
        for c, s in pairs
    )
    for arm in db.scalars(select(m.ClassArm).where(m.ClassArm.school_id == actor.school_id)):
        if arm.id in data.class_teacher_ids:
            arm.teacher_id = teacher.id
        elif arm.teacher_id == teacher.id:
            arm.teacher_id = None
    audit(db, actor, "STAFF_ASSIGNMENT_CHANGED", "users", teacher.id, data.model_dump())


@router.post("/staff", status_code=201)
def create_staff(data: StaffInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "staff.manage")
    require(user, "users.manage")
    if data.date_of_birth and data.date_of_birth > date.today():
        raise HTTPException(422, "Date of birth cannot be in the future.")
    account = new_account(db, user, f"{data.first_name} {data.last_name}", data.email, data.role, data.active)
    staff = m.Staff(
        school_id=user.school_id,
        user_id=account.id,
        name=account.name,
        **data.model_dump(exclude={"email", "role", "subjects", "class_teacher_ids"}),
    )
    db.add(staff)
    save_assignments(db, user, account, data)
    db.flush()
    audit(db, user, "STAFF_ACCOUNT_CREATED", "staff", staff.id)
    return {"staff": serialize(staff), "user": serialize(account), "invitation_queued": account.active}


@router.post("/parents", status_code=201)
def create_parent(data: ParentInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "parents.manage")
    require(user, "users.manage")
    for child in data.children:
        scoped(db, m.Student, child.student_id, user)
    account = new_account(db, user, data.name, data.email, "PARENT")
    parent = m.Parent(school_id=user.school_id, user_id=account.id, **data.model_dump(exclude={"children"}))
    db.add(parent)
    db.flush()
    for child in data.children:
        db.add(m.StudentParent(school_id=user.school_id, parent_id=parent.id, **child.model_dump()))
        audit(db, user, "PARENT_STUDENT_LINKED", "students", child.student_id, {"parent_id": parent.id})
    audit(db, user, "PARENT_ACCOUNT_CREATED", "parents", parent.id)
    return {"parent": serialize(parent), "user": serialize(account), "invitation_queued": True}


@router.post("/parents/{parent_id}/activate", status_code=201)
def activate_parent(parent_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "parents.manage")
    require(user, "users.manage")
    parent = scoped(db, m.Parent, parent_id, user, lock=True)
    if parent.user_id:
        raise HTTPException(409, "This parent already has an account. Use resend invitation.")
    from pydantic import TypeAdapter, ValidationError

    try:
        TypeAdapter(EmailStr).validate_python(parent.email)
    except ValidationError:
        raise HTTPException(422, "Save a valid email address for this parent first.")
    account = new_account(db, user, parent.name, parent.email, "PARENT")
    parent.user_id = account.id
    audit(db, user, "PARENT_ACCOUNT_CREATED", "parents", parent.id)
    return serialize(account)


@router.get("/{user_id}/assignments")
def assignments(user_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "staff.manage")
    teacher = scoped(db, m.User, user_id, user)
    return {
        "subjects": [
            {"class_id": a.class_id, "subject_id": a.subject_id}
            for a in db.scalars(
                select(m.SubjectAssignment).where(
                    m.SubjectAssignment.school_id == user.school_id, m.SubjectAssignment.teacher_id == teacher.id
                )
            )
        ],
        "class_teacher_ids": list(
            db.scalars(
                select(m.ClassArm.id).where(m.ClassArm.school_id == user.school_id, m.ClassArm.teacher_id == teacher.id)
            )
        ),
    }


@router.post("/{user_id}/assignments")
def update_assignments(
    user_id: str, data: AssignmentsInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")
):
    require(user, "staff.manage")
    teacher = scoped(db, m.User, user_id, user, lock=True)
    save_assignments(db, user, teacher, data)
    return {"message": "Teacher assignments saved."}


@router.post("/{user_id}/invite", status_code=202)
def resend(user_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "users.manage")
    account = scoped(db, m.User, user_id, user, lock=True)
    if account.id == user.id or account.role in {"SCHOOL_OWNER", "PLATFORM_SUPER_ADMIN"}:
        raise HTTPException(403, "Use the password reset workflow for this account.")
    invite(db, user, account)
    return {"message": "A new invitation is queued. Previous credentials and sessions are invalid."}


@router.post("/relationships/{link_id}/unlink")
def unlink(link_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "parents.manage")
    link = scoped(db, m.StudentParent, link_id, user, lock=True)
    audit(db, user, "PARENT_STUDENT_UNLINKED", "students", link.student_id, {"parent_id": link.parent_id})
    db.delete(link)
    return {"message": "Parent relationship removed."}
