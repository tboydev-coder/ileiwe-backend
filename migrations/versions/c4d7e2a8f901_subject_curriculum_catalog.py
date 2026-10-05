"""Add subject school levels and seed the NERDC catalogue."""

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa

from app.curriculum import PRIMARY_SUBJECTS, SECONDARY_SUBJECTS

revision = "c4d7e2a8f901"
down_revision = "b7210f82c001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("subjects", sa.Column("school_level", sa.String(length=20), nullable=False, server_default="PRIMARY"))
    connection = op.get_bind()
    subjects = connection.execute(
        sa.text("SELECT id, school_type FROM schools WHERE school_type != 'PLATFORM'")
    ).mappings()
    now = datetime.now(timezone.utc)
    rows = []
    for school in subjects:
        catalogue = (
            PRIMARY_SUBJECTS
            if school["school_type"] == "PRIMARY"
            else SECONDARY_SUBJECTS
            if school["school_type"] == "SECONDARY"
            else PRIMARY_SUBJECTS + SECONDARY_SUBJECTS
        )
        for name, code, category in catalogue:
            if connection.execute(
                sa.text("SELECT 1 FROM subjects WHERE school_id = :school_id AND code = :code"),
                {"school_id": school["id"], "code": code},
            ).first():
                continue
            rows.append(
                {
                    "id": str(uuid4()),
                    "school_id": school["id"],
                    "name": name,
                    "code": code,
                    "category": category,
                    "school_level": "PRIMARY" if (name, code, category) in PRIMARY_SUBJECTS else "SECONDARY",
                    "compulsory": True,
                    "active": True,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    if rows:
        op.bulk_insert(
            sa.table(
                "subjects",
                sa.column("id", sa.String),
                sa.column("school_id", sa.String),
                sa.column("name", sa.String),
                sa.column("code", sa.String),
                sa.column("category", sa.String),
                sa.column("school_level", sa.String),
                sa.column("compulsory", sa.Boolean),
                sa.column("active", sa.Boolean),
                sa.column("created_at", sa.DateTime),
                sa.column("updated_at", sa.DateTime),
            ),
            rows,
        )


def downgrade():
    op.drop_column("subjects", "school_level")
