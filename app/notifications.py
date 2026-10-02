import re
from uuid import uuid4
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import StrictModel
from .core.database import get_db
from .core.models import now
from .core.security import current_user, require, audit
from .models import (
    Parent,
    StudentParent,
    School,
    Notification,
    NotificationTemplate,
    Student,
    ClassArm,
    InboxItem,
    User,
)
from .resources import scoped

router = APIRouter(prefix="/messages", tags=["Communication"])


def render_template(body, variables):
    return re.sub(r"{{\s*(\w+)\s*}}", lambda match: str(variables.get(match[1], match[0])), body)


def queue_parents(db, user, student, event, event_key, body, variables=None, document_id=None, send_external=True):
    school = db.get(School, user.school_id)
    parents = db.scalars(
        select(Parent)
        .join(StudentParent, StudentParent.parent_id == Parent.id)
        .where(
            StudentParent.school_id == user.school_id,
            Parent.school_id == user.school_id,
            StudentParent.student_id == student.id,
        )
    ).all()
    for parent in parents:
        category = (
            "results"
            if event in {"result_published", "document_ready"} and not (document_id and "receipt" in body)
            else "finance"
            if event in {"payment", "payment_recorded", "payment_reversed", "balance_updated"} or "receipt" in body
            else "attendance"
            if event == "attendance"
            else "announcements"
        )
        mandatory = school.mandatory_notifications or {}
        if not mandatory.get(category) and not (parent.notification_preferences or {}).get(category, True):
            continue
        context = {
            "school_name": school.name,
            "student_name": f"{student.first_name} {student.last_name}",
            "parent_name": parent.name,
            **(variables or {}),
        }
        if parent.user_id:
            key = f"{event_key}:{parent.id}:IN_APP"
            if not db.scalar(select(InboxItem.id).where(InboxItem.event_key == key)):
                db.add(
                    InboxItem(
                        school_id=user.school_id,
                        user_id=parent.user_id,
                        student_id=student.id,
                        title=event.replace("_", " ").title(),
                        body=render_template(body, context),
                        event=event,
                        event_key=key,
                    )
                )
        if not send_external:
            continue
        for channel, recipient, enabled in [
            ("EMAIL", parent.email, parent.notify_email),
        ]:
            if (
                (not enabled and not mandatory.get(channel.lower()))
                or not recipient
                or (document_id and channel != "EMAIL")
            ):
                continue
            key = f"{event_key}:{parent.id}:{channel}"
            if db.scalar(select(Notification.id).where(Notification.event_key == key)):
                continue
            template = db.scalar(
                select(NotificationTemplate).where(
                    NotificationTemplate.school_id == user.school_id,
                    NotificationTemplate.event == event,
                    NotificationTemplate.channel == channel,
                )
            )
            context = {
                "school_name": school.name,
                "student_name": f"{student.first_name} {student.last_name}",
                "parent_name": parent.name,
                **(variables or {}),
            }
            db.add(
                Notification(
                    school_id=user.school_id,
                    channel=channel,
                    recipient=recipient,
                    subject=f"{school.name} · {event.replace('_', ' ').title()}",
                    body=render_template(template.body if template else body, context),
                    event_key=key,
                    document_id=document_id,
                    available_at=now(),
                )
            )


