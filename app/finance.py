from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from .auth import StrictModel
from .core.database import get_db
from .core.models import now
from .core.security import current_user, require, audit
from .models import Student, StudentCharge, Payment, Term, FeeStructure, FeeItem, Document
from .resources import scoped, serialize
from .notifications import queue_parents

router = APIRouter(tags=["Finance"])


def ledger(db, school_id, student_id, term_id=None):
    charges = select(func.coalesce(func.sum(StudentCharge.amount - StudentCharge.discount), 0)).where(
        StudentCharge.school_id == school_id, StudentCharge.student_id == student_id
    )
    payments = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.school_id == school_id, Payment.student_id == student_id, Payment.reversed.is_(False)
    )
    if term_id:
        charges, payments = charges.where(StudentCharge.term_id == term_id), payments.where(Payment.term_id == term_id)
    charged, paid = Decimal(db.scalar(charges)), Decimal(db.scalar(payments))
    return {"charged": charged, "paid": paid, "balance": charged - paid}


class PaymentInput(StrictModel):
    student_id: str
    term_id: str
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2, allow_inf_nan=False)
    payment_date: date
    method: Literal["CASH", "BANK_TRANSFER", "POS", "CHEQUE", "OTHER"]
    reference: str = Field(default="", max_length=120)
    idempotency_key: str = Field(min_length=8, max_length=120)


class Reason(StrictModel):
    reason: str = Field(min_length=5, max_length=500)


@router.get("/students/{student_id}/ledger")
def student_ledger(
    student_id: str,
    term_id: str | None = None,
    user=Depends(current_user),
    db: Session = Depends(get_db, scope="function"),
):
    require(user, "finance.view")
    scoped(db, Student, student_id, user)
    if term_id:
        scoped(db, Term, term_id, user)
    return ledger(db, user.school_id, student_id, term_id)


@router.post("/fees/{structure_id}/apply")
def apply_fees(structure_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "finance.manage_fees")
    structure = scoped(db, FeeStructure, structure_id, user, lock=True)
    items = db.scalars(
        select(FeeItem).where(FeeItem.school_id == user.school_id, FeeItem.structure_id == structure.id)
    ).all()
    if not items:
        raise HTTPException(422, "Add at least one fee item first.")
    students = db.scalars(
        select(Student)
        .where(Student.school_id == user.school_id, Student.class_id == structure.class_id, Student.status == "ACTIVE")
        .order_by(Student.id)
        .with_for_update()
    ).all()
    count = 0
    for student in students:
        for item in items:
            exists = db.scalar(
                select(StudentCharge.id).where(
                    StudentCharge.student_id == student.id,
                    StudentCharge.term_id == structure.term_id,
                    StudentCharge.fee_item_id == item.id,
                )
            )
            if not exists:
                db.add(
                    StudentCharge(
                        school_id=user.school_id,
                        student_id=student.id,
                        term_id=structure.term_id,
                        fee_item_id=item.id,
                        description=item.name,
                        amount=item.amount,
                        discount=0,
                    )
                )
                count += 1
    audit(db, user, "finance.fees_applied", "fee_structures", structure.id, {"charges": count})
    return {"charges_created": count}


@router.post("/payments", status_code=201)
def record_payment(data: PaymentInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "finance.record_payment")
    student = scoped(db, Student, data.student_id, user, lock=True)
    term = scoped(db, Term, data.term_id, user)
    existing = db.scalar(
        select(Payment).where(Payment.school_id == user.school_id, Payment.idempotency_key == data.idempotency_key)
    )
    if existing:
        if any(getattr(existing, key) != value for key, value in data.model_dump().items()):
            raise HTTPException(409, "Idempotency key was already used for a different payment.")
        return serialize(existing)
    if data.payment_date > date.today():
        raise HTTPException(422, "Payment date cannot be in the future.")
    previous = ledger(db, user.school_id, student.id, term.id)["balance"]
    if data.amount > previous:
        raise HTTPException(422, "Payment exceeds the outstanding balance for this term.")
    payment = Payment(
        school_id=user.school_id,
        **data.model_dump(),
        receipt_number="IW-" + uuid4().hex[:16].upper(),
        recorded_by=user.id,
        previous_balance=previous,
        new_balance=previous - data.amount,
    )
    db.add(payment)
    db.flush()
    db.add(
        Document(
            school_id=user.school_id,
            student_id=student.id,
            term_id=term.id,
            payment_id=payment.id,
            kind="RECEIPT",
            format="pdf",
            available_at=now(),
            email_parents=True,
        )
    )
    audit(
        db,
        user,
        "finance.payment_recorded",
        "payments",
        payment.id,
        {"amount": str(payment.amount), "receipt": payment.receipt_number},
    )
    queue_parents(
        db,
        user,
        student,
        "payment",
        f"payment:{payment.id}",
        "{{school_name}} received NGN {{amount_paid}} for {{student_name}}. Balance: NGN {{balance}}. Receipt: {{receipt_number}}.",
        {
            "amount_paid": payment.amount,
            "balance": payment.new_balance,
            "receipt_number": payment.receipt_number,
            "term": term.name,
        },
    )
    return serialize(payment)


@router.post("/payments/{payment_id}/reverse")
def reverse(payment_id: str, data: Reason, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "finance.manage_fees")
    payment = scoped(db, Payment, payment_id, user)
    student = scoped(db, Student, payment.student_id, user, lock=True)
    db.refresh(payment)
    if payment.reversed:
        raise HTTPException(409, "Payment has already been reversed.")
    payment.reversed, payment.reversal_reason = True, data.reason
    audit(
        db,
        user,
        "finance.payment_reversed",
        "payments",
        payment.id,
        {"reason": data.reason, "amount": str(payment.amount)},
    )
    queue_parents(
        db,
        user,
        student,
        "payment_reversed",
        f"reversal:{payment.id}",
        "Payment receipt "
        + payment.receipt_number
        + " for {{student_name}} has been reversed. Please contact {{school_name}} for details.",
    )
    return serialize(payment)


@router.post("/students/{student_id}/balance-reminder", status_code=202)
def reminder(student_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "finance.manage_fees")
    student = scoped(db, Student, student_id, user)
    balance = ledger(db, user.school_id, student.id)["balance"]
    queue_parents(
        db,
        user,
        student,
        "balance",
        f"balance:{student.id}:{date.today()}",
        "{{student_name}} has an outstanding balance of NGN {{balance}} at {{school_name}}.",
        {"balance": balance},
    )
    audit(db, user, "finance.balance_reminder", "students", student.id)
    return {"message": "Balance reminder queued for eligible guardians."}
