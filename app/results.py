from decimal import Decimal, ROUND_HALF_UP
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
    Student,
    Term,
    Subject,
    AssessmentComponent,
    GradingScale,
    GradingScaleItem,
    Result,
    ResultScore,
    Document,
)
from .resources import scoped, class_access, class_teacher_ids, base_query, serialize
from .notifications import queue_parents

router = APIRouter(prefix="/results", tags=["Results"])


class ScoreInput(StrictModel):
    component_id: str
    score: Decimal = Field(ge=0, allow_inf_nan=False, max_digits=6, decimal_places=2)


class ResultInput(StrictModel):
    student_id: str
    subject_id: str
    term_id: str
    scores: list[ScoreInput] = Field(min_length=1, max_length=30)


class TransitionInput(StrictModel):
    action: Literal["submit", "review", "approve", "publish", "reopen"]
    reason: str = Field(default="", max_length=500)


class ReportInput(StrictModel):
    student_id: str
    term_id: str
    format: Literal["pdf", "docx"] = "pdf"
    email_parents: bool = False


def calculate_grade(db, user, scores):
    components = db.scalars(
        select(AssessmentComponent).where(
            AssessmentComponent.school_id == user.school_id, AssessmentComponent.active.is_(True)
        )
    ).all()
    by_id = {c.id: c for c in components}
    if set(s.component_id for s in scores) != set(by_id) or len(scores) != len(by_id):
        raise HTTPException(422, "Supply exactly one score for every active assessment component.")
    if sum(c.weight for c in components) != Decimal("100"):
        raise HTTPException(422, "Active assessment weights must total 100%.")
    total = Decimal("0")
    for score in scores:
        component = by_id[score.component_id]
        if score.score > component.max_score:
            raise HTTPException(422, f"{component.name} exceeds its maximum score.")
        total += score.score / component.max_score * component.weight
    total = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    scale = db.scalar(
        select(GradingScale).where(GradingScale.school_id == user.school_id, GradingScale.active.is_(True))
    )
    if not scale:
        raise HTTPException(422, "Configure an active grading scale.")
    grade = db.scalars(
        select(GradingScaleItem).where(
            GradingScaleItem.school_id == user.school_id,
            GradingScaleItem.scale_id == scale.id,
            GradingScaleItem.min_score <= total,
            GradingScaleItem.max_score >= total,
        )
    ).all()
    if len(grade) != 1:
        raise HTTPException(422, "The grading scale must contain one grade for this score.")
    return total, grade[0], by_id


@router.post("", status_code=201)
def enter_result(data: ResultInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "results.enter")
    student = scoped(db, Student, data.student_id, user, lock=True)
    scoped(db, Term, data.term_id, user)
    scoped(db, Subject, data.subject_id, user)
    if not student.class_id:
        raise HTTPException(422, "Assign the student to a class first.")
    class_access(db, user, student.class_id, data.subject_id)
    record = db.scalar(
        select(Result).where(
            Result.student_id == student.id, Result.subject_id == data.subject_id, Result.term_id == data.term_id
        )
    )
    if record and record.status != "DRAFT":
        raise HTTPException(409, "Only draft results can be changed. Request an authorized reopening.")
    if record and user.role == "TEACHER" and record.entered_by != user.id:
        raise HTTPException(403, "Only the teacher who entered this draft can edit it.")
    total, grade, components = calculate_grade(db, user, data.scores)
    if record:
        for score in db.scalars(select(ResultScore).where(ResultScore.result_id == record.id)):
            db.delete(score)
        db.flush()
        record.total, record.grade, record.remark = total, grade.label, grade.remark
        record.passing = grade.passing
    else:
        record = Result(
            school_id=user.school_id,
            student_id=student.id,
            class_id=student.class_id,
            subject_id=data.subject_id,
            term_id=data.term_id,
            total=total,
            passing=grade.passing,
            grade=grade.label,
            remark=grade.remark,
            entered_by=user.id,
        )
        db.add(record)
        db.flush()
    for score in data.scores:
        c = components[score.component_id]
        db.add(
            ResultScore(
                school_id=user.school_id,
                result_id=record.id,
                component_id=c.id,
                score=score.score,
                max_score=c.max_score,
                weight=c.weight,
            )
        )
    audit(db, user, "results.scores_saved", "results", record.id, {"total": str(total), "grade": grade.label})
    return serialize(record)


