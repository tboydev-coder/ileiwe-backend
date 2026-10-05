import hashlib
import secrets
from datetime import timedelta, timezone
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.orm import Session
from .config import get_settings
from .database import get_db
from .models import now
from ..models import User, School, AuthToken, AuditLog, Role, Permission, RolePermission, UserRole

passwords = PasswordHash.recommended()
bearer = HTTPBearer(auto_error=False)
ADMIN = {"SCHOOL_OWNER", "SCHOOL_ADMIN", "PLATFORM_SUPER_ADMIN"}
PERMISSIONS = {
    "PRINCIPAL": {
        "students.view",
        "students.update",
        "parents.view",
        "staff.view",
        "academics.view",
        "academics.manage",
        "attendance.view",
        "attendance.mark",
        "attendance.correct",
        "results.view",
        "results.enter",
        "results.review",
        "results.publish",
        "finance.view",
        "notifications.view",
        "notifications.send_email",
        "reports.view",
        "audit.view",
    },
    "TEACHER": {
        "students.view",
        "academics.view",
        "attendance.view",
        "attendance.mark",
        "results.view",
        "results.enter",
        "reports.view",
    },
    "ACCOUNTANT": {
        "students.view",
        "parents.view",
        "academics.view",
        "finance.view",
        "finance.manage_fees",
        "finance.record_payment",
        "finance.generate_receipt",
        "notifications.view",
        "notifications.send_email",
        "reports.view",
    },
    "STAFF": {"students.view", "parents.view", "academics.view", "attendance.view", "notifications.view"},
    "PARENT": {"portal.view"},
}
ALL_PERMISSIONS = set().union(
    *PERMISSIONS.values(),
    {
        "students.create",
        "students.archive",
        "parents.manage",
        "staff.manage",
        "users.manage",
        "school.manage_settings",
        "school.manage_branding",
    },
)


def permission_names(role):
    return sorted(ALL_PERMISSIONS if role in ADMIN else PERMISSIONS.get(role, set()))


def seed_roles(db):
    permissions = {permission.name: permission for permission in db.scalars(select(Permission))}
    for name in sorted(ALL_PERMISSIONS - permissions.keys()):
        permission = Permission(name=name)
        db.add(permission)
        permissions[name] = permission
    db.flush()
    roles = {role.name: role for role in db.scalars(select(Role))}
    for name in sorted(ADMIN | set(PERMISSIONS)):
        role = roles.get(name)
        if not role:
            role = Role(name=name)
            db.add(role)
            db.flush()
            roles[name] = role
        assigned = set(db.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)))
        db.add_all(
            RolePermission(role_id=role.id, permission_id=permissions[p].id)
            for p in permission_names(name)
            if permissions[p].id not in assigned
        )
    db.flush()


def seed_platform_admin(db):
    """Create the bootstrap platform account once, without resetting its password."""
    seed_roles(db)
    admin = db.scalar(select(User).where(User.username == "admin"))
    if admin:
        if admin.role != "PLATFORM_SUPER_ADMIN":
            admin.role = "PLATFORM_SUPER_ADMIN"
            admin.token_version += 1
            assign_role(db, admin)
        return admin
    school = db.scalar(select(School).where(School.school_type == "PLATFORM"))
    if not school:
        school = School(
            name="Ile-Iwe Platform",
            email="platform@ile-iwe.local",
            school_type="PLATFORM",
            status="ACTIVE",
        )
        db.add(school)
        db.flush()
    admin = User(
        school_id=school.id,
        username="admin",
        email="admin@ile-iwe.local",
        name="Platform Administrator",
        password_hash=passwords.hash("admin"),
        role="PLATFORM_SUPER_ADMIN",
        must_change_password=True,
    )
    db.add(admin)
    db.flush()
    assign_role(db, admin)
    return admin
def assign_role(db, user):
    for record in db.scalars(select(UserRole).where(UserRole.user_id == user.id)):
        db.delete(record)
    role = db.scalar(select(Role).where(Role.name == user.role))
    db.add(UserRole(school_id=user.school_id, user_id=user.id, role_id=role.id))


def audit(db, user, action, entity, entity_id, details=None):
    db.add(
        AuditLog(
            school_id=user.school_id,
            actor_id=user.id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=details or {},
        )
    )


def require(user, permission):
    if permission not in permission_names(user.role):
        raise HTTPException(403, "You do not have permission to perform this action.")


def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db, scope="function"),
):
    if not credentials:
        raise HTTPException(401, "Sign in to continue.")
    try:
        payload = jwt.decode(
            credentials.credentials, get_settings().jwt_secret_key, algorithms=["HS256"], audience="ile-iwe-api"
        )
        if payload.get("kind") != "access":
            raise ValueError()
        user = db.get(User, payload["sub"])
        if not user or not user.active or user.token_version != payload["version"]:
            raise ValueError()
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    school = db.get(School, user.school_id)
    if school.status != "ACTIVE" and user.role != "PLATFORM_SUPER_ADMIN":
        raise HTTPException(403, "Your school is awaiting approval or has been suspended.")
    request.state.user_id, request.state.school_id = user.id, user.school_id
    if user.must_change_password:
        allowed = {"/api/v1/auth/me", "/api/v1/auth/change-password", "/api/v1/auth/logout"}
        if request.url.path not in allowed:
            raise HTTPException(
                403,
                {
                    "code": "ACCOUNT_MUST_CHANGE_PASSWORD",
                    "message": "Change your temporary password before accessing your portal.",
                },
            )
        if (
            request.url.path == "/api/v1/auth/change-password"
            and user.temporary_password_expires_at
            and user.temporary_password_expires_at.replace(tzinfo=timezone.utc) <= now()
        ):
            raise HTTPException(403, "Your temporary password has expired. Request a new invitation.")
    return user


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def issue_tokens(db, user):
    settings = get_settings()
    access = jwt.encode(
        {
            "sub": user.id,
            "version": user.token_version,
            "kind": "access",
            "aud": "ile-iwe-api",
            "iat": now(),
            "exp": now() + timedelta(minutes=settings.access_token_expire_minutes),
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )
    refresh = secrets.token_urlsafe(48)
    db.add(
        AuthToken(
            school_id=user.school_id,
            user_id=user.id,
            token_hash=token_hash(refresh),
            kind="refresh",
            expires_at=now() + timedelta(days=settings.refresh_token_expire_days),
        )
    )
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer"}
