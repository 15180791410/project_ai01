from datetime import date, datetime
import re

from db import get_db
from service import rbac

# 功能：员工模糊搜索客户
def search_crm(name, owner_id=None):
    # 借个数据库连接
    conn = get_db()
    # 使用 SQL 的 LIKE 语法，只要名字里包含用户输入的字，就查出来
    sql = "SELECT * FROM crm_lead WHERE (customer_name LIKE ? OR background_info LIKE ?)"
    args = [f"%{name}%", f"%{name}%"]
    if owner_id:
        sql += " AND owner_employee_id=?"
        args.append(owner_id)
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def search_students_and_leads(raw_text, user):
    """统一搜索学生档案与 CRM 客户线索。

    员工说“罗同学”时既可能指学生，也可能指客户。旧实现只查 crm_lead，
    所以本校学生会被回答成“没有任何档案”。本函数分别返回 students 和
    leads，由上层明确区分两类数据，并继续执行 RBAC 数据范围。
    """
    name = _extract_person(raw_text)
    if not name:
        return {"query_name": "", "students": [], "leads": []}

    conn = get_db()
    try:
        students = []
        if rbac.has_capability(user, "student.read"):
            scope, args = rbac.student_scope(user)
            students = [dict(row) for row in conn.execute(
                "SELECT u.id, u.username, u.real_name, u.contact_info, "
                "COALESCE(NULLIF(u.target_country,''),'未填写') AS target_country, "
                "COALESCE(u.lifecycle_stage,'CONSULT') AS lifecycle_stage, "
                "u.advisor_id, u.homeroom_id "
                "FROM sys_user u WHERE u.user_type='STUDENT' "
                "AND (REPLACE(u.real_name,' ','') LIKE ? OR u.username LIKE ?)" + scope +
                " ORDER BY u.real_name, u.id",
                ["%" + name + "%", "%" + name + "%"] + list(args),
            ).fetchall()]

        leads = []
        if rbac.has_capability(user, "crm.read"):
            owner_id = user["id"] if rbac.effective_role(user) == "ADVISOR" else None
            leads = search_crm(name, owner_id)
        return {"query_name": name, "students": students, "leads": leads}
    finally:
        conn.close()

# 功能：员工录入新客户
def add_crm(customer_name, contact_info, owner_id, background_info="",
            follow_up_history="", target_country=""):
    conn = get_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cursor = conn.execute(
            "INSERT INTO crm_lead "
            "(customer_name, contact_info, background_info, follow_up_history, "
            "target_country, owner_employee_id, create_time, update_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (customer_name, contact_info, background_info, follow_up_history,
             target_country, owner_id, now, now),
        )
        conn.commit()
        record = {
            "id": cursor.lastrowid,
            "customer_name": customer_name,
            "contact_info": contact_info,
            "background_info": background_info,
            "follow_up_history": follow_up_history,
            "target_country": target_country,
            "owner_employee_id": owner_id,
            "status": "新增意向",
        }
    finally:
        conn.close()
    return record

# 功能：员工提交日报
def add_report(employee_id, content, report_date=""):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO employee_daily_report "
            "(employee_id, report_date, content, create_time) VALUES (?, ?, ?, ?)",
            (employee_id, report_date or date.today().isoformat(), content,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        record = {
            "id": cursor.lastrowid,
            "employee_id": employee_id,
            "report_date": report_date or date.today().isoformat(),
            "content": content,
        }
    finally:
        conn.close()
    return record

# 功能：员工审批学生请假
def approve_leave(leave_id, approver_id, is_approved, comment=""):
    conn = get_db()
    # 1. 先查请假单存不存在
    leave = conn.execute("SELECT * FROM student_admin_service WHERE id=?", (leave_id,)).fetchone()
    if not leave:
        conn.close()
        return None
        
    status_str = "已通过" if is_approved else "已驳回"
    if not is_approved and not comment.strip():
        conn.close()
        return {"status": "invalid", "msg": "驳回请假必须填写理由"}
    if leave["status"] != "待审批":
        conn.close()
        return {"status": "handled", "msg": "该申请已处理，不能重复审批"}
    cursor = conn.execute(
        "UPDATE student_admin_service SET status=?, approver_id=?, approver_comment=?, "
        "approve_time=? WHERE id=? AND status='待审批'",
        (status_str, approver_id, comment.strip(),
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), leave_id),
    )
    if cursor.rowcount == 0:
        conn.rollback()
        conn.close()
        return {"status": "handled", "msg": "该申请已被其他老师处理"}
    conn.commit()
    conn.close()
    return {"status": "success", "result": status_str, "student_id": leave["student_id"]}