@router.get("/{result_id}/scores")
def get_scores(result_id: str, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "results.view")
    record = db.scalar(base_query(db, user, Result).where(Result.id == result_id))
    if not record:
        raise HTTPException(404, "Result was not found.")
    return [
        serialize(s)
        for s in db.scalars(
            select(ResultScore).where(ResultScore.school_id == user.school_id, ResultScore.result_id == record.id)
        )
    ]


@router.post("/{result_id}/transition")
def transition(
    result_id: str, data: TransitionInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")
):
    permission = {
        "submit": "results.enter",
        "approve": "results.review",
        "review": "results.review",
        "publish": "results.publish",
        "reopen": "results.publish",
    }[data.action]
    require(user, permission)
    record = scoped(db, Result, result_id, user, lock=True)
    class_access(db, user, record.class_id, record.subject_id)
    if user.role == "TEACHER" and record.entered_by != user.id:
        raise HTTPException(403, "Only the teacher who entered this result can submit it.")
    previous = record.status
    if data.action == "reopen":
        if len(data.reason.strip()) < 5:
            raise HTTPException(422, "A correction reason of at least five characters is required.")
        record.status = "DRAFT"
        # Previously generated report cards are no longer authoritative after reopening.
        for document in db.scalars(
            select(Document).where(
                Document.school_id == user.school_id,
                Document.student_id == record.student_id,
                Document.term_id == record.term_id,
                Document.kind == "REPORT_CARD",
            )
        ):
            document.status = "SUPERSEDED"
    else:
        expected, target = {
            "submit": ("DRAFT", "SUBMITTED"),
            "review": ("SUBMITTED", "UNDER_REVIEW"),
            "approve": ("SUBMITTED", "APPROVED"),
            "publish": ("APPROVED", "PUBLISHED"),
        }[data.action]
        if record.status != expected and not (data.action == "approve" and record.status == "UNDER_REVIEW"):
            raise HTTPException(409, f"Result must be {expected.lower()} before this action.")
        record.status = target
    audit(
        db,
        user,
        f"results.{data.action}",
        "results",
        record.id,
        {"previous": previous, "status": record.status, "reason": data.reason},
    )
    if data.action == "publish":
        student = scoped(db, Student, record.student_id, user)
        queue_parents(
            db,
            user,
            student,
            "result_published",
            f"result:{record.id}:{record.updated_at.isoformat()}",
            "A result for {{student_name}} has been published by {{school_name}}.",
        )
    return serialize(record)


@router.post("/report-cards/generate", status_code=202)
def report(data: ReportInput, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "results.view")
    student = scoped(db, Student, data.student_id, user)
    scoped(db, Term, data.term_id, user)
    class_access(db, user, student.class_id)
    if user.role == "TEACHER" and student.class_id not in class_teacher_ids(db, user):
        raise HTTPException(403, "Report cards require a class-teacher assignment.")
    if data.email_parents:
        require(user, "notifications.send_email")
    results = db.scalars(
        select(Result).where(
            Result.school_id == user.school_id, Result.student_id == student.id, Result.term_id == data.term_id
        )
    ).all()
    if not results or any(r.status != "PUBLISHED" for r in results):
        raise HTTPException(409, "Publish all entered results for this student and term first.")
    document = Document(school_id=user.school_id, **data.model_dump(), kind="REPORT_CARD", available_at=now())
    db.add(document)
    db.flush()
    audit(db, user, "report_card.queued", "documents", document.id)
    return serialize(document)
