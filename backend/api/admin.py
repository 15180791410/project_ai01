from fastapi import APIRouter, Header
from db import get_db
from utils import format_data
from api.auth import (can_access_student, require_capability, request_token,
                      staff_role, student_scope)

router = APIRouter()

@router.get("/alerts", summary="查询心理预警列表（含学生姓名）")
def get_alerts(authorization: str = Header(default=""), token: str = ""):
    """心理预警只对有学生关怀职责的角色开放。

    顾问可只读本人负责学生的预警，便于和班主任协同；处理操作仍只对
    班主任、管理员和领导开放。维护人员没有业务数据权限。
    """
    user, err = require_capability(
        request_token(authorization, token), "alert.read"
    )
    if err:
        return err
    try:
        conn = get_db()
        scope, args = student_scope(user)
        rows = conn.execute(
            "SELECT a.*, u.real_name AS student_name, u.username AS student_username "
            "FROM student_psych_alert a "
            "LEFT JOIN sys_user u ON u.id = a.student_id WHERE 1=1" + scope +
            " ORDER BY a.create_time DESC", tuple(args)).fetchall()
        conn.close()
        return format_data.ok([dict(row) for row in rows])
    except Exception as e:
        print("查询预警失败:", repr(e))
        return format_data.server_error()

@router.put("/handle_alert", summary="处理心理预警")
def handle_alert(alert_id: int, authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "alert.handle"
    )
    if err:
        return err
    try:
        conn = get_db()
        alert = conn.execute(
            "SELECT a.*,u.homeroom_id FROM student_psych_alert a "
            "JOIN sys_user u ON u.id=a.student_id WHERE a.id=?",
            (alert_id,),
        ).fetchone()
        if not alert:
            return format_data.not_found("预警记录不存在")
        # 数据归属校验必须放在更新数据库之前。旧代码把这段判断写在了
        # return 之后，永远执行不到，是实际存在的越权漏洞。
        if not can_access_student(conn, user, alert["student_id"]):
            return format_data.forbidden("只能处理本人负责学生的预警")
        conn.execute(
            "UPDATE student_psych_alert SET status='已解决', handler_id=? WHERE id=?",
            (user["id"], alert_id),
        )
        conn.commit()
        from service.notify_logic import notify
        notify(alert["student_id"], "预警", "老师已关注你的情况",
               "你之前的情绪提醒已由老师跟进处理。", "emotion")
        return format_data.ok(msg="预警已标记为处理完毕")
    except Exception as e:
        print("处理预警失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：管理后台看所有客户
# ==========================================
@router.get("/leads", summary="查看所有客户")
def get_all_leads(authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "crm.read"
    )
    if err:
        return err
    try:
        conn = get_db()
        role = staff_role(user)
        if role in ("ADMIN", "LEADER"):
            rows = conn.execute(
                "SELECT l.*, u.real_name AS owner_name FROM crm_lead l "
                "LEFT JOIN sys_user u ON u.id=l.owner_employee_id "
                "ORDER BY l.update_time DESC, l.id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT l.*, u.real_name AS owner_name FROM crm_lead l "
                "LEFT JOIN sys_user u ON u.id=l.owner_employee_id "
                "WHERE l.owner_employee_id=? "
                "ORDER BY l.update_time DESC, l.id DESC",
                (user["id"],),
            ).fetchall()
        conn.close()
        return format_data.ok([dict(row) for row in rows])
    except Exception as e:
        print("查询客户失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：管理后台看所有成绩
# ==========================================
@router.get("/scores", summary="按学生姓名、国家和课程查询成绩")
def get_all_scores(student_id: int = 0, keyword: str = "",
                   country: str = "", course_name: str = "",
                   authorization: str = Header(default=""), token: str = ""):
    """成绩查询必须同时返回学生姓名、账号和意向国家，并支持组合筛选。"""
    user, err = require_capability(
        request_token(authorization, token), "score.read"
    )
    if err:
        return err
    try:
        conn = get_db()
        scope, args = student_scope(user)
        sql = (
            "SELECT s.*, u.real_name AS student_name, u.username AS student_username, "
            "COALESCE(NULLIF(u.target_country,''),'未填写') AS target_country "
            "FROM student_score s JOIN sys_user u ON u.id=s.student_id "
            "WHERE u.user_type='STUDENT'" + scope
        )
        if student_id:
            sql += " AND s.student_id=?"
            args.append(student_id)
        if keyword.strip():
            sql += " AND (u.real_name LIKE ? OR u.username LIKE ?)"
            like = "%" + keyword.strip() + "%"
            args += [like, like]
        if country.strip():
            sql += " AND COALESCE(NULLIF(u.target_country,''),'未填写')=?"
            args.append(country.strip())
        if course_name.strip():
            sql += " AND s.course_name LIKE ?"
            args.append("%" + course_name.strip() + "%")
        sql += " ORDER BY target_country, u.real_name, s.course_name, s.id"
        rows = conn.execute(sql, tuple(args)).fetchall()
        conn.close()
        return format_data.ok([dict(row) for row in rows])
    except Exception as e:
        print("查询成绩失败:", repr(e))
        return format_data.server_error()
