"""通用业务动作编排：准备、消歧、确认和最终执行。

写操作统一采用两段式：
- prepare_business_action：只保存草稿、补字段、列候选、生成确认预览；
- execute_business_action：消费一次性确认令牌后才写正式业务表。

查询动作允许 prepare 直接返回 STATE_READY，由 Dify 调用 execute 读取数据。
"""
from __future__ import annotations

import hmac
import json
import re
import secrets
from datetime import datetime, timedelta

from db import get_db
from service import (
    crm_logic, employee_logic, event_chat_logic, knowledge_logic, lifecycle,
    rbac, score_chat_logic, student_logic,
)
from service.notify_logic import notify


CONFIRM_ACTIONS = {
    "event_register", "score_upsert", "score_delete",
    "feedback_create", "feedback_cancel", "lead_create",
    "report_create", "lead_follow_up", "feedback_resolve",
}
ACTION_LABELS = {
    "event_register": "活动报名",
    "score_query": "成绩查询",
    "score_upsert": "成绩录入或修改",
    "score_delete": "删除成绩",
    "feedback_create": "提交售后",
    "feedback_cancel": "撤销售后工单",
    "lead_create": "录入客户线索",
    "report_create": "提交员工日报",
    "lead_follow_up": "客户跟进",
    "feedback_resolve": "解决售后工单",
    "application_create": "提交就读申请",
    "application_update": "修改就读申请",
    "application_cancel": "撤销就读申请",
    "application_advance": "推进申请阶段",
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _coerce_int(value):
    """把模型可能返回的 #176、ID 176 等编号统一成整数。"""
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 0


def _draft_key(user, visitor_key=""):
    if user:
        return str(user.get("user_type") or "USER") + ":" + str(user["id"])
    return "VISITOR:" + str(visitor_key or "anonymous")[:120]


def _load(key):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM business_action_draft WHERE draft_key=?", (key,)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        for source, target in (
            ("slots_json", "slots"),
            ("candidates_json", "candidates"),
            ("missing_json", "missing"),
        ):
            try:
                value = json.loads(result.get(source) or "{}")
            except (TypeError, ValueError):
                value = {} if target == "slots" else []
            result[target] = value
        return result
    finally:
        conn.close()


