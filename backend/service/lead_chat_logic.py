"""员工客户跟进查询的确定性路由。

员工说“查一下许同学的跟进记录”时，先用统一搜索分别查学生档案和 CRM
客户线索；只有两边都没有结果时才调用 NL2SQL 兜底。这样既符合 7.3 的
处理步骤，也能明确区分“学生档案存在”和“客户线索尚未建立”。
"""
from __future__ import annotations

from service import employee_logic, lifecycle


def _is_customer_query(query):
    text = (query or "").strip()
    if not any(word in text for word in ("客户", "同学", "跟进记录", "客户档案", "客户资料")):
        return False
    return any(word in text for word in ("查", "相关", "资料", "档案", "记录", "详情"))


def _lead_lines(leads):
    lines = ["客户档案："]
    for index, lead in enumerate(leads, 1):
        lines.extend([
            str(index) + ". 姓名：" + str(lead.get("customer_name") or "未填写"),
            "联系方式：" + str(lead.get("contact_info") or "未填写"),
            "背景：" + str(lead.get("background_info") or "未填写"),
            "跟进历史：" + str(lead.get("follow_up_history") or "暂无"),
            "状态：" + str(lead.get("status") or "未知"),
        ])
    return lines


def _student_only_lines(students):
    lines = [
        "系统中存在学生档案，但尚未建立对应的 CRM 客户线索。",
        "客户档案：",
    ]
    for index, student in enumerate(students, 1):
        stage_key = str(student.get("lifecycle_stage") or "CONSULT")
        stage = lifecycle.STAGE_LABEL.get(stage_key, stage_key)
        background = (
            "意向国家：" + str(student.get("target_country") or "未填写")
            + "；学籍阶段：" + stage
        )
        lines.extend([
            str(index) + ". 姓名：" + str(student.get("real_name") or student.get("username") or "未命名"),
            "联系方式：" + str(student.get("contact_info") or "未填写"),
            "背景：" + background,
            "跟进历史：暂无 CRM 跟进记录",
            "状态：未建立客户线索",
        ])
    return lines


def handle_customer_query(user, query):
    """处理员工客户/跟进查询；非客户查询返回 None。"""
    if not user or user.get("user_type") == "STUDENT" or not _is_customer_query(query):
        return None
    result = employee_logic.search_students_and_leads(query, user)
    name = str(result.get("query_name") or "").strip()
    if not name:
        return {
            "answer": "请明确要查询的客户姓名或同学姓名。",
            "changed": False,
        }

    students = result.get("students") or []
    leads = result.get("leads") or []
    if leads and students:
        lines = _lead_lines(leads)
        lines.append("另外匹配到学生档案：")
        for student in students:
            lines.append(
                str(student.get("real_name") or student.get("username"))
                + "｜" + str(student.get("contact_info") or "未填写")
                + "｜" + str(student.get("target_country") or "未填写")
            )
        return {"answer": "\n".join(lines), "changed": False}
    if leads:
        return {"answer": "\n".join(_lead_lines(leads)), "changed": False}
    if students:
        return {"answer": "\n".join(_student_only_lines(students)), "changed": False}

    # 统一搜索未命中时才执行 NL2SQL 兜底，符合需求 7.3.3 的顺序，
    # 同时不会绕过原有角色和数据范围限制。
    fallback = employee_logic.handle_nl2sql(query, user)
    rows = fallback.get("data") or []
    if fallback.get("denied"):
        return {"answer": str(fallback.get("msg") or "无权查询客户"), "changed": False}
    if rows:
        return {"answer": "\n".join(_lead_lines(rows)), "changed": False}
    return {
        "answer": "没有找到名为“" + name + "”的学生档案或 CRM 客户档案。",
        "changed": False,
    }
