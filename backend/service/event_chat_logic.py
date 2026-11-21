"""活动列表、编号选择和报名资料的确定性对话逻辑。

活动查询和报名属于运营业务数据，不应该让大模型自由发挥。本模块负责：
1. 从 event_lecture 读取最新活动；
2. 把“报名1/第1场”映射到具体活动；
3. 为免登录访客暂存已选场次，收集姓名和联系方式；
4. 在资料齐全后真实写入 event_registration，并校验名额和重复报名。
"""
from __future__ import annotations

import re
from datetime import date, datetime

from db import get_db
from service import crm_logic, lifecycle


CHINESE_NUMBERS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
SCOPE_LABELS = {
    "PUBLIC": "对外公开",
    "STUDENT": "学生活动",
    "EMPLOYEE": "员工内部",
    "ALL": "所有人",
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def upcoming_events(conn=None, scopes=None):
    """按活动可见范围返回尚未开始的活动。"""
    own = conn is None
    conn = conn or get_db()
    try:
        sql = "SELECT * FROM event_lecture"
        args = []
        if scopes:
            marks = ",".join("?" for _ in scopes)
            sql += (
                " WHERE COALESCE(NULLIF(audience_scope,''),'PUBLIC') IN ("
                + marks + ")"
            )
            args = list(scopes)
        sql += " ORDER BY start_time ASC, id ASC"
        rows = [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        if own:
            conn.close()
    today = date.today().isoformat()
    return [
        row for row in rows
        if not row.get("start_time") or str(row["start_time"])[:10] >= today
    ]


def event_list_answer(scopes=None, heading="当前可报名的活动如下："):
    """生成可操作的实时活动列表，编号可直接用于下一轮报名。"""
    events = upcoming_events(scopes=scopes)
    if not events:
        return "当前没有已发布的可报名活动。后续活动发布后，可以直接问我最新安排。"
    lines = [heading]
    for index, row in enumerate(events[:5], 1):
        maximum = int(row.get("max_participants") or 0)
        current = int(row.get("current_participants") or 0)
        seats = "不限名额" if maximum <= 0 else (
            "名额已满" if current >= maximum else "剩余 " + str(maximum - current) + " 个名额"
        )
        lines.append(
            str(index) + ". 「" + str(row.get("event_name") or "活动") + "」"
            + str(row.get("start_time") or "时间待定") + "，"
            + str(row.get("location") or "地点待定") + "，"
            + SCOPE_LABELS.get(row.get("audience_scope") or "PUBLIC", "活动")
            + "，" + seats
        )
    lines.append("要报名时直接回复“报名1”这种格式；选定后再补充姓名和手机号。")
    return "\n".join(lines)


def _event_number(query):
    compact = re.sub(r"\s+", "", query or "")
    match = re.search(
        r"(?:报名|参加|选择|选)(?:第)?([1-9]\d*|[一二三四五六七八九十])"
        r"(?:场|个|项)?",
        compact,
    )
    if not match:
        match = re.fullmatch(
            r"第([1-9]\d*|[一二三四五六七八九十])(?:场|个|项)?[？?。!！]*",
            compact,
        )
    if not match:
        return None
    value = match.group(1)
    return int(value) if value.isdigit() else CHINESE_NUMBERS.get(value)


def _event_by_name(query, events):
    """按活动全名或唯一活动类型匹配，名称不唯一时返回 None 让用户选择。"""
    text = re.sub(r"\s+", "", query or "")
    name_matches = [
        event for event in events
        if str(event.get("event_name") or "").replace(" ", "") in text
    ]
    if len(name_matches) == 1:
        return name_matches[0]
    type_matches = [
        event for event in events
        if event.get("event_type")
        and str(event["event_type"]).replace(" ", "") in text
    ]
    return type_matches[0] if len(type_matches) == 1 else None


def _registration_details(query):
    """从一句话中提取姓名和联系方式，支持手机和邮箱。"""
    text = query or ""
    phone = re.search(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d{9})(?!\d)", text)
    email = re.search(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text
    )
    contact = phone.group(1) if phone else (email.group(0).lower() if email else "")

    explicit = re.search(
        r"(?:姓名|名字|我叫|我是)\s*[:：]?\s*([\u4e00-\u9fa5·]{2,12})",
        text,
    )
    name = explicit.group(1) if explicit else ""
    if not name and contact:
        # 支持“张三 151xxxxxxxx”这种最常见的自然输入。
        cleaned = text
        if phone:
            cleaned = cleaned.replace(phone.group(0), " ")
        if email:
            cleaned = cleaned.replace(email.group(0), " ")
        cleaned = re.sub(
            r"报名|参加|选择|第?\s*\d+\s*(?:场|个|项)?|"
            r"电话|手机号|联系方式|姓名|名字|我叫|我是|[:：,，。]",
            " ",
            cleaned,
        )
        candidate = re.search(r"[\u4e00-\u9fa5·]{2,12}", cleaned)
        name = candidate.group(0) if candidate else ""
    return name.strip(), contact


def _visitor_key(value):
    key = (value or "anonymous").strip()
    return key[:128] or "anonymous"


def _get_draft(visitor_key):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM event_registration_draft WHERE visitor_key=?",
            (_visitor_key(visitor_key),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_draft(visitor_key, event, name="", contact=""):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO event_registration_draft "
            "(visitor_key,event_id,event_name,customer_name,contact_info,update_time) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(visitor_key) DO UPDATE SET "
            "event_id=excluded.event_id,event_name=excluded.event_name,"
            "customer_name=excluded.customer_name,contact_info=excluded.contact_info,"
            "update_time=excluded.update_time",
            (
                _visitor_key(visitor_key), int(event["id"]),
                str(event.get("event_name") or ""), name.strip(), contact.strip(), _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_draft(visitor_key):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM event_registration_draft WHERE visitor_key=?",
            (_visitor_key(visitor_key),),
        )
        conn.commit()
    finally:
        conn.close()


def register_event_record(event_id, name, contact, student_id=0,
                          target_country="", actor_type="PUBLIC"):
    """真实报名并返回结构化结果；与页面报名接口共用同一套约束。

    匿名访客报名会在同一个数据库事务内进入 CRM：已有联系方式则复用原线索，
    没有则创建新线索并分配顾问。这样活动名单和客户跟进不会形成两条断链。
    """
    conn = get_db()
    event = None
    contact = crm_logic.normalize_contact(contact)
    lead_info = {
        "lead_id": 0,
        "owner_employee_id": 0,
        "owner_name": "",
        "created": False,
    }
    try:
        conn.execute("BEGIN IMMEDIATE")
        event = conn.execute(
            "SELECT * FROM event_lecture WHERE id=?", (event_id,)
        ).fetchone()
        if not event:
            conn.rollback()
            return {"status": "not_found"}
        audience_scope = event["audience_scope"] or "PUBLIC"
        if audience_scope == "EMPLOYEE" and actor_type not in (
            "EMPLOYEE", "TEACHER", "ADMIN",
        ):
            conn.rollback()
            return {"status": "forbidden", "event": dict(event)}
        if audience_scope == "STUDENT" and actor_type in (
            "EMPLOYEE", "TEACHER", "ADMIN", "MAINTAINER",
        ):
            conn.rollback()
            return {"status": "forbidden", "event": dict(event)}
        duplicate = conn.execute(
            "SELECT * FROM event_registration "
            "WHERE event_id=? AND LOWER(TRIM(COALESCE(contact_info,'')))="
            "LOWER(TRIM(?)) LIMIT 1",
            (event_id, contact),
        ).fetchone()
        if duplicate:
            # 兼容历史数据：修复前产生的访客报名只有联系方式、没有 CRM 关联。
            # 再次提交时补建线索，但不重复占用名额，也不新增第二条报名。
            if not int(duplicate["customer_id"] or 0) and not int(
                duplicate["lead_id"] or 0
            ):
                lead_info = crm_logic.ensure_event_lead(
                    conn, event, name, contact,
                    target_country or str(duplicate["target_country"] or ""),
                )
                if lead_info["lead_id"]:
                    conn.execute(
                        "UPDATE event_registration SET lead_id=? WHERE id=?",
                        (lead_info["lead_id"], duplicate["id"]),
                    )
            conn.commit()
            return {
                "status": "duplicate",
                "event": dict(event),
                "registration_id": int(duplicate["id"]),
                **lead_info,
            }
        cursor = conn.execute(
            "UPDATE event_lecture SET current_participants=current_participants+1 "
            "WHERE id=? AND (COALESCE(max_participants,0)=0 "
            "OR COALESCE(current_participants,0)<max_participants)",
            (event_id,),
        )
        if cursor.rowcount == 0:
            conn.rollback()
            return {"status": "full", "event": dict(event)}

        # 学生报名代表校园活动参与，已有学生档案可按顾问/班主任范围查看；
        # 只有匿名客户报名才自动进入销售 CRM，避免给学生重复制造客户线索。
        if not student_id and actor_type in ("PUBLIC", "CUSTOMER"):
            lead_info = crm_logic.ensure_event_lead(
                conn, event, name, contact, target_country,
            )
        registration_cursor = conn.execute(
            "INSERT INTO event_registration "
            "(event_id,customer_id,status,contact_info,target_country,lead_id,create_time) "
            "VALUES (?,?,'已报名',?,?,?,?)",
            (
                event_id, student_id or 0, contact, target_country or "",
                lead_info["lead_id"], _now(),
            ),
        )
        conn.commit()
        result = {
            "status": "success",
            "registration_id": registration_cursor.lastrowid,
            "event": dict(event),
            "customer_name": name,
            "contact_info": contact,
            **lead_info,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if student_id and result.get("status") == "success":
        from service.notify_logic import notify
        notify(
            student_id, "活动", "报名成功：" + str(result["event"].get("event_name") or ""),
            "已成功报名「" + str(result["event"].get("event_name") or "")
            + "」，时间：" + str(result["event"].get("start_time") or "待定"),
            "service",
        )
    if result.get("lead_id") and result.get("owner_employee_id"):
        from service.notify_logic import notify
        notify(
            result["owner_employee_id"],
            "线索",
            "活动报名新线索：" + str(name or contact),
            "客户 " + str(name or contact) + " 报名「"
            + str(result["event"].get("event_name") or "")
            + "」，联系方式：" + str(contact) + "，请及时跟进。",
            "leads",
        )
    return result


def _registration_result_answer(result):
    event = result.get("event") or {}
    name = str(event.get("event_name") or "该活动")
    status = result.get("status")
    if status == "success":
        return (
            "报名成功。已为你登记「" + name + "」，时间："
            + str(event.get("start_time") or "待定") + "。"
        ), True
    if status == "duplicate":
        return "这个联系方式已经报名「" + name + "」，无需重复提交。", False
    if status == "full":
        return "「" + name + "」名额已满，暂时无法报名。", False
    if status == "forbidden":
        return "该活动仅面向指定对象开放，当前身份不能报名。", False
    return "没有找到该活动，请重新查询最新活动。", False


def _query_scope(text, user):
    """根据“学校/公司/内部”等限定词返回活动范围。"""
    compact = re.sub(r"\s+", "", text or "")
    staff = bool(user and user.get("user_type") in (
        "EMPLOYEE", "TEACHER", "ADMIN",
    ))
    if any(word in compact for word in ("员工活动", "员工内部", "公司内部", "内部活动", "团建")):
        return ("EMPLOYEE", "ALL") if staff else None
    if any(word in compact for word in ("学校", "校园", "校内", "学生活动", "在读学生")):
        return ("STUDENT", "ALL")
    if any(word in compact for word in ("公司", "企业", "对外", "公开活动")):
        return ("PUBLIC", "ALL")
    if staff:
        return ("PUBLIC", "STUDENT", "EMPLOYEE", "ALL")
    if user and user.get("user_type") == "STUDENT":
        return ("PUBLIC", "STUDENT", "ALL")
    return ("PUBLIC", "ALL")


def handle_event_chat(user, query, visitor_key=""):
    """处理活动列表和“报名1”多轮流程；非活动问题时返回 None。"""
    text = (query or "").strip()
    key = _visitor_key(visitor_key)
    draft = _get_draft(key)
    scopes = _query_scope(text, user)
    if scopes is None:
        return {
            "answer": "员工内部活动需要员工登录后才能查看，公开客服不会展示内部活动。",
            "changed": False,
        }
    actor_type = str(user.get("user_type") or "PUBLIC") if user else "PUBLIC"
    heading = "当前可报名的活动如下："
    if any(word in text for word in ("学校", "校园", "校内", "学生活动")):
        heading = "当前可报名的学生活动如下："
    elif any(word in text for word in ("公司", "企业", "对外", "公开活动")):
        heading = "公司近期对外活动如下："

    if draft and any(word in text for word in ("取消选择", "不报名了", "取消报名")):
        _clear_draft(key)
        return {"answer": "已取消本次报名选择，没有生成报名记录。", "changed": False}

    name, contact = _registration_details(text)
    if draft and (contact or name or "姓名" in text or "名字" in text):
        saved_name = name or str(draft.get("customer_name") or "")
        saved_contact = contact or str(draft.get("contact_info") or "")
        _save_draft(key, {
            "id": draft["event_id"],
            "event_name": draft.get("event_name") or "活动",
        }, saved_name, saved_contact)
        if saved_name and saved_contact:
            result = register_event_record(
                int(draft["event_id"]), saved_name, saved_contact,
                student_id=int(user["id"]) if user and actor_type == "STUDENT" else 0,
                actor_type=actor_type,
            )
            if result["status"] in ("success", "duplicate"):
                _clear_draft(key)
            answer, changed = _registration_result_answer(result)
            return {
                "answer": answer,
                "changed": changed,
                "registration_id": result.get("registration_id", 0),
            }
        missing = []
        if not saved_name:
            missing.append("姓名")
        if not saved_contact:
            missing.append("手机号或邮箱")
        return {
            "answer": "已选择「" + str(draft.get("event_name") or "活动")
                      + "」，还缺少" + "、".join(missing)
                      + "，请继续补充。",
            "changed": False,
        }

    if "报名" in text:
        matched_event = _event_by_name(text, upcoming_events(scopes=scopes))
        if matched_event:
            selected_name, selected_contact = _registration_details(text)
            _save_draft(key, matched_event, selected_name, selected_contact)
            if selected_name and selected_contact:
                result = register_event_record(
                    int(matched_event["id"]), selected_name, selected_contact,
                    student_id=int(user["id"]) if actor_type == "STUDENT" else 0,
                    actor_type=actor_type,
                )
                if result["status"] in ("success", "duplicate"):
                    _clear_draft(key)
                answer, changed = _registration_result_answer(result)
                return {
                    "answer": answer,
                    "changed": changed,
                    "registration_id": result.get("registration_id", 0),
                }
            return {
                "answer": "已选择「" + str(matched_event.get("event_name") or "活动")
                          + "」。请提供姓名和手机号，例如：张三 15180791410",
                "changed": False,
            }

    number = _event_number(text)
    if number:
        # 编号必须基于当前提问范围，例如公开访客的“报名1”不能选中
        # 员工内部培训，学生提问也只能在学生可见集合中编号。
        events = upcoming_events(scopes=scopes)
        if number > len(events):
            return {
                "answer": "没有第 " + str(number) + " 场活动，请回复“最近有什么活动”。",
                "changed": False,
            }
        event = events[number - 1]

        # 已登录学生优先使用账号里的姓名和联系方式，免去重复填写。
        if user and user.get("user_type") == "STUDENT":
            allowed, deny_msg = lifecycle.check(user["id"], "activities")
            if not allowed:
                return {"answer": deny_msg, "changed": False}
            conn = get_db()
            try:
                profile = conn.execute(
                    "SELECT real_name, contact_info FROM sys_user WHERE id=?",
                    (user["id"],),
                ).fetchone()
            finally:
                conn.close()
            student_name = str(profile["real_name"] or "") if profile else ""
            student_contact = str(profile["contact_info"] or "") if profile else ""
            if student_name and student_contact:
                result = register_event_record(
                    int(event["id"]), student_name, student_contact,
                    student_id=int(user["id"]), actor_type="STUDENT",
                )
                answer, changed = _registration_result_answer(result)
                return {
                    "answer": answer,
                    "changed": changed,
                    "registration_id": result.get("registration_id", 0),
                }

        selected_name, selected_contact = _registration_details(text)
        _save_draft(key, event, selected_name, selected_contact)
        if selected_name and selected_contact:
            result = register_event_record(
                int(event["id"]), selected_name, selected_contact,
                student_id=int(user["id"]) if actor_type == "STUDENT" else 0,
                actor_type=actor_type,
            )
            if result["status"] in ("success", "duplicate"):
                _clear_draft(key)
            answer, changed = _registration_result_answer(result)
            return {
                "answer": answer,
                "changed": changed,
                "registration_id": result.get("registration_id", 0),
            }
        return {
            "answer": "已选择第 " + str(number) + " 场「"
                      + str(event.get("event_name") or "活动")
                      + "」。请提供姓名和手机号，例如：张三 15180791410",
            "changed": False,
        }

    if any(word in text for word in ("员工活动", "团建", "研学", "文化交流", "内部活动")):
        return None
    event_words = ("活动", "讲座", "宣讲", "展会", "见面会", "联展", "分享会")
    if any(word in text for word in event_words):
        return {
            "answer": event_list_answer(scopes, heading),
            "changed": False,
        }
    return None
