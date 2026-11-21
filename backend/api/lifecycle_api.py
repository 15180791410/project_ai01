"""学生生命周期、入学档案和毕业审核接口。"""
from fastapi import APIRouter, Header
from pydantic import BaseModel

from api.auth import (
    can_access_student, require_capability, require_user, request_token,
    staff_role, student_scope,
)
from db import get_db
from service import lifecycle
from service.notify_logic import notify
from utils import format_data


router = APIRouter()


@router.get("/my", summary="学生查看本人完整生命周期")
def my_lifecycle(authorization: str = Header(default=""), token: str = ""):
    user, err = require_user(request_token(authorization, token), ("STUDENT",))
    if err:
        return err
    return format_data.ok(lifecycle.get_stage_info(user["id"]))


@router.get("/{student_id}", summary="员工查看学生生命周期详情")
def student_lifecycle(student_id: int,
                      authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "student.read"
    )
    if err:
        return err
    conn = get_db()
    try:
        if not can_access_student(conn, user, student_id):
            return format_data.forbidden("只能查看本人负责学生的生命周期")
    finally:
        conn.close()
    return format_data.ok(lifecycle.get_stage_info(student_id))


class EnrollmentData(BaseModel):
    token: str
    enrollment_date: str
    duration_months: int
    duration_text: str = ""
    note: str = ""


@router.post("/{student_id}/enrollment", summary="补录或修正学生入学档案")
def update_enrollment(student_id: int, data: EnrollmentData):
    user, err = require_capability(data.token, "student.enrollment.manage")
    if err:
        return err
    conn = get_db()
    try:
        if not can_access_student(conn, user, student_id):
            return format_data.forbidden("只能维护本人负责学生的入学档案")
    finally:
        conn.close()
    ok, msg = lifecycle.update_enrollment(
        student_id, data.enrollment_date, data.duration_months,
        duration_text=data.duration_text, note=data.note,
        operator_id=user["id"], operator_role=staff_role(user),
    )
    if not ok:
        return format_data.fail(msg)
    return format_data.ok(lifecycle.get_stage_info(student_id), msg)


class GraduationRequestData(BaseModel):
    token: str
    graduation_date: str
    completion_summary: str = ""
    document_ref: str = ""
    student_note: str = ""


@router.post("/graduation/request", summary="学生发起毕业审核")
def request_graduation(data: GraduationRequestData):
    user, err = require_user(data.token, ("STUDENT",))
    if err:
        return err
    ok, msg, request_id = lifecycle.request_graduation(
        user["id"], data.graduation_date,
        completion_summary=data.completion_summary,
        document_ref=data.document_ref,
        student_note=data.student_note,
    )
    if not ok:
        return format_data.fail(msg)
    return format_data.ok({"request_id": request_id}, msg)


@router.get("/graduation/my", summary="学生查看本人毕业审核记录")
def my_graduation_requests(authorization: str = Header(default=""),
                           token: str = ""):
    user, err = require_user(request_token(authorization, token), ("STUDENT",))
    if err:
        return err
    info = lifecycle.get_stage_info(user["id"])
    return format_data.ok(info.get("graduation_requests", []))


@router.get("/graduation/pending", summary="员工查看待审核毕业申请")
def pending_graduation(authorization: str = Header(default=""),
                       token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "graduation.manage"
    )
    if err:
        return err
    scope, args = student_scope(user)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT g.*,u.real_name AS student_name,u.username AS student_username,"
            "       e.program,e.country,e.expected_graduation_date "
            "FROM student_graduation_request g "
            "JOIN sys_user u ON u.id=g.student_id "
            "LEFT JOIN student_enrollment e ON e.id=g.enrollment_id "
            "WHERE g.status='待审核'" + scope +
            " ORDER BY g.create_time ASC, g.id ASC",
            tuple(args),
        ).fetchall()
        return format_data.ok([dict(row) for row in rows])
    finally:
        conn.close()


class GraduationReviewData(BaseModel):
    token: str
    request_id: int
    approved: bool
    review_note: str = ""


@router.post("/graduation/review", summary="审核毕业申请")
def review_graduation(data: GraduationReviewData):
    user, err = require_capability(data.token, "graduation.manage")
    if err:
        return err
    conn = get_db()
    try:
        request_row = conn.execute(
            "SELECT student_id FROM student_graduation_request WHERE id=?",
            (data.request_id,),
        ).fetchone()
        if not request_row:
            return format_data.not_found("毕业申请不存在")
        if not can_access_student(conn, user, request_row["student_id"]):
            return format_data.forbidden("只能审核本人负责学生的毕业申请")
    finally:
        conn.close()
    ok, msg = lifecycle.review_graduation(
        data.request_id, user["id"], data.approved, data.review_note
    )
    if not ok:
        return format_data.fail(msg)
    return format_data.ok(None, msg)
