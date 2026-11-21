"""员工端接口：负责 CRM、日报、成绩、审批和自然语言业务指令。"""
from fastapi import APIRouter, Header
from pydantic import BaseModel
from service import employee_logic
from service.notify_logic import notify
from utils import format_data
from api.auth import (can_access_student, current_user, require_capability,
                      request_token, staff_role, student_scope)
from service import rbac
from db import get_db

router = APIRouter()

# 定义数据模型
class LeadData(BaseModel):
    token: str
    customer_name: str
    contact_info: str
    owner_employee_id: int = 0
    background_info: str = ""
    follow_up_history: str = ""

class ReportData(BaseModel):
    token: str
    content: str
    report_date: str = ""

class ApproveData(BaseModel):
    token: str
    leave_id: int
    is_approved: bool
    comment: str = ""

class ScoreData(BaseModel):
    token: str
    student_id: int
    course_name: str
    score: float
    semester: str = ""

@router.get("/search", summary="统一搜索学生档案与意向客户")
def search(name: str, authorization: str = Header(default=""), token: str = ""):
    user, err = current_user(request_token(authorization, token))
    if err:
        return err
    if not (
        rbac.has_capability(user, "student.read")
        or rbac.has_capability(user, "crm.read")
    ):
        return format_data.forbidden("当前角色无权搜索学生或客户")
    try:
        result = employee_logic.search_students_and_leads(name, user)
        return format_data.ok(result)
    except Exception as e:
        print("统一搜索失败:", repr(e))
        return format_data.server_error()

@router.post("/add_lead", summary="录入意向客户")
def add_lead(data: LeadData):
    user, err = require_capability(data.token, "crm.manage")
    if err:
        return err
    if not data.customer_name.strip() or not data.contact_info.strip():
        return format_data.fail("客户姓名和联系方式不能为空")
    conn = get_db()
    try:
        if data.owner_employee_id and not conn.execute(
            "SELECT 1 FROM sys_user WHERE id=? AND user_type IN ('EMPLOYEE','TEACHER') "
            "AND status='正常'",
            (data.owner_employee_id,),
        ).fetchone():
            return format_data.fail("负责员工不存在或已停用")
    finally:
        conn.close()
    try:
        owner_id = data.owner_employee_id or user["id"]
        if staff_role(user) == "ADVISOR" and owner_id != user["id"]:
            return format_data.forbidden("顾问只能把新客户分配给自己")
        record = employee_logic.add_crm(
            data.customer_name.strip(), data.contact_info.strip(), owner_id,
            data.background_info.strip(), data.follow_up_history.strip(),
        )
        return format_data.ok(record, "录入成功")
    except Exception as e:
        print("录入客户失败:", repr(e))
        return format_data.server_error()

@router.post("/report", summary="提交员工日报")
def add_report(data: ReportData):
    user, err = require_capability(data.token, "report.self")
    if err:
        return err
    if not data.content.strip():
        return format_data.fail("日报内容不能为空")
    try:
        record = employee_logic.add_report(user["id"], data.content, data.report_date)
        return format_data.ok(record, "日报提交成功")
    except Exception as e:
        print("提交日报失败:", repr(e))
        return format_data.server_error()

@router.put("/approve_leave", summary="审批学生请假")
def approve_leave(data: ApproveData):
    user, err = require_capability(data.token, "leave.approve")
    if err:
        return err
    try:
        scope_conn = get_db()
        try:
            leave_student = scope_conn.execute(
                "SELECT student_id FROM student_admin_service WHERE id=?",
                (data.leave_id,),
            ).fetchone()
            if not leave_student:
                return format_data.not_found("请假申请不存在")
            if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
                scoped = scope_conn.execute(
                    "SELECT 1 FROM sys_user WHERE id=? AND homeroom_id=?",
                    (leave_student["student_id"], user["id"]),
                ).fetchone()
                if not scoped:
                    return format_data.forbidden("只能审批本人负责学生的请假")
        finally:
            scope_conn.close()
        res = employee_logic.approve_leave(
            data.leave_id, user["id"], data.is_approved, data.comment
        )
        if res is None:
            return format_data.not_found("请假申请不存在")
        if res.get("status") == "invalid":
            return format_data.fail(res["msg"])
        if res.get("status") == "handled":
            return format_data.conflict(res["msg"])
        notify(
            res["student_id"], "审批", "请假申请" + res["result"],
            "审批意见：" + (data.comment.strip() or "无"), "leaves",
        )
        return format_data.ok(msg=f"审批完成：{res['result']}")
    except Exception as e:
        print("旧版审批失败:", repr(e))
        return format_data.server_error()