def _save(key, user, action_type, state, slots, candidates=None,
          missing=None, confirmation_token="", confirmation_expires_at=""):
    conn = get_db()
    try:
        # 新草稿开始后，上一次执行结果不再具有展示价值。
        conn.execute("DELETE FROM business_action_result WHERE draft_key=?", (key,))
        conn.execute(
            "INSERT INTO business_action_draft "
            "(draft_key,actor_id,actor_type,action_type,state,slots_json,"
            "candidates_json,missing_json,confirmation_token,"
            "confirmation_expires_at,update_time) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(draft_key) DO UPDATE SET "
            "actor_id=excluded.actor_id,actor_type=excluded.actor_type,"
            "action_type=excluded.action_type,state=excluded.state,"
            "slots_json=excluded.slots_json,candidates_json=excluded.candidates_json,"
            "missing_json=excluded.missing_json,"
            "confirmation_token=excluded.confirmation_token,"
            "confirmation_expires_at=excluded.confirmation_expires_at,"
            "update_time=excluded.update_time",
            (
                key,
                int(user["id"]) if user else 0,
                str(user.get("user_type") or "VISITOR") if user else "VISITOR",
                action_type, state,
                json.dumps(slots or {}, ensure_ascii=False),
                json.dumps(candidates or [], ensure_ascii=False),
                json.dumps(missing or [], ensure_ascii=False),
                confirmation_token, confirmation_expires_at, _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _clear(key):
    conn = get_db()
    try:
        conn.execute("DELETE FROM business_action_draft WHERE draft_key=?", (key,))
        conn.commit()
    finally:
        conn.close()


def _save_result(key, action_type, result):
    """保存最近一次动作结果，供客服代理在下一层回传 changed 等结构化字段。"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO business_action_result "
            "(draft_key,action_type,result_json,update_time) VALUES (?,?,?,?) "
            "ON CONFLICT(draft_key) DO UPDATE SET "
            "action_type=excluded.action_type,result_json=excluded.result_json,"
            "update_time=excluded.update_time",
            (key, action_type, json.dumps(result, ensure_ascii=False), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def pop_last_result(user, visitor_key=""):
    """读取并删除当前会话最近一次结果，避免结果被后续闲聊重复消费。"""
    key = _draft_key(user, visitor_key)
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT result_json FROM business_action_result WHERE draft_key=?",
            (key,),
        ).fetchone()
        if not row:
            return {}
        conn.execute("DELETE FROM business_action_result WHERE draft_key=?", (key,))
        conn.commit()
        try:
            value = json.loads(row["result_json"] or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            return {}
    finally:
        conn.close()


def _result(status, message="", missing=None, candidates=None, slots=None,
            preview="", confirmation_token="", requires_confirmation=False,
            **extra):
    result = {
        "status_token": status,
        "message": message,
        "missing_fields": missing or [],
        "candidates": candidates or [],
        "slots": slots or {},
        "preview": preview,
        "confirmation_token": confirmation_token,
        "requires_confirmation": requires_confirmation,
    }
    result.update(extra)
    return result


def _action_from_text(query):
    text = str(query or "")
    if any(word in text for word in ("撤销申请", "取消申请", "撤回申请")):
        return "application_cancel"
    if any(word in text for word in ("修改申请", "更新申请", "改申请")):
        return "application_update"
    if any(word in text for word in ("申请就读", "提交就读申请", "提交申请", "报名就读")):
        return "application_create"
    if any(word in text for word in (
        "推进申请", "申请推进", "推进到下一阶段", "申请下一阶段",
    )):
        return "application_advance"
    if any(word in text for word in ("解决售后", "处理售后", "售后解决", "处理工单")):
        return "feedback_resolve"
    if any(word in text for word in ("写日报", "提交日报", "记录日报", "补日报")):
        return "report_create"
    if any(word in text for word in ("跟进客户", "客户跟进", "追加跟进", "更新客户状态")):
        return "lead_follow_up"
    if any(word in text for word in ("活动", "讲座", "宣讲", "展会", "见面会", "联展")) and "报名" in text:
        return "event_register"
    if "成绩" in text or "分数" in text:
        if "删除" in text:
            return "score_delete"
        if any(word in text for word in ("录入", "添加", "修改", "更新", "改成", "改为")):
            return "score_upsert"
        return "score_query"
    if any(word in text for word in ("售后", "反馈", "投诉")):
        if any(word in text for word in ("删除", "撤回", "取消", "撤销")):
            return "feedback_cancel"
        return "feedback_create"
    if any(word in text for word in ("客户线索", "录入客户", "新增客户", "添加客户")):
        return "lead_create"
    return ""


def has_active_action(user, visitor_key=""):
    draft = _load(_draft_key(user, visitor_key))
    return bool(draft and draft.get("state") in ("COLLECTING", "CONFIRMING", "READY"))


def active_action_type(user, visitor_key=""):
    draft = _load(_draft_key(user, visitor_key))
    if not draft or draft.get("state") not in ("COLLECTING", "CONFIRMING", "READY"):
        return ""
    return str(draft.get("action_type") or "")


def _event_candidates(user, query):
    scopes = event_chat_logic._query_scope(query, user)
    if scopes is None:
        return [], "当前身份不能查看所请求的活动范围。"
    events = event_chat_logic.upcoming_events(scopes=scopes)
    candidates = [
        {
            "index": index,
            "event_id": int(row["id"]),
            "label": str(row.get("event_name") or "活动")
                     + "｜" + str(row.get("start_time") or "时间待定")
                     + "｜" + str(row.get("location") or "地点待定"),
        }
        for index, row in enumerate(events[:5], 1)
    ]
    return candidates, ""


def _prepare_event(user, query, slots, candidates):
    event_candidates, scope_error = _event_candidates(user, query)
    if scope_error:
        return _result("STATE_ERROR", scope_error)
    event_id = int(slots.get("event_id") or 0)
    event_name = str(slots.get("event_name") or "").strip()
    selection = int(slots.get("selection") or 0)
    if not event_id and selection and 1 <= selection <= len(event_candidates):
        event_id = int(event_candidates[selection - 1]["event_id"])
    if not event_id and event_name:
        name_matches = [
            item for item in event_candidates
            if event_name.replace(" ", "") in str(item["label"]).replace(" ", "")
        ]
        if len(name_matches) == 1:
            event_id = int(name_matches[0]["event_id"])
    if not event_id:
        return _result(
            "STATE_AMBIGUOUS", "请选择要报名的活动。",
            missing=["event_id"], candidates=event_candidates,
            slots=slots,
        )
    conn = get_db()
    try:
        event = conn.execute(
            "SELECT * FROM event_lecture WHERE id=?", (event_id,)
        ).fetchone()
        if not event:
            return _result("STATE_ERROR", "活动不存在，请重新查询。",
                           candidates=event_candidates)
        profile = None
        if user and user.get("user_type") == "STUDENT":
            profile = conn.execute(
                "SELECT real_name,contact_info FROM sys_user WHERE id=?",
                (user["id"],),
            ).fetchone()
    finally:
        conn.close()
    name = str(slots.get("participant_name") or "").strip()
    contact = str(slots.get("contact_info") or "").strip()
    if profile:
        name = name or str(profile["real_name"] or "")
        contact = contact or str(profile["contact_info"] or "")
    missing = []
    if not name:
        missing.append("participant_name")
    if not contact:
        missing.append("contact_info")
    prepared = {
        "event_id": event_id,
        "event_name": str(event["event_name"] or "活动"),
        "event_time": str(event["start_time"] or "时间待定"),
        "location": str(event["location"] or "地点待定"),
        "participant_name": name,
        "contact_info": contact,
    }
    if missing:
        return _result(
            "STATE_MISSING", "报名信息不完整。",
            missing=missing, slots=prepared,
        )
    preview = (
        "活动：" + prepared["event_name"] + "\n"
        "时间：" + prepared["event_time"] + "\n"
        "地点：" + prepared["location"] + "\n"
        "报名人：" + name + "\n联系方式：" + contact
    )
    return _result(
        "STATE_CONFIRM", "活动报名信息已齐全，请确认。",
        slots=prepared, preview=preview, requires_confirmation=True,
    )


def _prepare_score(user, query, action_type, slots, history=""):
    if not user or not rbac.has_capability(user, "score.read"):
        return _result("STATE_ERROR", "当前角色不能操作成绩。")
    if action_type != "score_query" and not rbac.has_capability(user, "score.manage"):
        return _result("STATE_ERROR", "当前角色无权录入、修改或删除成绩。")
    conn = get_db()
    try:
        students = score_chat_logic._affected_students(conn, user)
        student = None
        student_id = int(slots.get("student_id") or 0)
        if student_id:
            student = next((row for row in students if int(row["id"]) == student_id), None)
        student_name = str(slots.get("student_name") or "").strip()
        if not student and student_name:
            student, _ = score_chat_logic._pick_student(students, student_name)
        # Dify 的字段抽取器可能把历史消息里的姓名带入本轮 slots。纯查询
        # 只能使用当前输入中明确出现的学生，或用户在候选中明确选择序号；
        # 不能因为上一轮聊过林子涵，就把“查成绩”解释成查林子涵。
        if action_type == "score_query" and not slots.get("selection"):
            current_student, _ = score_chat_logic._pick_student(students, query)
            if not current_student:
                slots = dict(slots)
                slots.pop("student_id", None)
                slots.pop("student_name", None)
                student = None
                student_id = 0
                student_name = ""
        # 查询必须保持为独立、可核验的检索动作；只有录入/修改/删除这类
        # 写操作才继承上一位学生，避免用户只说“查成绩”时被上下文误导。
        if not student and action_type != "score_query" and history:
            for line in reversed(str(history).splitlines()):
                candidate, _ = score_chat_logic._pick_student(students, line)
                if candidate:
                    student = candidate
                    break
        selection = int(slots.get("selection") or 0)
        if not student and selection and 1 <= selection <= len(students):
            student = students[selection - 1]
        if not student:
            candidates = [
                {
                    "index": index,
                    "student_id": int(row["id"]),
                    "label": str(row["real_name"] or row["username"])
                             + "（" + str(row["username"]) + "）",
                }
                for index, row in enumerate(students[:12], 1)
            ]
            return _result(
                "STATE_AMBIGUOUS", "请选择要操作的学生。",
                missing=["student_id"], candidates=candidates, slots=slots,
            )
        scores = score_chat_logic._query_scores(conn, student["id"])
    finally:
        conn.close()
    student_name = str(student["real_name"] or student["username"])
    prepared = dict(slots)
    prepared["student_id"] = int(student["id"])
    prepared["student_name"] = student_name
    if action_type == "score_query":
        return _result(
            "STATE_READY", "开始查询成绩。", slots=prepared,
        )
    score_id = int(slots.get("score_id") or 0)
    course = str(slots.get("course_name") or "").strip()
    if not score_id and course:
        target = next(
            (row for row in scores if course.lower() in str(row["course_name"]).lower()),
            None,
        )
        if target:
            score_id = int(target["id"])
    if not score_id and not course:
        # 新增成绩时，课程和分数是两个独立必填项。这里一次返回全部缺口，
        # 避免用户先选课程后又被迫多回答一轮；已有记录候选仍一并返回。
        missing = ["course_name"]
        message = "请选择课程或成绩记录。"
        if action_type == "score_upsert" and slots.get("score_value") in (None, ""):
            missing.append("score_value")
            message = "请选择课程并补充具体分数（0-100）。"
        return _result(
            "STATE_MISSING", message,
            missing=missing,
            candidates=[
                {
                    "index": index,
                    "score_id": int(row["id"]),
                    "label": str(row["course_name"]) + "：" + str(row["score"]) + " 分",
                }
                for index, row in enumerate(scores, 1)
            ],
            slots=prepared,
        )
    prepared["course_name"] = course or next(
        (str(row["course_name"]) for row in scores if int(row["id"]) == score_id), ""
    )
    prepared["score_id"] = score_id
    if action_type == "score_upsert":
        score_value = slots.get("score_value")
        try:
            score_value = float(score_value)
        except (TypeError, ValueError):
            score_value = None
        if score_value is None or not 0 <= score_value <= 100:
            return _result(
                "STATE_MISSING", "请提供 0-100 的具体分数。",
                missing=["score_value"], slots=prepared,
            )
        prepared["score_value"] = score_value
        action_label = "修改" if score_id else "录入"
        preview = (
            action_label + "成绩\n学生：" + student_name
            + "\n课程：" + str(prepared.get("course_name") or "待确定")
            + "\n新分数：" + str(score_value) + " 分"
        )
    else:
        if not score_id:
            return _result("STATE_MISSING", "请指定要删除的成绩。",
                           missing=["score_id"], slots=prepared)
        preview = (
            "删除成绩\n学生：" + student_name
            + "\n课程：" + str(prepared.get("course_name") or "待确定")
            + "\n成绩记录 ID：" + str(score_id)
        )
    return _result(
        "STATE_CONFIRM", "成绩操作已准备好，请确认。",
        slots=prepared, preview=preview, requires_confirmation=True,
    )


def _prepare_feedback(user, action_type, slots):
    if not user or user.get("user_type") != "STUDENT":
        return _result("STATE_ERROR", "只有学生账号可以提交或撤销售后。")
    if action_type == "feedback_create":
        content = str(slots.get("feedback_content") or "").strip()
        if not content:
            return _result(
                "STATE_MISSING", "请说明售后问题的具体内容。",
                missing=["feedback_content"], slots=slots,
            )
        prepared = {"feedback_content": content,
                    "feedback_detail": str(slots.get("feedback_detail") or "")}
        return _result(
            "STATE_CONFIRM", "售后内容已记录，请确认提交。",
            slots=prepared, preview="提交售后工单\n问题：" + content,
            requires_confirmation=True,
        )
    rows = student_logic.get_my_feedbacks(user["id"])
    pending = [row for row in rows if row.get("status") == "待处理"]
    ticket_id = int(slots.get("ticket_id") or 0)
    selection = int(slots.get("selection") or 0)
    if not ticket_id and selection and 1 <= selection <= len(pending):
        ticket_id = int(pending[selection - 1]["id"])
    if not ticket_id:
        return _result(
            "STATE_AMBIGUOUS", "请选择要撤回的售后工单。",
            missing=["ticket_id"], slots=slots,
            candidates=[
                {"index": index, "ticket_id": int(row["id"]),
                 "label": "工单 #" + str(row["id"]) + "｜" + str(row.get("content") or "")}
                for index, row in enumerate(pending[:10], 1)
            ],
        )
    target = next((row for row in pending if int(row["id"]) == ticket_id), None)
    if not target:
        return _result("STATE_ERROR", "没有找到待处理的该工单。")
    prepared = {"ticket_id": ticket_id,
                "ticket_content": str(target.get("content") or "")}
    return _result(
        "STATE_CONFIRM", "请确认撤回该售后工单。",
        slots=prepared,
        preview="撤回售后工单\n单号：#" + str(ticket_id)
                + "\n内容：" + prepared["ticket_content"],
        requires_confirmation=True,
    )


def _prepare_lead(user, slots):
    if not user or user.get("user_type") == "STUDENT" or not rbac.has_capability(user, "crm.manage"):
        return _result("STATE_ERROR", "当前角色无权录入客户线索。")
    prepared = {
        "customer_name": str(slots.get("customer_name") or "").strip(),
        "contact_info": crm_logic.normalize_contact(str(slots.get("contact_info") or "")),
        "target_country": str(slots.get("target_country") or "").strip(),
        "background_info": str(slots.get("background_info") or "").strip(),
    }
    missing = [field for field in ("customer_name", "contact_info") if not prepared[field]]
    if missing:
        return _result("STATE_MISSING", "客户线索信息不完整。",
                       missing=missing, slots=prepared)
    return _result(
        "STATE_CONFIRM", "客户线索信息已齐全，请确认录入。",
        slots=prepared,
        preview="新增客户线索\n姓名：" + prepared["customer_name"]
                + "\n联系方式：" + prepared["contact_info"]
                + "\n意向国家：" + (prepared["target_country"] or "未填写"),
        requires_confirmation=True,
    )


def _prepare_report(user, slots):
    """员工日报只生成草稿，用户确认后才写正式日报表。"""
    if not user or not rbac.has_capability(user, "report.self"):
        return _result("STATE_ERROR", "当前角色不能提交员工日报。")
    content = str(slots.get("report_content") or slots.get("content") or "").strip()
    report_date = str(slots.get("report_date") or "").strip()
    if report_date in ("今天", "今日"):
        report_date = datetime.now().strftime("%Y-%m-%d")
    if report_date:
        try:
            datetime.strptime(report_date, "%Y-%m-%d")
        except ValueError:
            return _result("STATE_ERROR", "日报日期格式应为 YYYY-MM-DD。")
    if not content:
        return _result(
            "STATE_MISSING", "请说明今天要写入日报的具体工作内容。",
            missing=["report_content"],
            slots={"report_date": report_date},
        )
    prepared = {"report_content": content, "report_date": report_date}
    return _result(
        "STATE_CONFIRM", "日报内容已整理，请确认提交。",
        slots=prepared,
        preview="提交员工日报\n日期：" + (report_date or "今天")
                + "\n内容：" + content,
        requires_confirmation=True,
    )


def _lead_scope(user):
    """CRM 查询范围与接口保持一致：顾问看本人，领导和管理员看全部。"""
    role = rbac.effective_role(user)
    if role in ("ADMIN", "LEADER"):
        return "", []
    return " AND l.owner_employee_id=?", [int(user["id"])]


def _prepare_lead_follow_up(user, slots, stored_candidates=None):
    """为客户跟进补齐目标客户、合法阶段和本次跟进记录。"""
    if not user or not rbac.has_capability(user, "crm.manage"):
        return _result("STATE_ERROR", "当前角色无权跟进客户线索。")
    conn = get_db()
    try:
        scope, args = _lead_scope(user)
        rows = conn.execute(
            "SELECT l.*, u.real_name AS owner_name FROM crm_lead l "
            "LEFT JOIN sys_user u ON u.id=l.owner_employee_id "
            "WHERE 1=1" + scope + " ORDER BY l.update_time DESC, l.id DESC",
            tuple(args),
        ).fetchall()
    finally:
        conn.close()

    candidates = [
        {
            "index": index,
            "lead_id": int(row["id"]),
            "label": str(row["customer_name"] or "未命名客户")
                     + "｜" + str(row["contact_info"] or "无联系方式")
                     + "｜" + str(row["status"] or "新增意向"),
        }
        for index, row in enumerate(rows[:12], 1)
    ]
    lead_id = int(slots.get("lead_id") or 0)
    selection = int(slots.get("selection") or 0)
    if not lead_id and selection and 1 <= selection <= len(candidates):
        lead_id = int(candidates[selection - 1]["lead_id"])
    customer_name = str(slots.get("customer_name") or "").strip()
    target = next((row for row in rows if int(row["id"]) == lead_id), None)
    if not target and customer_name:
        matches = [
            row for row in rows
            if customer_name.replace(" ", "") in
            (str(row["customer_name"] or "") + str(row["contact_info"] or "")).replace(" ", "")
        ]
        if len(matches) == 1:
            target = matches[0]
    if not target:
        return _result(
            "STATE_AMBIGUOUS", "请选择要跟进的客户。",
            missing=["lead_id"], candidates=candidates or (stored_candidates or []),
            slots=slots,
        )

    valid_statuses = ("新增意向", "跟进中", "已签约", "已流失")
    current_status = str(target["status"] or "新增意向")
    status = str(slots.get("lead_status") or slots.get("new_status") or current_status).strip()
    if status not in valid_statuses:
        return _result(
            "STATE_ERROR",
            "客户阶段只能是：新增意向、跟进中、已签约、已流失。",
            slots=slots,
        )
    note = str(slots.get("follow_up_note") or slots.get("note") or "").strip()
    if status == current_status and not note:
        return _result(
            "STATE_MISSING", "请补充本次跟进内容，或说明要变更的客户阶段。",
            missing=["follow_up_note"],
            candidates=candidates or (stored_candidates or []),
            slots=slots,
        )
    prepared = {
        "lead_id": int(target["id"]),
        "customer_name": str(target["customer_name"] or ""),
        "contact_info": str(target["contact_info"] or ""),
        "lead_status": status,
        "follow_up_note": note,
    }
    return _result(
        "STATE_CONFIRM", "客户跟进信息已准备好，请确认保存。",
        slots=prepared,
        preview="更新客户跟进\n客户：" + prepared["customer_name"]
                + "\n当前阶段：" + current_status + " → " + status
                + "\n跟进记录：" + (note or "仅更新阶段"),
        requires_confirmation=True,
    )


def _prepare_feedback_resolve(user, slots):
    """员工处理售后前必须选定具体工单，并明确解决方案。"""
    if not user or not rbac.has_capability(user, "feedback.resolve"):
        return _result("STATE_ERROR", "当前角色无权处理售后工单。")
    conn = get_db()
    try:
        scope, args = rbac.student_scope(user)
        rows = conn.execute(
            "SELECT t.*, u.real_name AS student_name, u.username AS student_username "
            "FROM student_feedback_ticket t JOIN sys_user u ON u.id=t.student_id "
            "WHERE t.status IN ('待处理','处理中')" + scope +
            " ORDER BY t.create_time DESC, t.id DESC",
            tuple(args),
        ).fetchall()
    finally:
        conn.close()
    candidates = [
        {
            "index": index,
            "ticket_id": int(row["id"]),
            "label": "工单 #" + str(row["id"]) + "｜"
                     + str(row["student_name"] or row["student_username"])
                     + "｜" + str(row["content"] or ""),
        }
        for index, row in enumerate(rows[:12], 1)
    ]
    ticket_id = int(slots.get("ticket_id") or 0)
    selection = int(slots.get("selection") or 0)
    if not ticket_id and selection and 1 <= selection <= len(candidates):
        ticket_id = int(candidates[selection - 1]["ticket_id"])
    keyword = str(slots.get("ticket_content") or "").strip()
    target = next((row for row in rows if int(row["id"]) == ticket_id), None)
    if not target and keyword:
        matches = [
            row for row in rows
            if keyword.replace(" ", "") in
            (str(row["content"] or "") + str(row["detail"] or "")).replace(" ", "")
        ]
        if len(matches) == 1:
            target = matches[0]
    if not target:
        return _result(
            "STATE_AMBIGUOUS", "请选择要解决的售后工单。",
            missing=["ticket_id"], candidates=candidates, slots=slots,
        )
    solution = str(slots.get("solution") or "").strip()
    if not solution:
        return _result(
            "STATE_MISSING", "请补充这次售后的处理方案。",
            missing=["solution"], candidates=candidates, slots=slots,
        )
    prepared = {
        "ticket_id": int(target["id"]),
        "student_name": str(target["student_name"] or target["student_username"]),
        "ticket_content": str(target["content"] or ""),
        "solution": solution,
    }
    return _result(
        "STATE_CONFIRM", "售后处理方案已生成，请确认后解决工单并通知学生。",
        slots=prepared,
        preview="解决售后工单\n学生：" + prepared["student_name"]
                + "\n工单：#" + str(prepared["ticket_id"])
                + "\n问题：" + prepared["ticket_content"]
                + "\n处理方案：" + solution,
        requires_confirmation=True,
    )


def _application_catalog():
    """从 Dify 实时读取国家、模式和项目，不在后端保存方案主数据。"""
    return knowledge_logic.program_catalog()["programs"]


def _catalog_candidates(programs, limit=12):
    return [
        {
            "index": index,
            "label": str(item.get("country") or "未填写") + "｜"
                     + str(item.get("category") or "未分类") + "｜"
                     + str(item.get("project_name") or "未命名项目"),
        }
        for index, item in enumerate(programs[:limit], 1)
    ]


def _validate_application_choice(country, mode, program):
    programs = _application_catalog()
    target = next((
        item for item in programs
        if str(item.get("country") or "") == country
        and str(item.get("category") or "") == mode
        and str(item.get("project_name") or "") == program
    ), None)
    return target, programs


def _prepare_application(user, action_type, slots):
    """学生对就读申请的新增、修改、撤销，先补字段并等待明确确认。"""
    if not user or user.get("user_type") != "STUDENT":
        return _result("STATE_ERROR", "只有学生账号可以办理本人就读申请。")
    if not rbac.has_capability(user, "application.self"):
        return _result("STATE_ERROR", "当前账号没有就读申请权限。")

    conn = get_db()
    try:
        pending = conn.execute(
            "SELECT * FROM application_progress WHERE student_id=? "
            "AND initiator_type='STUDENT' AND status='待受理' "
            "ORDER BY id DESC",
            (user["id"],),
        ).fetchall()
    finally:
        conn.close()
    selection = int(slots.get("selection") or 0)
    application_id = int(slots.get("application_id") or 0)
    candidates = [
        {
            "index": index,
            "application_id": int(row["id"]),
            "label": "申请 #" + str(row["id"]) + "｜"
                     + str(row["country"] or "国家待定") + "｜"
                     + str(row["program"] or "项目待定"),
        }
        for index, row in enumerate(pending, 1)
    ]

    if action_type == "application_create":
        stage = lifecycle.get_stage(user["id"])
        if stage in ("ENROLLED", "ALUMNI"):
            return _result("STATE_ERROR", "你已入学或毕业，无需重复提交就读申请。")
        country = str(
            slots.get("country") or slots.get("target_country") or ""
        ).strip()
        mode = str(slots.get("application_mode") or slots.get("mode") or "").strip()
        program = str(slots.get("program") or slots.get("project_name") or "").strip()
        missing = [
            field for field, value in (
                ("country", country),
                ("application_mode", mode),
                ("program", program),
            ) if not value
        ]
        if missing:
            try:
                programs = _application_catalog()
            except Exception as exc:
                print("读取 Dify 申请方案失败:", repr(exc))
                return _result(
                    "STATE_ERROR", "方案目录暂时不可用，请稍后重试；系统未提交申请。"
                )
            return _result(
                "STATE_MISSING",
                "请选择就读国家、学习模式和具体项目。",
                missing=missing, candidates=_catalog_candidates(programs),
                slots=slots,
            )
        try:
            target, _ = _validate_application_choice(country, mode, program)
        except Exception as exc:
            print("校验 Dify 申请方案失败:", repr(exc))
            return _result("STATE_ERROR", "方案目录暂时不可用，系统未提交申请。")
        if not target:
            return _result(
                "STATE_ERROR", "所选国家、模式和项目不属于公司当前已发布的项目范围。"
            )
        prepared = {
            "country": country,
            "application_mode": mode,
            "program": program,
            "university": str(slots.get("university") or "").strip(),
            "remark": str(slots.get("remark") or "").strip(),
        }
        return _result(
            "STATE_CONFIRM", "就读申请信息已整理，请确认提交。",
            slots=prepared,
            preview="提交就读申请\n国家：" + country
                    + "\n模式：" + mode + "\n项目：" + program
                    + "\n院校：" + (prepared["university"] or "待定")
                    + "\n备注：" + (prepared["remark"] or "无"),
            requires_confirmation=True,
        )

    if not application_id and selection and 1 <= selection <= len(candidates):
        application_id = int(candidates[selection - 1]["application_id"])
    target = next((row for row in pending if int(row["id"]) == application_id), None)
    if not target and len(pending) == 1:
        target = pending[0]
    if not target:
        if not candidates:
            return _result("STATE_ERROR", "当前没有可修改或撤销的待受理申请。")
        return _result(
            "STATE_AMBIGUOUS", "请选择要办理的待受理申请。",
            missing=["application_id"], candidates=candidates, slots=slots,
        )
    prepared = {"application_id": int(target["id"])}
    if action_type == "application_cancel":
        prepared.update({
            "application_country": str(target["country"] or ""),
            "application_program": str(target["program"] or ""),
        })
        return _result(
            "STATE_CONFIRM", "请确认撤销该就读申请。",
            slots=prepared,
            preview="撤销就读申请\n申请 #" + str(target["id"])
                    + "\n国家：" + str(target["country"] or "待定")
                    + "\n项目：" + str(target["program"] or "待定"),
            requires_confirmation=True,
        )

    provided = {
        "country": str(
            slots.get("country") or slots.get("target_country") or ""
        ).strip(),
        "application_mode": str(
            slots.get("application_mode") or slots.get("mode") or ""
        ).strip(),
        "program": str(slots.get("program") or slots.get("project_name") or "").strip(),
        "university": str(slots.get("university") or "").strip(),
        "remark": str(slots.get("remark") or "").strip(),
    }
    if not any(provided.values()):
        return _result(
            "STATE_MISSING", "请说明要修改的国家、模式、项目、院校或备注。",
            missing=["application_change"],
            candidates=candidates, slots=prepared,
        )
    merged = {
        "application_id": int(target["id"]),
        "country": provided["country"] or str(target["country"] or ""),
        "application_mode": provided["application_mode"]
                            or str(target["application_mode"] or ""),
        "program": provided["program"] or str(target["program"] or ""),
        "university": provided["university"] or str(target["university"] or ""),
        "remark": provided["remark"] or str(target["student_remark"] or ""),
    }
    try:
        valid, _ = _validate_application_choice(
            merged["country"], merged["application_mode"], merged["program"]
        )
    except Exception as exc:
        print("校验 Dify 修改方案失败:", repr(exc))
        return _result("STATE_ERROR", "方案目录暂时不可用，系统未修改申请。")
    if not valid:
        return _result(
            "STATE_ERROR", "修改后的国家、模式和项目不属于公司当前已发布的项目范围。"
        )
    return _result(
        "STATE_CONFIRM", "申请修改内容已整理，请确认保存。",
        slots=merged,
        preview="修改就读申请 #" + str(target["id"])
                + "\n国家：" + merged["country"]
                + "\n模式：" + merged["application_mode"]
                + "\n项目：" + merged["program"]
                + "\n院校：" + (merged["university"] or "待定")
                + "\n备注：" + (merged["remark"] or "无"),
        requires_confirmation=True,
    )


def _prepare_application_advance(user, slots):
    """员工推进申请阶段必须定位具体申请，并且只能前进一个阶段。"""
    if not user or user.get("user_type") == "STUDENT" \
            or not rbac.has_capability(user, "application.manage"):
        return _result("STATE_ERROR", "当前角色无权推进申请阶段。")
    conn = get_db()
    try:
        scope, args = rbac.student_scope(user)
        rows = conn.execute(
            "SELECT a.*,u.real_name AS student_name,u.username AS student_username "
            "FROM application_progress a JOIN sys_user u ON u.id=a.student_id "
            "WHERE COALESCE(a.status,'') NOT IN ('已撤销','已完成')" + scope +
            " ORDER BY a.update_time DESC,a.id DESC",
            tuple(args),
        ).fetchall()
    finally:
        conn.close()
    candidates = [
        {
            "index": index,
            "application_id": int(row["id"]),
            "label": "申请 #" + str(row["id"]) + "｜"
                     + str(row["student_name"] or row["student_username"])
                     + "｜" + str(row["country"] or "国家待定")
                     + "｜" + str(row["program"] or "项目待定")
                     + "｜" + str(row["stage"] or "阶段待定"),
        }
        for index, row in enumerate(rows[:15], 1)
    ]
    application_id = int(slots.get("application_id") or 0)
    selection = int(slots.get("selection") or 0)
    if not application_id and selection and 1 <= selection <= len(candidates):
        application_id = int(candidates[selection - 1]["application_id"])
    student_name = str(slots.get("student_name") or "").strip()
    target = next((row for row in rows if int(row["id"]) == application_id), None)
    if not target and student_name:
        matches = [
            row for row in rows
            if student_name in (
                str(row["student_name"] or "") + str(row["student_username"] or "")
            )
        ]
        if len(matches) == 1:
            target = matches[0]
    if not target:
        return _result(
            "STATE_AMBIGUOUS", "请选择要推进的就读申请。",
            missing=["application_id"], candidates=candidates, slots=slots,
        )

    stages = lifecycle.APPLICATION_STAGES
    current_stage = str(target["stage"] or stages[0])
    if current_stage not in stages:
        current_stage = stages[0]
    current_index = stages.index(current_stage)
    if current_index >= len(stages) - 1:
        return _result("STATE_ERROR", "该申请已完成全部阶段，不能继续推进。")
    requested_stage = str(
        slots.get("application_stage") or slots.get("stage") or ""
    ).strip()
    next_stage = stages[current_index + 1]
    if requested_stage and requested_stage != next_stage:
        return _result(
            "STATE_ERROR",
            "申请只能按顺序推进；下一阶段应为“" + next_stage + "”。",
        )
    note = str(slots.get("note") or slots.get("advance_note") or "").strip()
    prepared = {
        "application_id": int(target["id"]),
        "student_name": str(target["student_name"] or target["student_username"]),
        "current_stage": current_stage,
        "application_stage": next_stage,
        "note": note or ("进入“" + next_stage + "”阶段"),
    }
    return _result(
        "STATE_CONFIRM", "申请推进信息已准备好，请确认。",
        slots=prepared,
        preview="推进就读申请\n学生：" + prepared["student_name"]
                + "\n申请：#" + str(prepared["application_id"])
                + "\n阶段：" + current_stage + " → " + next_stage
                + "\n说明：" + prepared["note"],
        requires_confirmation=True,
    )


def prepare_business_action(user, data):
    query = str(data.get("query") or "").strip()
    key = _draft_key(user, data.get("visitor_key", ""))
    draft = _load(key)
    action_type = (
        str(data.get("action_type") or "").strip()
        or (str(draft.get("action_type") or "") if draft else "")
        or _action_from_text(query)
    )
    # 当前消息若明确说了“查询/新增/修改/删除”等动作，以规则识别结果
    # 为准，避免 Dify 字段抽取把“查成绩”误判成“新增成绩”。没有动作词
    # 的续答（如“报名1”“确认”“林子涵”）仍沿用草稿中的动作类型。
    text_action_type = _action_from_text(query)
    if text_action_type:
        action_type = text_action_type
    if action_type not in ACTION_LABELS:
        return _result("STATE_ERROR", "没有识别到可执行的业务动作。")
    if not user and action_type != "event_register":
        return _result("STATE_ERROR", "访客只能办理活动报名，其他业务请先登录。")
    user_action = str(data.get("user_action") or "").strip().lower()
    incoming = data.get("slots") or {}
    if not isinstance(incoming, dict):
        incoming = {}
    slots = dict(draft.get("slots") or {}) if draft and draft.get("action_type") == action_type else {}
    for name, value in incoming.items():
        if value not in (None, "", [], {}):
            slots[name] = value
    if draft and draft.get("action_type") != action_type:
        slots = dict(incoming)
    for name in (
        "event_id", "student_id", "score_id", "ticket_id", "lead_id",
        "application_id", "selection",
    ):
        if name in slots:
            slots[name] = _coerce_int(slots[name])
    if (
        user_action in ("cancel", "取消", "撤销")
        and action_type not in ("feedback_cancel", "application_cancel")
        and not text_action_type
    ):
        _clear(key)
        return _result("STATE_CANCELLED", "已取消本次业务草稿。")

    if action_type == "event_register":
        result = _prepare_event(user, query, slots, draft.get("candidates") if draft else [])
    elif action_type in ("score_query", "score_upsert", "score_delete"):
        result = _prepare_score(
            user, query, action_type, slots,
            str(data.get("history") or ""),
        )
    elif action_type in ("feedback_create", "feedback_cancel"):
        result = _prepare_feedback(user, action_type, slots)
    elif action_type == "lead_create":
        result = _prepare_lead(user, slots)
    elif action_type == "report_create":
        result = _prepare_report(user, slots)
    elif action_type == "lead_follow_up":
        result = _prepare_lead_follow_up(
            user, slots, draft.get("candidates") if draft else [],
        )
    elif action_type == "feedback_resolve":
        result = _prepare_feedback_resolve(user, slots)
    elif action_type in (
        "application_create", "application_update", "application_cancel",
    ):
        result = _prepare_application(user, action_type, slots)
    elif action_type == "application_advance":
        result = _prepare_application_advance(user, slots)
    else:
        return _result("STATE_ERROR", "暂不支持该动作。")

    if result["status_token"] == "STATE_CONFIRM":
        if user_action == "confirm" and draft and draft.get("state") == "CONFIRMING":
            token = secrets.token_urlsafe(32)
            expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            _save(key, user, action_type, "READY", result["slots"],
                  confirmation_token=token, confirmation_expires_at=expires)
            result.update({
                "status_token": "STATE_READY",
                "message": "用户已确认，可以执行。",
                "confirmation_token": token,
            })
            return result
        _save(key, user, action_type, "CONFIRMING", result["slots"],
              candidates=result["candidates"], missing=result["missing_fields"])
        return result
    if result["status_token"] in ("STATE_MISSING", "STATE_AMBIGUOUS"):
        _save(key, user, action_type, "COLLECTING", result["slots"],
              candidates=result["candidates"], missing=result["missing_fields"])
        return result
    if result["status_token"] == "STATE_READY":
        token = secrets.token_urlsafe(32)
        expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        _save(key, user, action_type, "READY", result["slots"],
              confirmation_token=token, confirmation_expires_at=expires)
        result["confirmation_token"] = token
        return result
    return result


def _score_execute(user, action_type, slots):
    conn = get_db()
    try:
        students = score_chat_logic._affected_students(conn, user)
        student = next(
            (row for row in students if int(row["id"]) == int(slots.get("student_id") or 0)),
            None,
        )
        if not student:
            return _result("STATE_ERROR", "学生不在当前员工数据范围内。")
        scores = score_chat_logic._query_scores(conn, student["id"])
        name = str(student["real_name"] or student["username"])
        if action_type == "score_query":
            lines = ["已查到 " + name + " 的成绩："]
            if not scores:
                lines.append("暂无成绩记录。")
            for index, row in enumerate(scores, 1):
                lines.append(str(index) + ". " + str(row["course_name"]) + "：" + str(row["score"]) + " 分")
            return _result("STATE_EXECUTED", "\n".join(lines))
        score_id = int(slots.get("score_id") or 0)
        target = next((row for row in scores if int(row["id"]) == score_id), None)
        if action_type == "score_delete":
            if not target:
                return _result("STATE_ERROR", "成绩记录不存在。")
            conn.execute("DELETE FROM student_score WHERE id=?", (score_id,))
            conn.commit()
            return _result("STATE_EXECUTED",
                           "已删除 " + name + " 的" + str(target["course_name"]) + "成绩。",
                           changed=True)
        value = float(slots.get("score_value"))
        course = str(slots.get("course_name") or "")
        if target:
            conn.execute("UPDATE student_score SET score=? WHERE id=?", (value, score_id))
            conn.commit()
            return _result("STATE_EXECUTED",
                           "已将 " + name + " 的" + str(target["course_name"])
                           + "成绩更新为 " + str(value) + " 分。", changed=True)
        conn.execute(
            "INSERT INTO student_score (student_id,course_name,score,semester) "
            "VALUES (?,?,?,?)",
            (student["id"], course, value, "智能助手录入"),
        )
        conn.commit()
        return _result("STATE_EXECUTED",
                       "已录入 " + name + " 的" + course + "成绩：" + str(value) + " 分。",
                       changed=True)
    finally:
        conn.close()


def execute_business_action(user, confirmation_token, visitor_key=""):
    key = _draft_key(user, visitor_key)
    draft = _load(key)
    if not draft or draft.get("state") != "READY":
        return _result("STATE_ERROR", "当前没有待执行的业务草稿。")
    expected = str(draft.get("confirmation_token") or "")
    if not expected or not hmac.compare_digest(expected, str(confirmation_token or "")):
        return _result("STATE_ERROR", "确认信息已失效，请重新确认。")
    try:
        expired = datetime.strptime(
            str(draft.get("confirmation_expires_at") or ""), "%Y-%m-%d %H:%M:%S"
        ) < datetime.now()
    except ValueError:
        expired = True
    if expired:
        _clear(key)
        return _result("STATE_ERROR", "确认已超时，请重新操作。")
    action_type = str(draft["action_type"])
    if not user and action_type != "event_register":
        _clear(key)
        return _result("STATE_ERROR", "访客只能执行活动报名。")
    slots = draft.get("slots") or {}
    try:
        if action_type == "event_register":
            actor_type = str(user.get("user_type") or "PUBLIC") if user else "PUBLIC"
            result = event_chat_logic.register_event_record(
                int(slots["event_id"]), str(slots["participant_name"]),
                str(slots["contact_info"]),
                student_id=int(user["id"]) if user and actor_type == "STUDENT" else 0,
                actor_type=actor_type,
            )
            changed = result.get("status") == "success"
            answer = {
                "success": "报名成功：" + str(slots.get("event_name") or ""),
                "duplicate": "该联系方式已经报名该活动。",
                "full": "活动名额已满。",
                "forbidden": "当前身份不能报名该活动。",
            }.get(result.get("status"), "活动报名失败。")
            output = _result("STATE_EXECUTED", answer, changed=changed,
                             registration_id=result.get("registration_id", 0))
        elif action_type in ("score_query", "score_upsert", "score_delete"):
            output = _score_execute(user, action_type, slots)
        elif action_type == "feedback_create":
            record = student_logic.submit_feedback(
                user["id"], str(slots["feedback_content"]),
                str(slots.get("feedback_detail") or ""),
            )
            output = _result("STATE_EXECUTED",
                             "售后工单已提交，单号 #" + str(record["id"]),
                             changed=True, ticket_id=record["id"])
        elif action_type == "feedback_cancel":
            status, row = student_logic.cancel_feedback(user["id"], int(slots["ticket_id"]))
            if status != "success":
                output = _result("STATE_ERROR", "工单状态已变化，无法撤回。")
            else:
                output = _result("STATE_EXECUTED",
                                 "已撤回售后工单 #" + str(slots["ticket_id"]),
                                 changed=True)
        elif action_type == "lead_create":
            owner_id = user["id"]
            record = employee_logic.add_crm(
                str(slots["customer_name"]), str(slots["contact_info"]),
                owner_id, str(slots.get("background_info") or ""),
                target_country=str(slots.get("target_country") or ""),
            )
            output = _result("STATE_EXECUTED",
                             "客户线索已录入，编号 #" + str(record["id"]),
                             changed=True, lead_id=record["id"])
        elif action_type == "report_create":
            record = employee_logic.add_report(
                int(user["id"]), str(slots["report_content"]),
                str(slots.get("report_date") or ""),
            )
            output = _result(
                "STATE_EXECUTED",
                "日报已提交，日期：" + str(record["report_date"]),
                changed=True, report_id=record["id"],
            )
        elif action_type == "lead_follow_up":
            if not rbac.has_capability(user, "crm.manage"):
                output = _result("STATE_ERROR", "当前角色无权跟进客户线索。")
            else:
                conn = get_db()
                try:
                    scope, scope_args = _lead_scope(user)
                    lead = conn.execute(
                        "SELECT l.* FROM crm_lead l WHERE l.id=?" + scope,
                        [int(slots["lead_id"])] + list(scope_args),
                    ).fetchone()
                    if not lead:
                        output = _result(
                            "STATE_ERROR", "客户不存在或不在当前员工数据范围内。"
                        )
                    else:
                        status = str(slots.get("lead_status") or lead["status"])
                        if status not in ("新增意向", "跟进中", "已签约", "已流失"):
                            output = _result("STATE_ERROR", "客户阶段无效。")
                        else:
                            note = str(slots.get("follow_up_note") or "").strip()
                            entry = "\n" + _now() + " [员工#" + str(user["id"]) + "] "
                            entry += (
                                note + "（阶段：" + status + "）"
                                if note else "阶段更新为：" + status
                            )
                            history = str(lead["follow_up_history"] or "")
                            conn.execute(
                                "UPDATE crm_lead SET status=?,follow_up_history=?,"
                                "update_time=? WHERE id=?",
                                (status, (history + entry).strip(), _now(),
                                 int(lead["id"])),
                            )
                            conn.commit()
                            output = _result(
                                "STATE_EXECUTED",
                                "已保存 " + str(lead["customer_name"])
                                + " 的跟进记录，当前阶段：" + status,
                                changed=True, lead_id=int(lead["id"]),
                            )
                finally:
                    conn.close()
        elif action_type == "feedback_resolve":
            if not rbac.has_capability(user, "feedback.resolve"):
                output = _result("STATE_ERROR", "当前角色无权处理售后工单。")
            else:
                conn = get_db()
                try:
                    scope, scope_args = rbac.student_scope(user)
                    target = conn.execute(
                        "SELECT t.id,t.student_id FROM student_feedback_ticket t "
                        "JOIN sys_user u ON u.id=t.student_id WHERE t.id=?"
                        " AND t.status IN ('待处理','处理中')" + scope,
                        [int(slots["ticket_id"])] + list(scope_args),
                    ).fetchone()
                finally:
                    conn.close()
                if not target:
                    output = _result(
                        "STATE_ERROR", "工单不存在、已处理或不在当前数据范围内。"
                    )
                else:
                    resolved = employee_logic.resolve_feedback(
                        int(target["id"]), str(slots["solution"])
                    )
                    if not isinstance(resolved, dict):
                        output = _result("STATE_ERROR", "工单状态已变化，请刷新后重试。")
                    else:
                        notified = notify(
                            int(target["student_id"]), "售后", "售后工单已解决",
                            str(slots["solution"]), "feedback",
                        )
                        if notified:
                            conn = get_db()
                            try:
                                conn.execute(
                                    "UPDATE student_feedback_ticket SET is_notified=1 "
                                    "WHERE id=?",
                                    (int(target["id"]),),
                                )
                                conn.commit()
                            finally:
                                conn.close()
                        output = _result(
                            "STATE_EXECUTED",
                            "售后工单 #" + str(target["id"]) + " 已解决"
                            + ("，并已通知学生。" if notified else "；通知暂未送达。"),
                            changed=True, ticket_id=int(target["id"]),
                            notified=notified,
                        )
        elif action_type in (
            "application_create", "application_update", "application_cancel",
        ):
            if not user or user.get("user_type") != "STUDENT":
                output = _result("STATE_ERROR", "只有学生可以办理本人的就读申请。")
            else:
                # 申请档案的阶段补全与操作日志复用申请模块的唯一实现，
                # 避免 AI 链路和页面链路写出两套不同格式。
                from api.application import (
                    STAGES, _ensure_stage_records, _insert_history,
                )
                conn = get_db()
                try:
                    if action_type == "application_create":
                        stage = lifecycle.get_stage(user["id"])
                        if stage in ("ENROLLED", "ALUMNI"):
                            output = _result(
                                "STATE_ERROR", "你已入学或毕业，无需重复提交申请。"
                            )
                        else:
                            country = str(slots["country"])
                            mode = str(slots["application_mode"])
                            program_name = str(slots["program"])
                            valid, _ = _validate_application_choice(
                                country, mode, program_name
                            )
                            existing = conn.execute(
                                "SELECT id FROM application_progress "
                                "WHERE student_id=? AND country=? AND program=? "
                                "AND status IN ('待受理','进行中')",
                                (user["id"], country, program_name),
                            ).fetchone()
                            if not valid:
                                output = _result(
                                    "STATE_ERROR",
                                    "方案组合已变化，请重新从公司现有项目中选择。",
                                )
                            elif existing:
                                output = _result(
                                    "STATE_ERROR", "相同项目已有待受理或进行中的申请。"
                                )
                            else:
                                advisor = conn.execute(
                                    "SELECT id FROM sys_user "
                                    "WHERE user_type='EMPLOYEE' "
                                    "AND staff_role='ADVISOR' AND status='正常' "
                                    "ORDER BY id LIMIT 1"
                                ).fetchone()
                                advisor_id = advisor["id"] if advisor else None
                                cursor = conn.execute(
                                    "INSERT INTO application_progress "
                                    "(student_id,country,university,program,stage,"
                                    "stage_detail,status,operator_id,initiator_type,"
                                    "application_mode,student_remark,create_time,"
                                    "update_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                    (
                                        user["id"], country,
                                        str(slots.get("university") or ""),
                                        program_name, STAGES[0],
                                        "学生已主动提交就读申请，等待顾问受理",
                                        "待受理", advisor_id, "STUDENT",
                                        mode, str(slots.get("remark") or ""),
                                        _now(), _now(),
                                    ),
                                )
                                app_id = int(cursor.lastrowid)
                                _insert_history(
                                    conn, app_id, STAGES[0],
                                    "学生主动提交就读申请", user["id"],
                                    action="提交申请",
                                )
                                _ensure_stage_records(conn, app_id)
                                conn.execute(
                                    "UPDATE sys_user SET lifecycle_stage='APPLYING',"
                                    "advisor_id=COALESCE(advisor_id,?) WHERE id=?",
                                    (advisor_id, user["id"]),
                                )
                                conn.commit()
                                if advisor_id:
                                    notify(
                                        advisor_id, "申请", "新的学生就读申请",
                                        str(user.get("real_name") or user["username"])
                                        + " 申请 " + country + " / " + program_name
                                        + "，请及时受理。",
                                        "application",
                                    )
                                output = _result(
                                    "STATE_EXECUTED",
                                    "就读申请已提交，申请编号 #" + str(app_id),
                                    changed=True, application_id=app_id,
                                )
                    elif action_type == "application_update":
                        app_id = int(slots["application_id"])
                        row = conn.execute(
                            "SELECT * FROM application_progress "
                            "WHERE id=? AND student_id=?",
                            (app_id, user["id"]),
                        ).fetchone()
                        if not row or row["initiator_type"] != "STUDENT" \
                                or row["status"] != "待受理":
                            output = _result(
                                "STATE_ERROR", "只有本人待受理的申请可以修改。"
                            )
                        else:
                            country = str(slots["country"])
                            mode = str(slots["application_mode"])
                            program_name = str(slots["program"])
                            valid, _ = _validate_application_choice(
                                country, mode, program_name
                            )
                            duplicate = conn.execute(
                                "SELECT id FROM application_progress "
                                "WHERE student_id=? AND id<>? AND country=? "
                                "AND program=? AND status IN ('待受理','进行中')",
                                (user["id"], app_id, country, program_name),
                            ).fetchone()
                            if not valid:
                                output = _result(
                                    "STATE_ERROR",
                                    "修改后的方案组合不在公司现有项目范围内。",
                                )
                            elif duplicate:
                                output = _result(
                                    "STATE_ERROR", "已有相同项目的有效申请。"
                                )
                            else:
                                conn.execute(
                                    "UPDATE application_progress SET country=?,"
                                    "application_mode=?,program=?,university=?,"
                                    "student_remark=?,update_time=? WHERE id=?",
                                    (
                                        country, mode, program_name,
                                        str(slots.get("university") or ""),
                                        str(slots.get("remark") or ""),
                                        _now(), app_id,
                                    ),
                                )
                                _insert_history(
                                    conn, app_id, row["stage"],
                                    "学生修改了申请信息", user["id"],
                                    action="修改申请",
                                )
                                conn.commit()
                                if row["operator_id"]:
                                    notify(
                                        row["operator_id"], "申请",
                                        "学生修改了就读申请",
                                        str(user.get("real_name") or user["username"])
                                        + " 修改了申请，请重新确认。",
                                        "application",
                                    )
                                output = _result(
                                    "STATE_EXECUTED", "申请信息已修改。",
                                    changed=True, application_id=app_id,
                                )
                    else:
                        app_id = int(slots["application_id"])
                        row = conn.execute(
                            "SELECT * FROM application_progress "
                            "WHERE id=? AND student_id=?",
                            (app_id, user["id"]),
                        ).fetchone()
                        if not row or row["initiator_type"] != "STUDENT" \
                                or row["status"] != "待受理":
                            output = _result(
                                "STATE_ERROR", "只有本人待受理的申请可以撤销。"
                            )
                        else:
                            conn.execute(
                                "UPDATE application_progress SET status='已撤销',"
                                "stage_detail='学生主动撤销申请',update_time=? "
                                "WHERE id=?",
                                (_now(), app_id),
                            )
                            _insert_history(
                                conn, app_id, row["stage"], "学生撤销了申请",
                                user["id"], action="撤销申请",
                            )
                            active = conn.execute(
                                "SELECT COUNT(*) FROM application_progress "
                                "WHERE student_id=? AND status IN ('待受理','进行中')",
                                (user["id"],),
                            ).fetchone()[0]
                            if not active:
                                conn.execute(
                                    "UPDATE sys_user SET lifecycle_stage='CONSULT' "
                                    "WHERE id=? AND lifecycle_stage='APPLYING'",
                                    (user["id"],),
                                )
                            conn.commit()
                            if row["operator_id"]:
                                notify(
                                    row["operator_id"], "申请",
                                    "学生撤销了就读申请",
                                    str(user.get("real_name") or user["username"])
                                    + " 撤销了申请。",
                                    "application",
                                )
                            output = _result(
                                "STATE_EXECUTED", "就读申请已撤销。",
                                changed=True, application_id=app_id,
                            )
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        elif action_type == "application_advance":
            if (not user or user.get("user_type") == "STUDENT"
                    or not rbac.has_capability(user, "application.manage")):
                output = _result("STATE_ERROR", "当前角色无权推进申请阶段。")
            else:
                from api.application import _ensure_stage_records
                conn = get_db()
                try:
                    scope, scope_args = rbac.student_scope(user)
                    row = conn.execute(
                        "SELECT a.*,u.real_name AS student_name,"
                        "u.username AS student_username "
                        "FROM application_progress a "
                        "JOIN sys_user u ON u.id=a.student_id "
                        "WHERE a.id=?" + scope,
                        [int(slots["application_id"])] + list(scope_args),
                    ).fetchone()
                    if not row or row["status"] in ("已撤销", "已完成"):
                        output = _result(
                            "STATE_ERROR", "申请不存在、已撤销或已完成。"
                        )
                    else:
                        stages = lifecycle.APPLICATION_STAGES
                        current_stage = str(row["stage"] or stages[0])
                        if current_stage not in stages:
                            current_stage = stages[0]
                        current_index = stages.index(current_stage)
                        target_stage = str(slots["application_stage"])
                        expected_stage = (
                            stages[current_index + 1]
                            if current_index + 1 < len(stages) else ""
                        )
                        if not expected_stage or target_stage != expected_stage:
                            output = _result(
                                "STATE_ERROR", "申请阶段状态已变化，请重新确认。"
                            )
                        else:
                            next_index = current_index + 1
                            status = (
                                "已完成" if next_index == len(stages) - 1
                                else "进行中"
                            )
                            note = str(slots.get("note") or "").strip() or (
                                "进入“" + target_stage + "”阶段"
                            )
                            _ensure_stage_records(conn, int(row["id"]))
                            conn.execute(
                                "UPDATE application_progress SET stage=?,"
                                "stage_detail=?,status=?,operator_id=?,"
                                "update_time=? WHERE id=?",
                                (target_stage, note, status, user["id"],
                                 _now(), int(row["id"])),
                            )
                            conn.execute(
                                "UPDATE application_stage_record SET "
                                "status='已完成',summary=?,completed_at=?,"
                                "source_type='MANUAL',operator_id=?,update_time=? "
                                "WHERE application_id=? AND stage=?",
                                (note, _now(), user["id"], _now(),
                                 int(row["id"]), current_stage),
                            )
                            conn.execute(
                                "UPDATE application_stage_record SET status=?,"
                                "started_at=?,source_type='MANUAL',"
                                "operator_id=?,update_time=? "
                                "WHERE application_id=? AND stage=?",
                                ("已完成" if status == "已完成" else "进行中",
                                 _now(), user["id"], _now(),
                                 int(row["id"]), target_stage),
                            )
                            lifecycle.ensure_application_history(
                                conn, int(row["id"]), next_index,
                                user["id"], note,
                            )
                            conn.commit()
                            notify(
                                int(row["student_id"]), "申请",
                                "申请进度更新：" + target_stage,
                                note + "；"
                                + str(row["university"] or row["country"]
                                      or "你的申请"),
                                "application",
                            )
                            lifecycle.on_application_stage(
                                int(row["student_id"]), target_stage
                            )
                            output = _result(
                                "STATE_EXECUTED",
                                "已推进到“" + target_stage + "”阶段。",
                                changed=True,
                                application_id=int(row["id"]),
                                stage=target_stage,
                            )
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        else:
            output = _result("STATE_ERROR", "不支持的执行业务。")
    except Exception as exc:
        print("通用业务动作执行失败:", repr(exc))
        output = _result("STATE_ERROR", "业务执行失败，请稍后重试。")
    _save_result(key, action_type, output)
    _clear(key)
    return output