class MessageInput(StrictModel):
    channel: Literal["EMAIL", "IN_APP"]
    subject: str = Field(default="School update", min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    audience: Literal["school", "class", "selected", "parents"] = "school"
    class_id: str | None = None
    student_ids: list[str] = Field(default_factory=list, max_length=500)
    parent_ids: list[str] = Field(default_factory=list, max_length=500)


def recipients(db, user, data):
    query = select(Parent).where(Parent.school_id == user.school_id)
    if data.audience == "parents":
        if not data.parent_ids:
            raise HTTPException(422, "Select at least one parent.")
        for id in data.parent_ids:
            scoped(db, Parent, id, user)
        query = query.where(Parent.id.in_(data.parent_ids))
    elif data.audience in {"class", "selected"}:
        students = select(Student.id).where(Student.school_id == user.school_id)
        if data.audience == "class":
            scoped(db, ClassArm, data.class_id, user)
            students = students.where(Student.class_id == data.class_id)
        else:
            if not data.student_ids:
                raise HTTPException(422, "Select at least one student.")
            for id in data.student_ids:
                scoped(db, Student, id, user)
            students = students.where(Student.id.in_(data.student_ids))
        query = query.where(
            Parent.id.in_(
                select(StudentParent.parent_id).where(
                    StudentParent.school_id == user.school_id, StudentParent.student_id.in_(students)
                )
            )
        )
    parents = db.scalars(query).all()
    mandatory = db.get(School, user.school_id).mandatory_notifications or {}
    return [
        p
        for p in parents
        if (mandatory.get("announcements") or (p.notification_preferences or {}).get("announcements", True))
        and (
            p.user_id
            if data.channel == "IN_APP"
            else p.email and (p.notify_email or mandatory.get("email"))
            if data.channel == "EMAIL"
            else False
        )
    ]


@router.post("/preview")
def preview(data: MessageInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "notifications.send_" + ("email" if data.channel == "IN_APP" else data.channel.lower()))
    people = recipients(db, user, data)
    school = db.get(School, user.school_id)
    return {
        "recipient_count": len(people),
        "preview": render_template(
            data.body, {"school_name": school.name, "parent_name": people[0].name if people else "Parent"}
        ),
    }


@router.post("/send", status_code=202)
def send(data: MessageInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "notifications.send_" + ("email" if data.channel == "IN_APP" else data.channel.lower()))
    people = recipients(db, user, data)
    school = db.get(School, user.school_id)
    batch = str(uuid4())
    for parent in people:
        if data.channel == "IN_APP":
            db.add(
                InboxItem(
                    school_id=user.school_id,
                    user_id=parent.user_id,
                    title=data.subject,
                    body=render_template(data.body, {"school_name": school.name, "parent_name": parent.name}),
                    event="message",
                    event_key=f"message:{batch}:{parent.id}",
                )
            )
            continue
        db.add(
            Notification(
                school_id=user.school_id,
                channel=data.channel,
                recipient=parent.email if data.channel == "EMAIL" else parent.phone,
                subject=data.subject,
                body=render_template(data.body, {"school_name": school.name, "parent_name": parent.name}),
                event_key=f"message:{batch}:{parent.id}",
                available_at=now(),
            )
        )
    audit(
        db, user, "notifications.queued", "notifications", batch, {"recipients": len(people), "channel": data.channel}
    )
    return {"queued": len(people)}


def school_updates_enabled(db, account, school):
    if account.role != "PARENT" or (school.mandatory_notifications or {}).get("announcements"):
        return True
    parent = db.scalar(select(Parent).where(Parent.school_id == school.id, Parent.user_id == account.id))
    return bool(parent and (parent.notification_preferences or {}).get("announcements", True))


def broadcast_announcement(db, actor, announcement):
    school = db.get(School, actor.school_id)
    accounts = select(User).where(User.school_id == actor.school_id, User.active.is_(True))
    for account in db.scalars(accounts):
        if account.role not in {"PARENT", "TEACHER"}:
            continue
        if not school_updates_enabled(db, account, school):
            continue
        from .resources import audience_query
        from .models import Announcement

        if db.scalar(audience_query(db, account, Announcement).where(Announcement.id == announcement.id)):
            key = f"announcement:{announcement.id}:{account.id}"
            if not db.scalar(select(InboxItem.id).where(InboxItem.event_key == key)):
                db.add(
                    InboxItem(
                        school_id=actor.school_id,
                        user_id=account.id,
                        title=announcement.title,
                        body=announcement.body,
                        event="announcement",
                        event_key=key,
                    )
                )


def queue_calendar_reminders(db):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from .models import CalendarEvent
    from .resources import audience_query

    for school in db.scalars(select(School).where(School.status == "ACTIVE")):
        today = datetime.now(ZoneInfo(school.timezone)).date()
        if not db.scalar(
            select(CalendarEvent.id).where(CalendarEvent.school_id == school.id, CalendarEvent.start_date == today)
        ):
            continue
        for account in db.scalars(
            select(User).where(User.school_id == school.id, User.active.is_(True), User.role.in_(["TEACHER", "PARENT"]))
        ):
            if not school_updates_enabled(db, account, school):
                continue
            for event in db.scalars(
                audience_query(db, account, CalendarEvent).where(CalendarEvent.start_date == today)
            ):
                key = f"calendar:{event.id}:{account.id}:{today}"
                if not db.scalar(select(InboxItem.id).where(InboxItem.event_key == key)):
                    db.add(
                        InboxItem(
                            school_id=school.id,
                            user_id=account.id,
                            title=f"Today: {event.title}"[:200],
                            body=event.body,
                            event="calendar",
                            event_key=key,
                        )
                    )