@router.post("/add_score", summary="录入学生成绩")
def add_score(data: ScoreData):
    user, err = require_capability(data.token, "score.manage")
    if err:
        return err
    if not 0 <= data.score <= 100:
        return format_data.fail("成绩必须在 0 到 100 之间")
    if not data.course_name.strip():
        return format_data.fail("课程名称不能为空")
    conn = get_db()
    try:
        student = conn.execute(
            "SELECT 1 FROM sys_user WHERE id=? AND user_type='STUDENT'",
            (data.student_id,),
        ).fetchone()
        if not student:
            return format_data.fail("学生不存在")
        if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
            scope, args = student_scope(user)
            scoped = conn.execute(
                "SELECT 1 FROM sys_user u WHERE u.id=?" + scope,
                [data.student_id] + args,
            ).fetchone()
            if not scoped:
                return format_data.forbidden("只能录入本人负责学生的成绩")
    finally:
        conn.close()
    try:
        record = employee_logic.add_score(
            data.student_id, data.course_name, data.score, data.semester
        )
        return format_data.ok(record, "成绩录入成功")
    except Exception as e:
        print("录入成绩失败:", repr(e))
        return format_data.server_error()


# ==========================================
# 新增接口：客户状态流转
# ==========================================
class LeadStatusData(BaseModel):
    token: str
    lead_id: int
    new_status: str


