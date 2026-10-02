import uuid
from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, declared_attr
from .database import Base


def uid():
    return str(uuid.uuid4())


def now():
    return datetime.now(timezone.utc)


class Record(Base):
    __abstract__ = True
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TenantRecord(Record):
    __abstract__ = True
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id"), index=True)

    @declared_attr.directive
    def __table_args__(cls):
        return (UniqueConstraint("school_id", "id"),)
