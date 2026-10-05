import secrets
from datetime import timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select, or_
from sqlalchemy.orm import Session
from .core.database import get_db
from .core.config import get_settings
from .core.models import now
from .core.security import (
    passwords,
    current_user,
    issue_tokens,
    token_hash,
    audit,
    seed_roles,
    assign_role,
    permission_names,
)
from .models import (
    School,
    User,
    AcademicSession,
    Term,
    AuthToken,
    AssessmentComponent,
    GradingScale,
    GradingScaleItem,
    Notification,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(StrictModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class Onboard(Login):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(min_length=2, max_length=180)
    school_name: str = Field(min_length=2, max_length=180)
    school_email: EmailStr
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=2000)
    website: str = Field(default="", max_length=300, pattern=r"^(https?://[^\s]+)?$")
    registration_number: str = Field(default="", max_length=80)
    school_type: Literal["PRIMARY", "SECONDARY", "COMBINED"] = "COMBINED"
    primary_color: str = Field(default="#156b55", pattern=r"^#[0-9a-fA-F]{6}$")
    secondary_color: str = Field(default="#d4a843", pattern=r"^#[0-9a-fA-F]{6}$")
    accent_color: str = Field(default="#5279b8", pattern=r"^#[0-9a-fA-F]{6}$")
    session_name: str = Field(default="2026/2027", min_length=2, max_length=80)
    term_name: str = Field(default="First term", min_length=2, max_length=80)


class TokenInput(StrictModel):
    token: str = Field(min_length=20, max_length=300)


class ResetInput(TokenInput):
    password: str = Field(min_length=12, max_length=128)


class EmailInput(StrictModel):
    email: EmailStr


def create_school(db, data):
    if db.scalar(select(User).where(User.email == str(data.email).lower())):
        raise HTTPException(409, "An account with this email already exists.")
    seed_roles(db)
    school = School(
        name=data.school_name,
        email=str(data.school_email).lower(),
        phone=data.phone,
        address=data.address,
        website=data.website,
        registration_number=data.registration_number,
        school_type=data.school_type,
        primary_color=data.primary_color,
        secondary_color=data.secondary_color,
        accent_color=data.accent_color,
        status="PENDING" if get_settings().require_school_approval else "ACTIVE",
    )
    db.add(school)
    db.flush()
    user = User(
        school_id=school.id,
        name=data.name,
        email=str(data.email).lower(),
        password_hash=passwords.hash(data.password),
        role="SCHOOL_OWNER",
    )
    session = AcademicSession(school_id=school.id, name=data.session_name)
    db.add_all([user, session])
    db.flush()
    assign_role(db, user)
    db.add(Term(school_id=school.id, session_id=session.id, name=data.term_name))
    from .curriculum import seed_subjects

    seed_subjects(db, school)
    for name, weight in [("CA 1", 20), ("CA 2", 20), ("Exam", 60)]:
        db.add(AssessmentComponent(school_id=school.id, name=name, weight=weight, max_score=weight))
    scale = GradingScale(school_id=school.id, name="Default grading")
    db.add(scale)
    db.flush()
    for label, minimum, maximum, remark in [
        ("A", 70, 100, "Excellent"),
        ("B", 60, 69.99, "Very good"),
        ("C", 50, 59.99, "Good"),
        ("D", 45, 49.99, "Fair"),
        ("E", 40, 44.99, "Pass"),
        ("F", 0, 39.99, "Needs improvement"),
    ]:
        db.add(
            GradingScaleItem(
                school_id=school.id,
                scale_id=scale.id,
                label=label,
                min_score=minimum,
                max_score=maximum,
                remark=remark,
                passing=label != "F",
            )
        )
    audit(db, user, "school.onboarded", "schools", school.id)
    return user


@router.post("/onboard", status_code=201)
def onboard(data: Onboard, db: Session = Depends(get_db, scope="function")):
    if not get_settings().allow_school_registration:
        raise HTTPException(403, "School registration is currently closed.")
    user = create_school(db, data)
    return {**issue_tokens(db, user), "school_status": db.get(School, user.school_id).status}


DUMMY_HASH = passwords.hash("constant-timing-placeholder-password")


@router.post("/login")
def login(data: Login, db: Session = Depends(get_db, scope="function")):
    user = db.scalar(
        select(User).where(or_(User.email == str(data.email).lower(), User.username == str(data.email).lower()))
    )
    valid = passwords.verify(data.password, user.password_hash if user else DUMMY_HASH)
    if not user or not valid or not user.active:
        raise HTTPException(401, "Invalid email or password.")
    if (
        user.must_change_password
        and user.temporary_password_expires_at
        and user.temporary_password_expires_at.replace(tzinfo=timezone.utc) <= now()
    ):
        raise HTTPException(
            401,
            {
                "code": "INVALID_TEMPORARY_PASSWORD",
                "message": "Your temporary password has expired. Request a new invitation or reset your password.",
            },
        )
    school = db.get(School, user.school_id)
    if school.status != "ACTIVE" and user.role != "PLATFORM_SUPER_ADMIN":
        raise HTTPException(403, "Your school is awaiting approval or has been suspended.")
    audit(db, user, "auth.login", "users", user.id)
    return issue_tokens(db, user)


@router.get("/me")
def me(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    from .resources import serialize

    return {
        "user": serialize(user),
        "school": serialize(db.get(School, user.school_id)),
        "permissions": permission_names(user.role),
    }


@router.post("/refresh")
def refresh(data: TokenInput, db: Session = Depends(get_db, scope="function")):
    stored = db.scalar(
        select(AuthToken)
        .where(AuthToken.token_hash == token_hash(data.token), AuthToken.kind == "refresh")
        .with_for_update()
    )
    if not stored or stored.used or stored.expires_at.replace(tzinfo=timezone.utc) < now():
        raise HTTPException(401, "Refresh token is invalid or expired.")
    user = db.get(User, stored.user_id)
    if not user.active or (db.get(School, user.school_id).status != "ACTIVE" and user.role != "PLATFORM_SUPER_ADMIN"):
        raise HTTPException(401, "Account is unavailable.")
    stored.used = True
    return issue_tokens(db, user)


@router.post("/logout")
def logout(user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    user.token_version += 1
    for token in db.scalars(select(AuthToken).where(AuthToken.user_id == user.id, AuthToken.used.is_(False))):
        token.used = True
    audit(db, user, "auth.logout", "users", user.id)
    return {"message": "Signed out on all devices."}


@router.post("/forgot-password")
def forgot(data: EmailInput, db: Session = Depends(get_db, scope="function")):
    user = db.scalar(select(User).where(User.email == str(data.email).lower(), User.active.is_(True)))
    if user:
        token = secrets.token_urlsafe(48)
        db.add(
            AuthToken(
                school_id=user.school_id,
                user_id=user.id,
                token_hash=token_hash(token),
                kind="reset",
                expires_at=now() + timedelta(minutes=30),
            )
        )
        db.add(
            Notification(
                school_id=user.school_id,
                channel="EMAIL",
                recipient=user.email,
                subject="Reset your ile-iwe password",
                body=f"Reset your password: {get_settings().frontend_url}/reset-password?token={token}\nThis link expires in 30 minutes.",
                event_key=f"reset:{secrets.token_hex(16)}",
                available_at=now(),
            )
        )
    return {"message": "If the account exists, a password reset email has been queued."}


@router.post("/reset-password")
def reset(data: ResetInput, db: Session = Depends(get_db, scope="function")):
    token = db.scalar(
        select(AuthToken)
        .where(AuthToken.token_hash == token_hash(data.token), AuthToken.kind == "reset")
        .with_for_update()
    )
    if not token or token.used or token.expires_at.replace(tzinfo=timezone.utc) < now():
        raise HTTPException(400, "Reset link is invalid or expired.")
    user = db.get(User, token.user_id)
    user.password_hash = passwords.hash(data.password)
    user.must_change_password = False
    user.temporary_password_expires_at = None
    user.credential_nonce = None
    user.token_version += 1
    for stored in db.scalars(select(AuthToken).where(AuthToken.user_id == user.id)):
        stored.used = True
    audit(db, user, "auth.password_reset", "users", user.id)
    return {"message": "Password updated. You can now sign in."}


class ChangePassword(StrictModel):
    current_password: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=12, max_length=128)


@router.post("/change-password")
def change_password(data: ChangePassword, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    user = db.scalar(select(User).where(User.id == user.id).with_for_update())
    if not passwords.verify(data.current_password, user.password_hash):
        raise HTTPException(400, "Your current password is incorrect.")
    if data.password == data.current_password:
        raise HTTPException(422, "Choose a different password.")
    user.password_hash = passwords.hash(data.password)
    user.must_change_password = False
    user.temporary_password_expires_at = None
    user.credential_nonce = None
    user.token_version += 1
    for token in db.scalars(select(AuthToken).where(AuthToken.user_id == user.id)):
        token.used = True
    audit(db, user, "PASSWORD_CHANGED", "users", user.id)
    return issue_tokens(db, user)
