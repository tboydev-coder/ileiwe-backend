"""Explicit, local-only demonstration data. Never runs during application startup."""

from datetime import date, timedelta, time
from decimal import Decimal
from sqlalchemy import select
from .auth import Onboard, create_school
from .core.config import get_settings
from .core.database import SessionLocal, migrate
from .core.security import passwords, assign_role, audit
from .core.models import now
from . import models as m
from .students import generate_student_id

DEMO_PASSWORD = "Ile-iwe-demo-2026!"


def main():
    if get_settings().app_env == "production":
        raise SystemExit("Demo seeding is disabled in production.")
    migrate()
    with SessionLocal.begin() as db:
        if db.scalar(select(m.User).where(m.User.email == "owner@demo.ile-iwe.test")):
            print("Demo school already exists. No changes made.")
            return
        # email-validator deliberately rejects .test on public input; local-only demo accounts
        # use example.com, a reserved domain, so they can use the regular login form.
        if db.scalar(select(m.User).where(m.User.email == "owner.ile-iwe@example.com")):
            print("Demo school already exists. No changes made.")
            return
        owner = create_school(
            db,
            Onboard(
                name="Owolabi Adeyemi",
                email="owner.ile-iwe@example.com",
                password=DEMO_PASSWORD,
                school_name="Ireti Heights Academy",
                school_email="office.ile-iwe@example.com",
                phone="+234 800 000 0000",
                address="24 Oladipo Avenue, Ikeja, Lagos",
                primary_color="#156b55",
            ),
        )
        school = db.get(m.School, owner.school_id)
        school.status = "ACTIVE"
        term = db.scalar(select(m.Term).where(m.Term.school_id == school.id))
        accounts = {}
        for role, name, prefix in [
            ("SCHOOL_ADMIN", "Funmilayo Ajayi", "principal"),
            ("SCHOOL_ADMIN", "Bola Adekunle", "viceprincipal"),
            ("TEACHER", "Samuel Adewale", "hod"),
            ("TEACHER", "Chidinma Okeke", "teacher"),
            ("TEACHER", "Tunde Bakare", "teacher2"),
            ("ACCOUNTANT", "Amina Bello", "accountant"),
            ("PARENT", "Ngozi Okafor", "parent"),
            ("PARENT", "Mary Adeyemi", "parent2"),
            ("PARENT", "Aisha Bello", "parent3"),
        ]:
            user = m.User(
                school_id=school.id,
                name=name,
                email=f"{prefix}.ile-iwe@example.com",
                password_hash=passwords.hash(DEMO_PASSWORD),
                role=role,
                username=prefix + ".ile-iwe",
                must_change_password=True,
                temporary_password_expires_at=now() + timedelta(hours=get_settings().temporary_password_expire_hours),
            )
            db.add(user)
            db.flush()
            assign_role(db, user)
            accounts[prefix] = user
            if role != "PARENT":
                db.add(
                    m.Staff(
                        school_id=school.id,
                        user_id=user.id,
                        name=name,
                        job_title={"principal": "Principal", "viceprincipal": "Vice Principal", "hod": "HOD"}.get(
                            prefix, role.title()
                        ),
                        department="Mathematics" if prefix == "hod" else "General",
                        phone="+234 800 000 0000",
                        employment_date=date(2024, 9, 1),
                    )
                )
        classes = []
        for i, name in enumerate(["Primary 4", "Primary 5", "JS1", "JS2"]):
            level = m.ClassLevel(school_id=school.id, name=name, sort_order=i)
            db.add(level)
            db.flush()
            arm = m.ClassArm(
                school_id=school.id,
                level_id=level.id,
                name="A",
                teacher_id=accounts["teacher" if i < 2 else "teacher2"].id,
            )
            db.add(arm)
            db.flush()
            classes.append(arm)
        subjects = []
        for name, code in [("Mathematics", "MTH"), ("English Language", "ENG"), ("Basic Science", "BSC")]:
            subject = m.Subject(school_id=school.id, name=name, code=code)
            db.add(subject)
            db.flush()
            subjects.append(subject)
            for arm in classes:
                db.add(
                    m.SubjectAssignment(
                        school_id=school.id, class_id=arm.id, subject_id=subject.id, teacher_id=arm.teacher_id
                    )
                )
        names = [
            ("Adaeze", "Okafor"),
            ("Temiloluwa", "Adeyemi"),
            ("Ibrahim", "Bello"),
            ("Zainab", "Abubakar"),
            ("Chisom", "Eze"),
            ("Damilola", "Ojo"),
            ("Emeka", "Nwosu"),
            ("Aisha", "Yusuf"),
            ("Oluwatobi", "Adebayo"),
            ("Chiamaka", "Obi"),
            ("David", "John"),
            ("Fatima", "Musa"),
            ("Kehinde", "Ogunleye"),
            ("Nneka", "Umeh"),
            ("Seyi", "Oladipo"),
            ("Maryam", "Sule"),
            ("Chinedu", "Okoro"),
            ("Tomiwa", "Ajayi"),
            ("Esther", "James"),
            ("Ahmed", "Garba"),
            ("Yetunde", "Balogun"),
            ("Uchenna", "Ike"),
            ("Samuel", "George"),
            ("Halima", "Usman"),
        ]
        demo_students = []
        for index, (first, last) in enumerate(names):
            arm = classes[index // 6]
            student = m.Student(
                school_id=school.id,
                student_code=generate_student_id(),
                admission_no=f"DEMO-{index + 1:04d}",
                first_name=first,
                last_name=last,
                class_id=arm.id,
                gender="Female" if index % 2 == 0 else "Male",
                date_of_birth=date(2014 - index // 6, 3, index + 1),
                admission_date=date(2025, 9, 8),
            )
            parent = m.Parent(
                school_id=school.id,
                name="Ngozi Okafor" if index == 0 else f"Guardian of {first} {last}",
                email=f"guardian{index + 1}.ile-iwe@example.com",
                phone=f"+234800000{index:04d}",
                user_id=accounts["parent"].id if index == 0 else None,
            )
            db.add_all([student, parent])
            db.flush()
            demo_students.append(student)
            db.add(
                m.StudentParent(
                    school_id=school.id,
                    student_id=student.id,
                    parent_id=parent.id,
                    relationship="Guardian",
                    primary_contact=True,
                )
            )
            amount = Decimal("150000")
            db.add(
                m.StudentCharge(
                    school_id=school.id,
                    student_id=student.id,
                    term_id=term.id,
                    description="Demo tuition charge",
                    amount=amount,
                    discount=0,
                )
            )
            if index % 3 != 0:
                paid = Decimal("100000") if index % 2 else Decimal("150000")
                payment = m.Payment(
                    school_id=school.id,
                    student_id=student.id,
                    term_id=term.id,
                    amount=paid,
                    payment_date=date.today(),
                    method="BANK_TRANSFER",
                    reference="DEMO",
                    receipt_number=f"DEMO-RCP-{index:04d}",
                    idempotency_key=f"demo-payment-{index}",
                    recorded_by=accounts["accountant"].id,
                    previous_balance=amount,
                    new_balance=amount - paid,
                )
                db.add(payment)
                db.flush()
                db.add(
                    m.Document(
                        school_id=school.id,
                        student_id=student.id,
                        term_id=term.id,
                        payment_id=payment.id,
                        kind="RECEIPT",
                        format="pdf",
                        available_at=now(),
                    )
                )
            for offset in range(7):
                day = date.today() - timedelta(days=offset)
                if day.weekday() < 5 or offset == 0:
                    status = (
                        "ABSENT" if (index + offset) % 11 == 0 else "LATE" if (index + offset) % 13 == 0 else "PRESENT"
                    )
                    db.add(
                        m.Attendance(
                            school_id=school.id,
                            student_id=student.id,
                            class_id=arm.id,
                            term_id=term.id,
                            attendance_date=day,
                            status=status,
                            marked_by=arm.teacher_id,
                            source="MANUAL",
                        )
                    )
            components = db.scalars(
                select(m.AssessmentComponent).where(m.AssessmentComponent.school_id == school.id)
            ).all()
            for subject in subjects:
                total = Decimal(65 + index % 25)
                result = m.Result(
                    school_id=school.id,
                    student_id=student.id,
                    class_id=arm.id,
                    term_id=term.id,
                    subject_id=subject.id,
                    total=total,
                    passing=True,
                    grade="A" if total >= 70 else "B",
                    remark="Excellent" if total >= 70 else "Very good",
                    status="PUBLISHED" if index < 12 else "DRAFT",
                    entered_by=arm.teacher_id,
                )
                db.add(result)
                db.flush()
                for component in components:
                    db.add(
                        m.ResultScore(
                            school_id=school.id,
                            result_id=result.id,
                            component_id=component.id,
                            score=component.max_score * total / 100,
                            max_score=component.max_score,
                            weight=component.weight,
                        )
                    )
        for prefix, count in [("parent2", 2), ("parent3", 3)]:
            parent = m.Parent(
                school_id=school.id,
                user_id=accounts[prefix].id,
                name=accounts[prefix].name,
                email=accounts[prefix].email,
            )
            db.add(parent)
            db.flush()
            for child in demo_students[1 : count + 1]:
                db.add(
                    m.StudentParent(
                        school_id=school.id, parent_id=parent.id, student_id=child.id, relationship="Guardian"
                    )
                )
        for index, arm in enumerate(classes):
            for weekday in range(5):
                db.add(
                    m.TimetableEntry(
                        school_id=school.id,
                        class_id=arm.id,
                        subject_id=subjects[0].id,
                        teacher_id=arm.teacher_id,
                        room=f"Room {index + 1}",
                        weekday=weekday,
                        start_time=time(8 + index, 0),
                        end_time=time(8 + index, 45),
                        published=True,
                    )
                )
            structure = m.FeeStructure(
                school_id=school.id,
                name="Term tuition · " + db.get(m.ClassLevel, arm.level_id).name,
                class_id=arm.id,
                term_id=term.id,
            )
            db.add(structure)
            db.flush()
            db.add(m.FeeItem(school_id=school.id, structure_id=structure.id, name="Books & materials", amount=25000))
        db.add_all(
            [
                m.Announcement(
                    school_id=school.id,
                    title="Welcome to a new school year",
                    body="A fresh start, new friendships, and plenty of room to grow. We look forward to a wonderful year of learning together.",
                    created_by=owner.id,
                ),
                m.Announcement(
                    school_id=school.id,
                    title="A note for our teaching team",
                    audience="STAFF",
                    body="Please complete your class attendance each morning and keep your assessment records up to date. Thank you for the care you bring to every school day.",
                    created_by=owner.id,
                ),
            ]
        )
        db.add(
            m.NotificationTemplate(
                school_id=school.id,
                name="Family update",
                event="announcement",
                channel="EMAIL",
                body="Hello {{parent_name}},\nHere is an update from {{school_name}}.\nThank you for being part of our school community.",
            )
        )
        audit(db, owner, "demo.seeded", "schools", school.id, {"demo": True})
        for child in demo_students[:12]:
            db.add(
                m.Document(
                    school_id=school.id,
                    student_id=child.id,
                    term_id=term.id,
                    kind="REPORT_CARD",
                    format="pdf",
                    available_at=now(),
                )
            )
        db.add(
            m.CalendarEvent(
                school_id=school.id,
                title="PTA meeting",
                body="Meet your child's teaching team.",
                start_date=date.today() + timedelta(days=7),
                end_date=date.today() + timedelta(days=7),
                audience="ALL",
            )
        )
    print("Local demo school created. Login: owner.ile-iwe@example.com | Password: " + DEMO_PASSWORD)


if __name__ == "__main__":
    main()
