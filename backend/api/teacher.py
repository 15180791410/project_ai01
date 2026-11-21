"""
ai - 请假审批接口（老师/员工共用）
=========================================
解决"学生请假后谁审批"。此模块功能已合并进员工后台，
接口保留是因为员工后台的请假审批面板仍调用它。
审批时：通过可附留言，驳回必须填写理由。
"""
from fastapi import APIRouter, Header
from pydantic import BaseModel
from datetime import datetime

from db import get_db
from utils import format_data
from api.auth import (can_access_student, require_capability, request_token,
                      student_scope)

router = APIRouter()


class ApproveData(BaseModel):
    token: str
    leave_id: int
    approved: bool
    comment: str = ""   # 通过时的留言 / 驳回时的理由


def _guard(token):
    """请假审批只由班主任、管理员和领导使用。

    顾问不能审批请假；维护人员不接触业务数据。管理员和领导可审批全部学生，
    班主任只能审批本人班级学生。
    """
    user, err = require_capability(token, "leave.read")
    if err:
        return None, err
    if not (
        user.get("user_type") in ("ADMIN",)
        or (user.get("staff_role") or "") in ("HOMEROOM", "LEADER")
    ):
        return None, format_data.forbidden("只有班主任、管理员或领导可查看请假")
    return user, None


def _leave_rows(conn, where="", params=()):
    sql = """
        SELECT a.*, u.real_name AS student_name, u.username AS student_username
        FROM student_admin_service a
        LEFT JOIN sys_user u ON u.id = a.student_id
    """
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY a.create_time DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


@router.get("/pending-leaves", summary="待审批请假列表")
def pending_leaves(authorization: str = Header(default=""), token: str = ""):
    user, err = _guard(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        scope, args = student_scope(user)
        return format_data.ok(_leave_rows(
            conn, "a.status='待审批'" + scope, tuple(args)
        ))
    finally:
        conn.close()


@router.get("/all-leaves", summary="全部请假记录")
def all_leaves(authorization: str = Header(default=""), token: str = ""):
    user, err = _guard(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        scope, args = student_scope(user)
        where = scope[5:] if scope else ""
        return format_data.ok(_leave_rows(conn, where, tuple(args)))
    finally:
        conn.close()


@router.post("/approve", summary="审批请假（通过可留言，驳回必填理由）")
def approve(data: ApproveData):
    payload, err = require_capability(data.token, "leave.approve")
    if err:
        return err
    if not (
        payload.get("user_type") in ("ADMIN",)
        or (payload.get("staff_role") or "") in ("HOMEROOM", "LEADER")
    ):
        return format_data.forbidden("只有班主任、管理员或领导可审批请假")
    comment = (data.comment or "").strip()
    # 驳回必须给出理由，否则学生不知道为什么被拒
    if not data.approved and not comment:
        return format_data.fail("驳回时必须填写驳回理由")
    conn = get_db()
    try:
        leave = conn.execute(
            "SELECT * FROM student_admin_service WHERE id=?", (data.leave_id,)).fetchone()
        if not leave:
            return format_data.not_found("请假申请不存在")
        # 功能权限通过后还必须校验数据归属。这里先用同一连接确认学生是否
        # 属于当前班主任，防止伪造 leave_id 审批其他班级的请假。
        if not can_access_student(conn, payload, leave["student_id"]):
            return format_data.forbidden("只能审批本人负责学生的请假")
        if leave["status"] != "待审批":
            return format_data.fail("该申请已处理，不能重复审批")
        status = "已通过" if data.approved else "已驳回"
        cursor = conn.execute(
            "UPDATE student_admin_service SET status=?, approver_id=?, "
            "approver_comment=?, approve_time=? WHERE id=? AND status='待审批'",
            (status, payload["id"], comment, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             data.leave_id))
        if cursor.rowcount == 0:
            conn.rollback()
            return format_data.conflict("该申请已被其他老师处理")
        conn.commit()
        # 通知学生审批结果
        from service.notify_logic import notify
        notify(leave["student_id"], "审批", "请假申请" + status,
               ("老师留言：" + comment) if comment else ("你的请假申请已" + status), "leaves")
        msg = "已通过该请假申请" + ("：" + comment if comment else "")
        if not data.approved:
            msg = "已驳回该请假申请，理由：" + comment
        return format_data.ok(None, msg)
    finally:
        conn.close()