# 功能：员工录入学生成绩
def add_score(student_id, course_name, score, semester):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO student_score (student_id, course_name, score, semester) "
            "VALUES (?, ?, ?, ?)",
            (student_id, course_name, score, semester),
        )
        conn.commit()
        return {
            "id": cursor.lastrowid,
            "student_id": student_id,
            "course_name": course_name,
            "score": score,
            "semester": semester,
        }
    finally:
        conn.close()

# ==========================================
# 新增补齐：7. 客户状态流转
# ==========================================
def update_lead_status(lead_id, new_status):
    # 需求规定：只能是这四种状态
    valid_status = ["新增意向", "跟进中", "已签约", "已流失"]
    if new_status not in valid_status:
        return "invalid"
    
    conn = get_db()
    lead = conn.execute("SELECT * FROM crm_lead WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        conn.close()
        return "not_found"
        
    conn.execute(
        "UPDATE crm_lead SET status=?, update_time=? WHERE id=?",
        (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), lead_id),
    )
    conn.commit()
    conn.close()
    return "success"

# ==========================================
# 新增补齐：8. 高级客户查询（按状态、负责员工过滤）
# ==========================================
def search_crm_advanced(status=None, owner_id=None, keyword=""):
    conn = get_db()
    sql = "SELECT * FROM crm_lead WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if owner_id:
        sql += " AND owner_employee_id = ?"
        params.append(owner_id)
    if keyword and keyword.strip():
        like = "%" + keyword.strip() + "%"
        sql += " AND (customer_name LIKE ? OR background_info LIKE ?)"
        params.extend([like, like])
    sql += " ORDER BY update_time DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]

# ==========================================
# 新增补齐：9. 解决售后工单并通知（FR-014）
# ==========================================
def resolve_feedback(ticket_id, solution):
    conn = get_db()
    ticket = conn.execute("SELECT * FROM student_feedback_ticket WHERE id=?", (ticket_id,)).fetchone()
    if not ticket:
        conn.close()
        return "not_found"
    if ticket["status"] == "已解决":
        conn.close()
        return "handled"
    
    # 通知由 API 层在同一请求中统一发送；这里只负责落库。
    cursor = conn.execute(
        "UPDATE student_feedback_ticket SET status='已解决', solution=? WHERE id=?",
        (solution, ticket_id),
    )
    if cursor.rowcount == 0:
        conn.rollback()
        conn.close()
        return "handled"
    conn.commit()
    conn.close()
    return {"status": "success", "student_id": ticket["student_id"]}