@router.put("/update_lead_status", summary="更新客户状态")
def update_lead_status(data: LeadStatusData):
    user, err = require_capability(data.token, "crm.manage")
    if err:
        return err
    try:
        if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
            conn = get_db()
            try:
                owned = conn.execute(
                    "SELECT 1 FROM crm_lead WHERE id=? AND owner_employee_id=?",
                    (data.lead_id, user["id"]),
                ).fetchone()
                if not owned:
                    return format_data.forbidden("只能更新本人负责的客户")
            finally:
                conn.close()
        res = employee_logic.update_lead_status(data.lead_id, data.new_status)
        if res == "invalid":
            return format_data.fail("状态不合法，只能是：新增意向/跟进中/已签约/已流失")
        elif res == "not_found":
            return format_data.not_found("客户不存在")
        return format_data.ok(msg="状态更新成功")
    except Exception as e:
        print("更新客户状态失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：高级客户查询
# ==========================================
@router.get("/search_advanced", summary="高级搜索客户")
def search_advanced(status: str | None = None, owner_id: int | None = None,
                    keyword: str = "",
                    authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "crm.read"
    )
    if err:
        return err
    if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
        owner_id = user["id"]
    try:
        res = employee_logic.search_crm_advanced(status, owner_id, keyword)
        return format_data.ok(res)
    except Exception as e:
        print("高级搜索失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：解决售后工单
# ==========================================
@router.get("/feedbacks", summary="查询负责范围内的售后工单")
def list_feedbacks(status: str = "", keyword: str = "", study_country: str = "",
                   authorization: str = Header(default=""), token: str = ""):
    """按角色数据范围查询学生售后工单。

    管理员/领导查看全部；班主任查看本班；顾问查看本人负责学生。
    筛选条件全部使用参数绑定，不允许前端拼接 SQL。
    """
    user, err = require_capability(
        request_token(authorization, token), "feedback.resolve"
    )
    if err:
        return err
    if status and status not in ("待处理", "处理中", "已解决"):
        return format_data.fail("售后状态无效")
    conn = get_db()
    try:
        scope, args = student_scope(user)
        # 售后只对已入学学生开放，因此这里展示申请档案中的实际就读国家。
        # target_country 是咨询意向，不能拿来冒充已经入学的国家。
        study_country_expr = (
            "COALESCE(NULLIF(("
            "SELECT a.country FROM application_progress a "
            "WHERE a.student_id=u.id AND COALESCE(a.country,'')<>'' "
            "ORDER BY CASE a.status WHEN '已完成' THEN 0 "
            "WHEN '进行中' THEN 1 ELSE 2 END, "
            "a.update_time DESC, a.id DESC LIMIT 1"
            "),''),'未关联')"
        )
        sql = (
            "SELECT t.*, u.real_name AS student_name, u.username AS student_username, "
            "u.contact_info AS student_contact, "
            "COALESCE(NULLIF(u.target_country,''),'未填写') AS intended_country, "
            + study_country_expr + " AS study_country "
            "FROM student_feedback_ticket t "
            "JOIN sys_user u ON u.id=t.student_id "
            "WHERE u.user_type='STUDENT'" + scope
        )
        if status:
            sql += " AND t.status=?"
            args.append(status)
        if keyword.strip():
            like = "%" + keyword.strip() + "%"
            sql += (" AND (u.real_name LIKE ? OR u.username LIKE ? "
                    "OR t.content LIKE ? OR COALESCE(t.detail,'') LIKE ?)")
            args.extend([like, like, like, like])
        if study_country.strip():
            sql += " AND " + study_country_expr + "=?"
            args.append(study_country.strip())
        sql += (
            " ORDER BY CASE t.status WHEN '待处理' THEN 0 "
            "WHEN '处理中' THEN 1 ELSE 2 END, t.create_time DESC, t.id DESC"
        )
        return format_data.ok([
            dict(row) for row in conn.execute(sql, tuple(args)).fetchall()
        ])
    finally:
        conn.close()


class ResolveFeedbackData(BaseModel):
    token: str
    ticket_id: int
    solution: str


@router.put("/resolve_feedback", summary="解决售后工单并通知")
def resolve_feedback(data: ResolveFeedbackData):
    user, err = require_capability(data.token, "feedback.resolve")
    if err:
        return err
    if not data.solution.strip():
        return format_data.fail("解决方案不能为空")
    try:
        conn = get_db()
        try:
            ticket = conn.execute(
                "SELECT t.student_id FROM student_feedback_ticket t "
                "JOIN sys_user u ON u.id=t.student_id WHERE t.id=?",
                (data.ticket_id,),
            ).fetchone()
            if not ticket:
                return format_data.not_found("工单不存在")
            # 顾问/班主任的数据归属不同，统一由 can_access_student 判断，
            # 不能只按 homeroom_id 判断，否则顾问拥有权限后仍无法处理。
            if not can_access_student(conn, user, ticket["student_id"]):
                return format_data.forbidden("只能处理本人负责学生的售后")
        finally:
            conn.close()
        res = employee_logic.resolve_feedback(data.ticket_id, data.solution)
        if isinstance(res, str) and res == "not_found":
            return format_data.not_found("工单不存在")
        if isinstance(res, str) and res == "handled":
            return format_data.conflict("该工单已经解决，不能重复处理")
        notified = notify(
            res["student_id"], "售后", "售后工单已解决",
            data.solution.strip() or "工作人员已处理你的反馈。", "feedback",
        )
        if notified:
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE student_feedback_ticket SET is_notified=1 WHERE id=?",
                    (data.ticket_id,),
                )
                conn.commit()
            finally:
                conn.close()
        return format_data.ok(msg="工单已解决，并通知了学生")
    except Exception as e:
        print("解决售后失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：查询日报
# ==========================================
@router.get("/reports", summary="查询员工日报")
def get_reports(employee_id: int | None = None,
                authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "report.self"
    )
    if err:
        return err
    if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
        employee_id = user["id"]
    try:
        res = employee_logic.get_reports(employee_id)
        return format_data.ok(res)
    except Exception as e:
        print("查询日报失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：员工端查成绩
# ==========================================
@router.get("/search_scores", summary="查询学生成绩")
def search_scores(student_id: int | None = None, course_name: str | None = None,
                  authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "score.read"
    )
    if err:
        return err
    try:
        if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
            conn = get_db()
            try:
                scope, args = student_scope(user)
                allowed_ids = [r["id"] for r in conn.execute(
                    "SELECT u.id FROM sys_user u WHERE u.user_type='STUDENT'" + scope,
                    tuple(args),
                ).fetchall()]
            finally:
                conn.close()
            if student_id and student_id not in allowed_ids:
                return format_data.forbidden("只能查询本人负责学生的成绩")
            if not student_id and not allowed_ids:
                return format_data.ok([])
            if not student_id:
                placeholders = ",".join("?" * len(allowed_ids))
                conn = get_db()
                try:
                    sql = "SELECT * FROM student_score WHERE student_id IN (" + placeholders + ")"
                    if course_name:
                        sql += " AND course_name LIKE ?"
                        values = allowed_ids + ["%" + course_name + "%"]
                    else:
                        values = allowed_ids
                    return format_data.ok([
                        dict(r) for r in conn.execute(sql, values).fetchall()
                    ])
                finally:
                    conn.close()
        res = employee_logic.search_scores(student_id, course_name)
        return format_data.ok(res)
    except Exception as e:
        print("员工查成绩失败:", repr(e))
        return format_data.server_error()


# ==========================================
# 新增接口：NL2SQL
# ==========================================
# 规定前端传过来的格式
class CommandData(BaseModel):
    token: str
    command: str

@router.post("/nl2sql", summary="处理NL2SQL自然语言指令")
def nl2sql(data: CommandData):
    user, err = require_capability(data.token, "business.overview")
    if err:
        return err
    if not data.command.strip():
        return format_data.fail("指令不能为空")
    try:
        res = employee_logic.handle_nl2sql(data.command, user)
        if res.get("denied"):
            return format_data.forbidden(res.get("msg") or "当前角色无权执行该指令")
        return format_data.ok(res)
    except Exception as e:
        print("NL2SQL 失败:", repr(e))
        return format_data.server_error()
