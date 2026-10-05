"""Upgrade an occupied initial-schema database without rebuilding its school data."""

from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, MetaData, select, func
from app.core import database
from app.curriculum import PRIMARY_SUBJECTS, SECONDARY_SUBJECTS


def test_upgrade_preserves_initial_accounts_and_staff(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    engine = create_engine("sqlite:///" + (tmp_path / "legacy.db").as_posix())
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    timestamp = datetime.now(timezone.utc)
    school_id, user_id, staff_id = (str(uuid4()) for _ in range(3))
    common = {"created_at": timestamp, "updated_at": timestamp}
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "a3a12ba1111f")
        metadata = MetaData()
        metadata.reflect(conn)
        conn.execute(
            metadata.tables["schools"].insert(),
            {
                **common,
                "id": school_id,
                "name": "Existing school",
                "email": "office@example.com",
                "phone": "",
                "address": "",
                "website": "",
                "registration_number": "",
                "school_type": "COMBINED",
                "primary_color": "#123456",
                "timezone": "Africa/Lagos",
                "currency": "NGN",
                "status": "ACTIVE",
            },
        )
        conn.execute(
            metadata.tables["users"].insert(),
            {
                **common,
                "id": user_id,
                "school_id": school_id,
                "name": "Existing teacher",
                "email": "existing@example.com",
                "password_hash": "existing-hash-preserved",
                "role": "TEACHER",
                "active": True,
                "token_version": 3,
            },
        )
        conn.execute(
            metadata.tables["staff"].insert(),
            {
                **common,
                "id": staff_id,
                "school_id": school_id,
                "name": "Existing teacher",
                "user_id": user_id,
                "phone": "123",
                "job_title": "Teacher",
                "active": True,
            },
        )
    monkeypatch.setattr(database, "engine", engine)
    database.migrate()
    database.migrate()
    with engine.connect() as conn:
        metadata = MetaData()
        metadata.reflect(conn)
        user = conn.execute(select(metadata.tables["users"])).mappings().one()
        assert user["id"] == user_id and user["password_hash"] == "existing-hash-preserved"
        assert user["token_version"] == 3 and user["must_change_password"] is False
        staff = conn.execute(select(metadata.tables["staff"])).mappings().one()
        assert staff["id"] == staff_id and staff["phone"] == "123"
        school = conn.execute(select(metadata.tables["schools"])).mappings().one()
        assert school["primary_color"] == "#123456" and school["secondary_color"] == "#d4a843"
        assert not conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    engine.dispose()


def test_subject_catalog_is_seeded_by_school_type(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    engine = create_engine("sqlite:///" + (tmp_path / "curriculum.db").as_posix())
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    timestamp = datetime.now(timezone.utc)
    school_ids = {"PRIMARY": str(uuid4()), "SECONDARY": str(uuid4())}
    common = {"created_at": timestamp, "updated_at": timestamp}
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.upgrade(config, "a3a12ba1111f")
        metadata = MetaData()
        metadata.reflect(conn)
        for school_type, school_id in school_ids.items():
            conn.execute(
                metadata.tables["schools"].insert(),
                {
                    **common,
                    "id": school_id,
                    "name": school_type.title(),
                    "email": f"{school_type.lower()}@example.com",
                    "phone": "",
                    "address": "",
                    "website": "",
                    "registration_number": "",
                    "school_type": school_type,
                    "primary_color": "#123456",
                    "timezone": "Africa/Lagos",
                    "currency": "NGN",
                    "status": "ACTIVE",
                },
            )
        command.upgrade(config, "head")
    with engine.connect() as conn:
        metadata = MetaData()
        metadata.reflect(conn)
        counts = {
            row["school_id"]: row["count"]
            for row in conn.execute(
                select(metadata.tables["subjects"].c.school_id, func.count().label("count"))
                .group_by(metadata.tables["subjects"].c.school_id)
            ).mappings()
        }
        assert counts[school_ids["PRIMARY"]] == len(PRIMARY_SUBJECTS)
        assert counts[school_ids["SECONDARY"]] == len(SECONDARY_SUBJECTS)
