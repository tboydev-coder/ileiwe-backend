"""Portal identities, inbox, calendar, staff fields and three-color school palette."""

from alembic import op
import sqlalchemy as sa

revision = "b7210f82c001"
down_revision = "a3a12ba1111f"
branch_labels = None
depends_on = None


def upgrade():
    for name, default in [("secondary_color", "#d4a843"), ("accent_color", "#5279b8")]:
        op.add_column("schools", sa.Column(name, sa.String(7), nullable=False, server_default=default))
    op.add_column("schools", sa.Column("mandatory_notifications", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("users", sa.Column("username", sa.String(100), nullable=True))
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.add_column("users", sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("temporary_password_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("credential_nonce", sa.String(100), nullable=True))
    op.add_column("parents", sa.Column("notification_preferences", sa.JSON(), nullable=False, server_default="{}"))
    for name, length, default in [
        ("first_name", 80, ""),
        ("last_name", 80, ""),
        ("gender", 30, ""),
        ("department", 100, ""),
        ("staff_type", 80, "Teaching"),
        ("address", None, ""),
        ("emergency_contact", 300, ""),
    ]:
        op.add_column(
            "staff", sa.Column(name, sa.String(length) if length else sa.Text(), nullable=False, server_default=default)
        )
    op.add_column("staff", sa.Column("staff_code", sa.String(80), nullable=True))
    op.add_column("staff", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column("staff", sa.Column("photo_key", sa.String(500), nullable=True))
    tenant_unique = next(
        c for c in sa.inspect(op.get_bind()).get_unique_constraints("staff") if c["column_names"] == ["school_id", "id"]
    )
    with op.batch_alter_table(
        "staff", naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s_%(column_1_name)s"}
    ) as batch:
        batch.drop_constraint(tenant_unique["name"] or "uq_staff_school_id_id", type_="unique")
        batch.create_unique_constraint("uq_staff_tenant_id", ["school_id", "id"])
        batch.create_unique_constraint("uq_staff_school_code", ["school_id", "staff_code"])
    op.add_column("announcements", sa.Column("audience", sa.String(20), nullable=False, server_default="ALL"))
    with op.batch_alter_table("announcements") as batch:
        batch.add_column(sa.Column("class_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_announcements_class_id", "class_arms", ["class_id"], ["id"])
        batch.create_foreign_key(
            "fk_announcements_class_id_tenant", "class_arms", ["school_id", "class_id"], ["school_id", "id"]
        )
    # Preserve the initial revision. Reflect its unnamed check and replace only
    # the result status constraint; batch mode also supports existing SQLite data.
    op.add_column("results", sa.Column("passing", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute(
        "UPDATE results SET passing = COALESCE((SELECT grading_scale_items.passing FROM grading_scale_items JOIN grading_scales ON grading_scales.id = grading_scale_items.scale_id WHERE grading_scales.school_id = results.school_id AND grading_scales.active = TRUE AND grading_scale_items.label = results.grade AND grading_scale_items.min_score <= results.total AND grading_scale_items.max_score >= results.total LIMIT 1), FALSE)"
    )
    table = sa.Table("results", sa.MetaData(), autoload_with=op.get_bind())
    for constraint in list(table.constraints):
        if isinstance(constraint, sa.CheckConstraint) and "status IN" in str(constraint.sqltext):
            table.constraints.remove(constraint)
    table.append_constraint(
        sa.CheckConstraint(
            "status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','APPROVED','PUBLISHED')", name="ck_results_status"
        )
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("results", copy_from=table, recreate="always") as batch:
            batch.alter_column("status", existing_type=sa.String(20), type_=sa.String(20))
    else:
        checks = sa.inspect(op.get_bind()).get_check_constraints("results")
        for check in checks:
            if "status" in check["sqltext"]:
                op.drop_constraint(check["name"], "results", type_="check")
        op.create_check_constraint(
            "ck_results_status", "results", "status IN ('DRAFT','SUBMITTED','UNDER_REVIEW','APPROVED','PUBLISHED')"
        )

    def common():
        return [
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("school_id", sa.String(36), sa.ForeignKey("schools.id"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        ]

    op.create_table(
        "calendar_events",
        *common(),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("audience", sa.String(20), nullable=False),
        sa.Column("class_id", sa.String(36), sa.ForeignKey("class_arms.id")),
        sa.CheckConstraint("end_date >= start_date"),
        sa.UniqueConstraint("school_id", "id", name="uq_calendar_events_tenant_id"),
        sa.ForeignKeyConstraint(
            ["school_id", "class_id"],
            ["class_arms.school_id", "class_arms.id"],
            name="fk_calendar_events_class_id_tenant",
        ),
    )
    op.create_table(
        "inbox_items",
        *common(),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("student_id", sa.String(36), sa.ForeignKey("students.id")),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("event", sa.String(50), nullable=False),
        sa.Column("event_key", sa.String(250), nullable=False, unique=True),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("school_id", "id", name="uq_inbox_items_tenant_id"),
        sa.ForeignKeyConstraint(
            ["school_id", "user_id"], ["users.school_id", "users.id"], name="fk_inbox_items_user_id_tenant"
        ),
        sa.ForeignKeyConstraint(
            ["school_id", "student_id"], ["students.school_id", "students.id"], name="fk_inbox_items_student_id_tenant"
        ),
    )
    for table_name, fields in [
        ("calendar_events", ["school_id", "start_date"]),
        ("inbox_items", ["school_id", "user_id"]),
    ]:
        for field in fields:
            op.create_index(f"ix_{table_name}_{field}", table_name, [field])


def downgrade():
    raise RuntimeError("This migration adds account security state. Restore a verified backup to roll back safely.")
