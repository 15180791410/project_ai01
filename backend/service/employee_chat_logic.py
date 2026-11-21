"""员工助手的确定性业务问答。

这里处理“员工名单、我的班级、权限能力、所有学生、业务汇总、客户状态、
学生档案”等必须来自当前业务数据库的问题。它不生成知识库内容，也不替代
Dify 的政策/制度问答；命中这些结构化问题后直接返回真实数据，避免大模型
把上一轮成绩上下文错误复用到下一轮。
"""
from __future__ import annotations

import re
from datetime import datetime

from db import get_db
from service import lifecycle, rbac
from service.notify_logic import notify


STAFF_ROLE_LABELS = {
    "ADVISOR": "顾问",
    "HOMEROOM": "班主任",
    "LEADER": "领导",
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _md_table(headers, rows):
    """生成安全的 Markdown 表格，单元格内换行和竖线会被压平。"""
    def clean(value):
        return str(value if value is not None else "").replace("|", "／").replace("\n", " ")

    lines = [
        "| " + " | ".join(clean(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(clean(item) for item in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def _student_rows(conn, user):
    """读取当前员工数据范围内的学生。"""
    scope, args = rbac.student_scope(user)
    return conn.execute(
        "SELECT u.id,u.username,u.real_name,u.contact_info,u.target_country,"
        "       COALESCE(u.lifecycle_stage,'CONSULT') AS lifecycle_stage,"
        "       a.real_name AS advisor_name,h.real_name AS homeroom_name "
        "FROM sys_user u "
        "LEFT JOIN sys_user a ON a.id=u.advisor_id "
        "LEFT JOIN sys_user h ON h.id=u.homeroom_id "
        "WHERE u.user_type='STUDENT'" + scope +
        " ORDER BY u.real_name,u.id",
        tuple(args),
    ).fetchall()


def _student_table(rows):
    table_rows = []
    for row in rows[:50]:
        stage_key = str(row["lifecycle_stage"] or "CONSULT")
        stage = lifecycle.STAGE_LABEL.get(stage_key, stage_key)
        table_rows.append((
            row["real_name"] or row["username"],
            row["username"] or "—",
            stage,
            row["target_country"] or "未填写",
            row["advisor_name"] or "未分配",
        ))
    result = _md_table(
        ("学生姓名", "账号", "学籍阶段", "意向国家", "顾问"),
        table_rows,
    )
    if len(rows) > 50:
        result += "\n\n其余 " + str(len(rows) - 50) + " 名学生请在「学生分布」页面查看。"
    return result


def _handle_staff_directory(conn, query):
    """回答公司内有哪些顾问/班主任/领导。"""
    target_role = next(
        (role for word, role in (
            ("班主任", "HOMEROOM"),
            ("顾问", "ADVISOR"),
            ("领导", "LEADER"),
        ) if word in query),
        "",
    )
    asks_list = any(word in query for word in (
        "哪些", "是谁", "名单", "有谁", "多少人", "几位",
    ))
    if not target_role or not asks_list:
        return None
    rows = conn.execute(
        "SELECT real_name,username,department FROM sys_user "
        "WHERE user_type IN ('EMPLOYEE','TEACHER') AND staff_role=? "
        "AND status='正常' ORDER BY id",
        (target_role,),
    ).fetchall()
    if not rows:
        return "当前没有在职的" + STAFF_ROLE_LABELS[target_role] + "账号。"
    table = _md_table(
        ("姓名", "部门"),
        (
            (row["real_name"] or row["username"], row["department"] or "部门未填写")
            for row in rows
        ),
    )
    return "当前在职" + STAFF_ROLE_LABELS[target_role] + "共 " + str(len(rows)) + " 人：\n\n" + table


def _capability_answer(user):
    """按当前角色返回真实可用的助手能力，避免泛化承诺。"""
    role = rbac.effective_role(user)
    if role == "HOMEROOM":
        abilities = [
            "查看本人班级学生和学籍阶段",
            "查询、录入本人班级学生成绩",
            "查询并审批本人班级请假",
            "查看并处理本人班级心理预警",
            "查询并处理本人班级售后工单",
            "查看本人班级申请进度、写工作日报",
        ]
    elif role == "ADVISOR":
        abilities = [
            "查看本人学生和客户线索",
            "查询、录入本人学生成绩",
            "跟进客户、推进留学申请",
            "查看活动及本人线索的报名名单",
            "查看本人学生心理预警和售后工单",
        ]
    elif role == "LEADER":
        abilities = [
            "查看全部学生、客户线索和申请",
            "查看并处理全部心理预警、售后和成绩",
            "查询全部活动和报名名单",
            "查看工作日报与业务汇总",
        ]
    elif role == "ADMIN":
        abilities = [
            "查看全部业务数据",
            "管理活动、员工账号和角色",
            "查询学生、客户、成绩、请假、预警、售后和申请",
        ]
    else:
        abilities = ["查看系统运行状态"]
    lines = ["当前角色可以这样使用员工助手："]
    lines.extend(str(index) + ". " + text for index, text in enumerate(abilities, 1))
    lines.append("涉及公司政策、费用、制度和项目规则的问题会结合公司业务资料回答。")
    return "\n".join(lines)


def _handle_class_scope(conn, user, query):
    """回答班主任的带班范围。系统当前没有行政班名称字段，不编造班名。"""
    if not any(word in query for word in ("班级", "带班")):
        return None
    if not any(word in query for word in ("我负责", "我的", "哪个", "哪班", "带哪个")):
        return None
    rows = _student_rows(conn, user)
    role = rbac.effective_role(user)
    if role == "HOMEROOM":
        teacher_name = str(user.get("real_name") or user.get("username") or "当前班主任")
        header = (
            "你当前负责「" + teacher_name + "所带班级」，共 "
            + str(len(rows)) + " 名学生（系统按班主任归属关系划班）："
        )
    elif role == "ADVISOR":
        header = "你是顾问，当前按顾问归属关系负责 " + str(len(rows)) + " 名学生："
    else:
        header = "当前角色可查看 " + str(len(rows)) + " 名学生："
    if not rows:
        return header + "暂无学生。"
    return header + "\n\n" + _student_table(rows)


def _handle_all_students(conn, user, query):
    compact = re.sub(r"[\s，。,.！!？?、：:；;]", "", query)
    asks_students = "学生" in compact
    asks_scope = any(word in compact for word in ("所有", "全部", "名单", "哪些", "情况", "信息"))
    if not (asks_students and asks_scope):
        return None
    # “所有学生”只是范围词，提问对象仍可能是请假、成绩、售后等具体业务。
    # 这些请求必须交给各自的业务处理器，不能统一下沉成学生名单。
    business_topics = (
        "请假", "审批", "成绩", "课程", "售后", "反馈", "预警",
        "客户", "线索", "活动", "申请", "毕业", "日报",
    )
    if any(topic in compact for topic in business_topics):
        return None
    rows = _student_rows(conn, user)
    role = rbac.effective_role(user)
    scope_text = {
        "HOMEROOM": "你负责的班级学生",
        "ADVISOR": "你负责的学生",
    }.get(role, "你可查看的学生")
    if not rows:
        return scope_text + "：暂无。"
    return (
        scope_text + "共 " + str(len(rows)) + " 名：\n\n"
        + _student_table(rows)
    )


def _lead_status_lines(conn, user):
    role = rbac.effective_role(user)
    sql = "SELECT status,COUNT(*) AS count FROM crm_lead"
    args = []
    if role == "ADVISOR":
        sql += " WHERE owner_employee_id=?"
        args.append(user["id"])
    sql += " GROUP BY status ORDER BY status"
    rows = conn.execute(sql, args).fetchall()
    return [str(row["status"]) + "：" + str(row["count"]) + " 条" for row in rows]


def _handle_customer_status(conn, user, query):
    compact = re.sub(r"\s+", "", query)
    if not any(word in compact for word in (
        "客户状态", "线索状态", "客户情况", "线索情况",
    )):
        return None
    if not rbac.has_capability(user, "crm.read"):
        return (
            "班主任岗位不承担 CRM 客户线索，因此当前账号没有客户状态数据权限。"
            "客户线索由顾问跟进，班主任可在本人班级范围内处理请假、预警和售后。"
        )
    scope = "你负责的" if rbac.effective_role(user) == "ADVISOR" else "全部"
    role = rbac.effective_role(user)
    sql = "SELECT status,COUNT(*) AS count FROM crm_lead"
    args = []
    if role == "ADVISOR":
        sql += " WHERE owner_employee_id=?"
        args.append(user["id"])
    sql += " GROUP BY status ORDER BY status"
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return scope + "客户线索：暂无记录。"
    table = _md_table(
        ("客户阶段", "数量"),
        ((row["status"], row["count"]) for row in rows),
    )
    return scope + "客户状态如下：\n\n" + table


def _pending_leaves(conn, user):
    scope, args = rbac.student_scope(user)
    return conn.execute(
        "SELECT a.id,a.start_time,a.end_time,a.reason,a.create_time,"
        "       u.real_name AS student_name,u.username "
        "FROM student_admin_service a JOIN sys_user u ON u.id=a.student_id "
        "WHERE a.service_type='请假' AND a.status='待审批'" + scope +
        " ORDER BY a.create_time,a.id",
        tuple(args),
    ).fetchall()


def _handle_leave_approval(conn, user, query):
    """处理员工请假审批意图，禁止跨数据范围审批。"""
    text = re.sub(r"\s+", "", query or "")
    if "请假" not in text or not any(
        word in text for word in ("审批", "批准", "通过", "驳回")
    ):
        return None
    if not rbac.has_capability(user, "leave.approve"):
        return {
            "answer": "当前角色没有请假审批权限。",
            "changed": False,
            "intent": "leave_approval_denied",
        }
    role = rbac.effective_role(user)
    scope_label = "全部学生" if role in ("ADMIN", "LEADER") else "本人班级学生"
    rows = _pending_leaves(conn, user)
    wants_bulk = (
        any(word in text for word in ("所有", "全部"))
        and any(word in text for word in ("审批", "通过", "批准"))
        and not any(word in text for word in ("确认通过", "确认批准"))
    )
    if wants_bulk:
        if not rows:
            return {
                "answer": scope_label + "当前没有待审批请假。",
                "changed": False,
                "intent": "leave_bulk_preview",
            }
        leave_table = _md_table(
            ("单号", "学生", "开始日期", "结束日期", "原因"),
            (
                (
                    "#" + str(row["id"]),
                    row["student_name"] or row["username"],
                    row["start_time"],
                    row["end_time"],
                    row["reason"] or "未填写原因",
                )
                for row in rows[:20]
            ),
        )
        if len(rows) > 20:
            leave_table += "\n\n其余 " + str(len(rows) - 20) + " 条请在请假审批页查看。"
        return {
            "answer": (
                "检测到批量审批请求。\n"
                "权限范围：" + scope_label + "\n"
                "待审批数量：" + str(len(rows)) + " 条\n\n"
                + leave_table + "\n\n"
                "为防止误批，本次没有直接执行。"
                "确认全部通过请回复“确认通过本人名下全部待审批请假”；"
                "如需逐条处理，请回复“通过请假 #单号”或“驳回请假 #单号，原因：...”。"
            ),
            "changed": False,
            "intent": "leave_bulk_preview",
        }

    # 明确的批量通过才执行，且上一轮必须先展示待审批清单。
    if (
        any(word in text for word in ("确认通过", "确认批准"))
        and any(word in text for word in ("全部", "所有", "名下"))
    ):
        if not rows:
            return {
                "answer": scope_label + "当前没有待审批请假。",
                "changed": False,
                "intent": "leave_bulk_approve",
            }
        conn.execute("BEGIN IMMEDIATE")
        for row in rows:
            conn.execute(
                "UPDATE student_admin_service SET status='已通过',approver_id=?,"
                " approver_comment=?,approve_time=? WHERE id=? AND status='待审批'",
                (user["id"], "员工助手批量通过", _now(), row["id"]),
            )
        conn.commit()
        for row in rows:
            student_id = _student_id_by_leave(conn, row["id"])
            if student_id:
                notify(
                    student_id,
                    "审批", "请假申请已通过",
                    "员工助手已批量审批你的请假：" + str(row["start_time"])
                    + " 至 " + str(row["end_time"]),
                    "leaves",
                )
        return {
            "answer": "已通过" + scope_label + "的 " + str(len(rows))
                      + " 条待审批请假，并逐条保留审批记录。",
            "changed": bool(rows),
            "intent": "leave_bulk_approve",
        }

    leave_id_match = re.search(r"(?:请假)?#?(\d+)", text)
    action_approve = any(word in text for word in ("通过", "批准"))
    action_reject = "驳回" in text
    if leave_id_match and (action_approve or action_reject):
        leave_id = int(leave_id_match.group(1))
        row = next(
            (item for item in _pending_leaves(conn, user)
             if int(item["id"]) == leave_id),
            None,
        )
        if not row:
            return {
                "answer": "没有找到该待审批请假，或它不在当前角色数据范围内。",
                "changed": False,
                "intent": "leave_single_review",
            }
        status = "已通过" if action_approve and not action_reject else "已驳回"
        reason_match = re.search(r"(?:原因|理由)[:：]?(.+)$", query or "")
        comment = reason_match.group(1).strip() if reason_match else ""
        if status == "已驳回" and not comment:
            return {
                "answer": "驳回请假必须补充原因，例如“驳回请假 #12，原因：材料不完整”。",
                "changed": False,
                "intent": "leave_single_review",
            }
        if not comment:
            comment = "员工助手审批通过"
        conn.execute(
            "UPDATE student_admin_service SET status=?,approver_id=?,"
            " approver_comment=?,approve_time=? WHERE id=? AND status='待审批'",
            (status, user["id"], comment, _now(), leave_id),
        )
        conn.commit()
        student_id = _student_id_by_leave(conn, leave_id)
        if student_id:
            notify(
                student_id, "审批", "请假申请" + status,
                comment, "leaves",
            )
        return {
            "answer": "请假 #" + str(leave_id) + " 已" + status
                      + ("，审批意见：" + comment if comment else "。"),
            "changed": True,
            "intent": "leave_single_review",
        }

    if not rows:
        return {
            "answer": scope_label + "当前没有待审批请假。",
            "changed": False,
            "intent": "leave_approval_preview",
        }
    leave_table = _md_table(
        ("单号", "学生", "开始日期", "结束日期", "原因"),
        (
            (
                "#" + str(row["id"]),
                row["student_name"] or row["username"],
                row["start_time"],
                row["end_time"],
                row["reason"] or "未填写原因",
            )
            for row in rows[:20]
        ),
    )
    return {
        "answer": (
            "当前角色只能处理" + scope_label + "，共有 "
            + str(len(rows)) + " 条待审批请假：\n\n"
            + leave_table + "\n\n"
            "请指定单号操作，例如“通过请假 #12”。"
            "如需全部通过，必须回复“确认通过本人名下全部待审批请假”。"
        ),
        "changed": False,
        "intent": "leave_approval_preview",
    }


def _student_id_by_leave(conn, leave_id):
    row = conn.execute(
        "SELECT student_id FROM student_admin_service WHERE id=?", (leave_id,)
    ).fetchone()
    return int(row["student_id"]) if row else 0


def _handle_performance(conn, user, query):
    if "业绩" not in query and not any(word in query for word in (
        "业务汇总", "运营汇总", "工作汇总",
    )):
        return None
    role = rbac.effective_role(user)
    scope, args = rbac.student_scope(user)
    student_count = conn.execute(
        "SELECT COUNT(*) FROM sys_user u WHERE u.user_type='STUDENT'" + scope,
        tuple(args),
    ).fetchone()[0]
    metrics = []
    if role == "HOMEROOM":
        header = "班主任岗位不计算销售客户业绩，以下是你的带班运营汇总："
    elif role == "ADVISOR":
        header = "你的业务汇总（仅本人数据范围）："
    else:
        header = "全公司业务汇总："
    metrics.append(("学生数", str(student_count) + " 人"))

    if rbac.has_capability(user, "crm.read"):
        lead_lines = _lead_status_lines(conn, user)
        metrics.append(("客户线索", "；".join(lead_lines) if lead_lines else "暂无"))

    if rbac.has_capability(user, "leave.read"):
        pending_leave = conn.execute(
            "SELECT COUNT(*) FROM student_admin_service a "
            "JOIN sys_user u ON u.id=a.student_id "
            "WHERE a.status='待审批'" + scope,
            tuple(args),
        ).fetchone()[0]
        metrics.append(("待审批请假", str(pending_leave) + " 条"))

    if rbac.has_capability(user, "alert.read"):
        open_alerts = conn.execute(
            "SELECT COUNT(*) FROM student_psych_alert a "
            "JOIN sys_user u ON u.id=a.student_id "
            "WHERE a.status='未处理'" + scope,
            tuple(args),
        ).fetchone()[0]
        metrics.append(("未处理心理预警", str(open_alerts) + " 条"))

    if rbac.has_capability(user, "feedback.resolve"):
        open_tickets = conn.execute(
            "SELECT COUNT(*) FROM student_feedback_ticket t "
            "JOIN sys_user u ON u.id=t.student_id "
            "WHERE t.status IN ('待处理','处理中')" + scope,
            tuple(args),
        ).fetchone()[0]
        metrics.append(("未解决售后工单", str(open_tickets) + " 条"))

    if rbac.has_capability(user, "score.read"):
        average = conn.execute(
            "SELECT ROUND(AVG(s.score),1) FROM student_score s "
            "JOIN sys_user u ON u.id=s.student_id "
            "WHERE u.user_type='STUDENT'" + scope,
            tuple(args),
        ).fetchone()[0]
        metrics.append((
            "学生平均成绩",
            str(average) + " 分" if average is not None else "暂无",
        ))
    return header + "\n\n" + _md_table(("指标", "数值"), metrics)


def _handle_named_student(conn, user, query):
    """短姓名查询返回学生档案和已有成绩，作为成绩查询的稳定入口。"""
    compact = re.sub(r"\s+", "", query)
    if not re.fullmatch(r"[\u4e00-\u9fa5·]{2,12}", compact):
        return None
    scope, args = rbac.student_scope(user)
    rows = conn.execute(
        "SELECT u.id,u.username,u.real_name,u.contact_info,u.target_country,"
        "       COALESCE(u.lifecycle_stage,'CONSULT') AS lifecycle_stage "
        "FROM sys_user u WHERE u.user_type='STUDENT'"
        " AND (REPLACE(u.real_name,' ','')=? OR u.username=?)" + scope,
        [compact, compact] + list(args),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        names = [str(row["real_name"] or row["username"]) for row in rows]
        return "匹配到多名学生：" + "、".join(names) + "，请补充账号。"
    row = rows[0]
    stage_key = str(row["lifecycle_stage"] or "CONSULT")
    stage = lifecycle.STAGE_LABEL.get(stage_key, stage_key)
    scores = conn.execute(
        "SELECT course_name,score FROM student_score "
        "WHERE student_id=? ORDER BY id",
        (row["id"],),
    ).fetchall()
    profile_table = _md_table(
        ("字段", "内容"),
        (
            ("学生姓名", row["real_name"] or row["username"]),
            ("账号", row["username"]),
            ("学籍阶段", stage),
            ("意向国家", row["target_country"] or "未填写"),
        ),
    )
    result = "学生档案：\n\n" + profile_table
    if scores:
        score_table = _md_table(
            ("课程名称", "成绩"),
            ((score["course_name"], str(score["score"]) + " 分") for score in scores),
        )
        result += "\n\n现有成绩：\n\n" + score_table
    else:
        result += "\n\n现有成绩：暂无"
    result += "\n\n如需录入或修改成绩，可回复“添加成绩”后继续说明科目和分数。"
    return result


def handle_employee_chat(user, query, history=""):
    """处理员工结构化问题；非命中返回 None，继续走原有业务或 Dify。"""
    del history
    if not user or user.get("user_type") not in ("EMPLOYEE", "TEACHER", "ADMIN"):
        return None
    text = str(query or "").strip()
    if not text:
        return {
            "answer": "请直接输入要查询的业务问题。",
            "changed": False,
        }
    compact = re.sub(r"[\s，。,.！!？?、：:；;]", "", text)
    if not compact:
        return {
            "answer": (
                "刚才这句话没有具体问题。你可以继续问：我的班级、所有学生、"
                "查成绩、客户状态、业务汇总。"
            ),
            "changed": False,
        }
    if any(word in text for word in (
        "你能查询哪些", "你能做什么", "你会什么", "有什么功能", "怎么用",
    )):
        return {"answer": _capability_answer(user), "changed": False, "intent": "capabilities"}

    conn = get_db()
    try:
        answer = _handle_staff_directory(conn, text)
        if answer is not None:
            return {"answer": answer, "changed": False, "intent": "staff_directory"}
        answer = _handle_class_scope(conn, user, text)
        if answer is not None:
            return {"answer": answer, "changed": False, "intent": "class_scope"}
        leave_approval = _handle_leave_approval(conn, user, text)
        if leave_approval is not None:
            return leave_approval
        answer = _handle_all_students(conn, user, text)
        if answer is not None:
            return {"answer": answer, "changed": False, "intent": "student_list"}
        answer = _handle_customer_status(conn, user, text)
        if answer is not None:
            return {"answer": answer, "changed": False, "intent": "customer_status"}
        answer = _handle_performance(conn, user, text)
        if answer is not None:
            return {"answer": answer, "changed": False, "intent": "performance"}
        answer = _handle_named_student(conn, user, text)
        if answer is not None:
            return {"answer": answer, "changed": False, "intent": "student_profile"}
    finally:
        conn.close()
    return None
