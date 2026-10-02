from datetime import datetime
from uuid import uuid4
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import Field
from .auth import StrictModel
from .core.database import get_db
from .core.security import current_user, require, audit
from .models import Student, ClassArm
from .resources import scoped

router = APIRouter(prefix="/students", tags=["Students"])


def generate_student_id():
    return f"IW-{datetime.now().year}-{uuid4().hex[:12].upper()}"


class MoveStudents(StrictModel):
    student_ids: list[str] = Field(min_length=1, max_length=200)
    class_id: str
    reason: str = Field(min_length=5, max_length=500)


@router.post("/move")
def move(data: MoveStudents, user=Depends(current_user), db: Session = Depends(get_db, scope="function")):
    require(user, "students.update")
    scoped(db, ClassArm, data.class_id, user)
    students = [scoped(db, Student, id, user, lock=True) for id in set(data.student_ids)]
    for student in students:
        previous = student.class_id
        student.class_id = data.class_id
        audit(
            db,
            user,
            "student.class_changed",
            "students",
            student.id,
            {"previous_class": previous, "class_id": data.class_id, "reason": data.reason},
        )
    return {"updated": len(students)}