# ==========================================
# 新增补齐：10. 查询员工日报（FR-019）
# ==========================================
def get_reports(employee_id=None):
    conn = get_db()
    base_sql = (
        "SELECT r.*, u.real_name AS employee_name, u.username AS employee_username "
        "FROM employee_daily_report r "
        "LEFT JOIN sys_user u ON u.id=r.employee_id "
    )
    if employee_id:
        rows = conn.execute(
            base_sql + "WHERE r.employee_id=? ORDER BY r.report_date DESC, r.id DESC",
            (employee_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            base_sql + "ORDER BY r.report_date DESC, r.id DESC"
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

# ==========================================
# 新增补齐：11. 员工端按课程查成绩（FR-021）
# ==========================================
def search_scores(student_id=None, course_name=None):
    conn = get_db()
    sql = "SELECT * FROM student_score WHERE 1=1"
    params = []
    if student_id:
        sql += " AND student_id = ?"
        params.append(student_id)
    if course_name:
        sql += " AND course_name LIKE ?"
        params.append(f"%{course_name}%")
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ==========================================
# 新增：NL2SQL 指令处理 (对应需求 FR-022)
# ==========================================
def _extract_person(command):
    clean = command
    for phrase in (
        "帮我", "请", "把", "将", "查一下", "查询", "查看", "查",
        "的跟进记录", "跟进记录", "目标状态", "的状态", "状态", "改成", "更新", "设为", "转为",
        "客户", "同学", "档案", "资料", "信息",
    ):
        clean = clean.replace(phrase, "")
    clean = re.sub(r"[，。,.！!？?\s]", "", clean).strip()
    return clean


def _extract_first_int(command):
    match = re.search(r"(?:工单|请假|ID|编号)?\s*(\d+)", command, re.I)
    return int(match.group(1)) if match else None


def handle_nl2sql(command, user=None):
    """执行员工自然语言业务指令，并在每个分支应用功能与数据范围。

    旧实现直接查询全表，任何员工只要会发指令就能看到所有客户、请假和
    心理相关内容。现在按统一 RBAC 矩阵执行：
    - 顾问：仅本人客户、本人学生成绩/申请/报名；
    - 班主任：仅本人班级请假、成绩、售后，不可访问 CRM；
    - 领导和管理员：可执行全部业务指令；
    - 其他后台角色：拒绝业务指令。
    """
    role = rbac.effective_role(user)

    def deny(intent, message):
        """统一返回权限拒绝结构，API 层据此生成 HTTP 403。"""
        return {
            "intent": intent,
            "sql": "无",
            "data": [],
            "msg": message,
            "denied": True,
        }

    def keep(intent, sql, rows):
        """把 sqlite Row 转成普通字典，并保持原有接口格式。"""
        data = [dict(row) for row in rows]
        return {
            "intent": intent,
            "sql": sql,
            "data": data,
            "msg": (
                f"成功执行：{intent}，查到 {len(data)} 条记录。"
                if data else f"未找到匹配记录（{intent}）。"
            ),
        }

    conn = get_db()
    try:
        if "查" in command and ("客户" in command or "同学" in command or "跟进记录" in command):
            intent = "查询客户"
            if not rbac.has_capability(user, "crm.read"):
                return deny(intent, "当前角色无权查询客户线索。")
            name = _extract_person(command)
            if not name:
                return deny(intent, "未识别到客户姓名，请明确说出姓名。")
            sql = "SELECT * FROM crm_lead WHERE customer_name LIKE ?"
            args = ["%" + name + "%"]
            if role == "ADVISOR":
                sql += " AND owner_employee_id=?"
                args.append(user["id"])
            return keep(intent, sql, conn.execute(sql, args).fetchall())

        if "查" in command and ("成绩" in command or "分数" in command):
            intent = "查询成绩"
            if not rbac.has_capability(user, "score.read"):
                return deny(intent, "当前角色无权查询学生成绩。")
            student_id = _extract_first_int(command)
            course = ""
            match = re.search(r"(?:课程|科目)(?:是|为)?([\u4e00-\u9fa5A-Za-z0-9]+)", command)
            if match:
                course = match.group(1)
            scope, scope_args = rbac.student_scope(user)
            sql = (
                "SELECT s.*, u.real_name AS student_name "
                "FROM student_score s JOIN sys_user u ON u.id=s.student_id "
                "WHERE 1=1" + scope
            )
            args = list(scope_args)
            if student_id:
                sql += " AND s.student_id=?"
                args.append(student_id)
            if course:
                sql += " AND s.course_name LIKE ?"
                args.append("%" + course + "%")
            return keep(intent, sql, conn.execute(sql, args).fetchall())

        if "查" in command and ("日报" in command or "汇报" in command):
            intent = "查询日报"
            if not rbac.has_capability(user, "report.self"):
                return deny(intent, "当前角色无权查询员工日报。")
            sql = "SELECT * FROM employee_daily_report"
            args = []
            if role not in ("ADMIN", "LEADER"):
                sql += " WHERE employee_id=?"
                args.append(user["id"])
            sql += " ORDER BY report_date DESC LIMIT 100"
            return keep(intent, sql, conn.execute(sql, args).fetchall())

        if "查" in command and "请假" in command:
            intent = "查询请假"
            if not rbac.has_capability(user, "leave.read"):
                return deny(intent, "当前角色无权查询学生请假。")
            scope, args = rbac.student_scope(user)
            sql = (
                "SELECT a.*, u.real_name AS student_name "
                "FROM student_admin_service a JOIN sys_user u ON u.id=a.student_id "
                "WHERE 1=1" + scope
            )
            for candidate in ("待审批", "已通过", "已驳回"):
                if candidate in command:
                    sql += " AND a.status=?"
                    args.append(candidate)
                    break
            sql += " ORDER BY a.create_time DESC LIMIT 100"
            return keep(intent, sql, conn.execute(sql, args).fetchall())

        if "查" in command and ("投诉" in command or "反馈" in command):
            intent = "查询投诉"
            if not rbac.has_capability(user, "feedback.resolve"):
                return deny(intent, "当前角色无权查询学生售后工单。")
            scope, args = rbac.student_scope(user)
            sql = (
                "SELECT t.*, u.real_name AS student_name "
                "FROM student_feedback_ticket t JOIN sys_user u ON u.id=t.student_id "
                "WHERE 1=1" + scope
            )
            if "待处理" in command:
                sql += " AND t.status=?"
                args.append("待处理")
            sql += " ORDER BY t.create_time DESC LIMIT 100"
            return keep(intent, sql, conn.execute(sql, args).fetchall())

        if ("通过" in command or "批准" in command or "驳回" in command) and "请假" in command:
            intent = "审批请假"
            if not rbac.has_capability(user, "leave.approve"):
                return deny(intent, "当前角色无权审批请假。")
            leave_id = _extract_first_int(command)
            if not leave_id:
                return deny(intent, "未识别到请假单 ID。")
            approved = "驳回" not in command
            comment = ""
            match = re.search(r"(?:理由|备注)[是为：:]\s*(.+)$", command)
            if match:
                comment = match.group(1).strip()
            if not approved and not comment:
                return deny(intent, "驳回请假必须填写理由。")
            scope, scope_args = rbac.student_scope(user)
            row = conn.execute(
                "SELECT a.* FROM student_admin_service a "
                "JOIN sys_user u ON u.id=a.student_id "
                "WHERE a.id=?" + scope,
                [leave_id] + list(scope_args),
            ).fetchone()
            if not row:
                return deny(intent, "请假申请不存在，或不在当前数据范围内。")
            if row["status"] != "待审批":
                return deny(intent, "该请假已处理，不能重复审批。")
            status = "已通过" if approved else "已驳回"
            conn.execute(
                "UPDATE student_admin_service SET status=?, approver_id=?, "
                "approver_comment=?, approve_time=? "
                "WHERE id=? AND status='待审批'",
                (status, user["id"], comment,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), leave_id),
            )
            conn.commit()
            sql = (
                "UPDATE student_admin_service SET status=?, approver_id=?, "
                "approver_comment=?, approve_time=? WHERE id=? AND status='待审批'"
            )
            return keep(intent, sql, [{"leave_id": leave_id, "status": status}])

        if ("状态" in command and ("客户" in command or "线索" in command)
                and any(word in command for word in ("改成", "更新", "设为", "转为"))):
            intent = "更新客户状态"
            if not rbac.has_capability(user, "crm.manage"):
                return deny(intent, "当前角色无权更新客户线索。")
            name = _extract_person(command.split("改成")[0].split("更新")[0])
            new_status = next(
                (s for s in ("新增意向", "跟进中", "已签约", "已流失") if s in command),
                "",
            )
            if not name or not new_status:
                return deny(intent, "请明确客户姓名和目标状态。")
            sql_find = "SELECT id FROM crm_lead WHERE customer_name LIKE ?"
            args = ["%" + name + "%"]
            if role == "ADVISOR":
                sql_find += " AND owner_employee_id=?"
                args.append(user["id"])
            row = conn.execute(sql_find + " LIMIT 1", args).fetchone()
            if not row:
                return deny(intent, "未找到该客户，或该客户不属于当前员工。")
            conn.execute(
                "UPDATE crm_lead SET status=?, update_time=? WHERE id=?",
                (new_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), row["id"]),
            )
            conn.commit()
            sql = "UPDATE crm_lead SET status=?, update_time=? WHERE id=?"
            return keep(intent, sql, [{"lead_id": row["id"], "status": new_status}])

        if ("状态" in command and ("投诉" in command or "工单" in command)
                and any(word in command for word in ("改成", "更新", "设为", "解决"))):
            intent = "更新投诉状态"
            if not rbac.has_capability(user, "feedback.resolve"):
                return deny(intent, "当前角色无权更新售后工单。")
            ticket_id = _extract_first_int(command)
            if not ticket_id:
                return deny(intent, "请明确售后工单 ID。")
            status = "已解决" if "解决" in command else (
                "处理中" if "处理中" in command else "待处理"
            )
            scope, scope_args = rbac.student_scope(user)
            row = conn.execute(
                "SELECT t.id FROM student_feedback_ticket t "
                "JOIN sys_user u ON u.id=t.student_id "
                "WHERE t.id=?" + scope,
                [ticket_id] + list(scope_args),
            ).fetchone()
            if not row:
                return deny(intent, "售后工单不存在，或不在当前数据范围内。")
            conn.execute("UPDATE student_feedback_ticket SET status=? WHERE id=?", (status, ticket_id))
            conn.commit()
            sql = "UPDATE student_feedback_ticket SET status=? WHERE id=?"
            return keep(intent, sql, [{"ticket_id": ticket_id, "status": status}])

        if "统计" in command or "有多少" in command:
            if "客户" in command or "线索" in command:
                intent = "统计客户"
                if not rbac.has_capability(user, "crm.read"):
                    return deny(intent, "当前角色无权统计客户线索。")
                sql = "SELECT status, COUNT(*) AS count FROM crm_lead"
                args = []
                if role == "ADVISOR":
                    sql += " WHERE owner_employee_id=?"
                    args.append(user["id"])
                sql += " GROUP BY status ORDER BY status"
                return keep(intent, sql, conn.execute(sql, args).fetchall())

            if "学生" in command:
                intent = "统计学生"
                if not rbac.has_capability(user, "student.read"):
                    return deny(intent, "当前角色无权统计学生。")
                scope, args = rbac.student_scope(user)
                sql = (
                    "SELECT COALESCE(NULLIF(u.target_country,''),'未填写') AS country, "
                    "COUNT(*) AS count FROM sys_user u "
                    "WHERE u.user_type='STUDENT'" + scope +
                    " GROUP BY country ORDER BY count DESC"
                )
                return keep(intent, sql, conn.execute(sql, args).fetchall())

            intent = "统计活动报名"
            if not rbac.has_capability(user, "event_registration.read"):
                return deny(intent, "当前角色无权统计活动报名。")
            sql = (
                "SELECT r.event_id, e.event_name, COUNT(*) AS count "
                "FROM event_registration r "
                "LEFT JOIN event_lecture e ON e.id=r.event_id "
                "LEFT JOIN sys_user u ON u.id=r.customer_id "
                "LEFT JOIN crm_lead l ON l.id=r.lead_id WHERE 1=1"
            )
            args = []
            if role == "ADVISOR":
                sql += " AND (u.advisor_id=? OR l.owner_employee_id=?)"
                args.extend([user["id"], user["id"]])
            sql += " GROUP BY r.event_id, e.event_name ORDER BY count DESC"
            return keep(intent, sql, conn.execute(sql, args).fetchall())

        return {
            "intent": "未知", "sql": "无", "data": [],
            "msg": "没听懂指令。支持：查客户、查成绩、查日报、查请假、查投诉、审批请假、更新客户/投诉状态。",
        }
    finally:
        conn.close()
