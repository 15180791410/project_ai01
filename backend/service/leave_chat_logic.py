"""学生智能助手请假的后端准备、确认与降级逻辑。

正常主链路由 Dify 完成意图分类、LLM 字段提取、追问和复述确认：

    Dify
    → prepare_leave_from_extraction（只保存草稿、归一化日期、检查缺失/重叠）
    → Dify 条件分支与确认
    → confirm_prepared_leave（消费一次性令牌后才正式写库）

`handle_leave_request` 只保留为 Dify 超时或不可用时的确定性降级路径。
草稿仍保存在 leave_request_draft，不依赖大模型记忆。
"""
from __future__ import annotations

import re
import hmac
import secrets
from datetime import datetime, timedelta

from db import get_db
from service import lifecycle, student_logic
from utils import time_parser


QUERY_WORDS = (
    "请假记录", "请假情况", "请假审批", "请假进度", "请过假", "我的请假",
    "怎么请假", "如何请假", "请假流程", "请假条件", "能否请假",
    "可以请假吗", "能请假吗",
)
LEAVE_TYPES = ("病假", "事假", "年假", "婚假", "丧假", "调休", "其他")
CANCEL_WORDS = ("取消请假", "取消", "不请了", "算了不请", "撤销请假草稿")
YES_WORDS = ("确认", "对", "是的", "没错", "可以", "没问题", "提交")
NO_WORDS = ("不对", "不是", "修改", "改一下")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_leave_request(text):
    if any(word in text for word in QUERY_WORDS):
        return False
    return bool(re.search(r"(?:我要|我想|帮我|需要)?\s*(?:请|休)(?:个)?假", text))


def _load_draft(student_id):
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM leave_request_draft WHERE student_id=?", (student_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def has_active_draft(student_id):
    draft = _load_draft(student_id)
    return bool(draft and draft.get("state") in ("COLLECTING", "CONFIRMING"))


def should_handle_as_leave(student_id, query):
    """Dify 不可用时判断是否应由后端请假降级处理器接管。"""
    return has_active_draft(student_id) or _is_leave_request(str(query or ""))


def _save_draft(student_id, fields, state, original_text):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO leave_request_draft "
            "(student_id,leave_type,start_date,end_date,days,reason,state,"
            " original_text,update_time) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(student_id) DO UPDATE SET "
            "leave_type=excluded.leave_type,start_date=excluded.start_date,"
            "end_date=excluded.end_date,days=excluded.days,reason=excluded.reason,"
            "state=excluded.state,original_text=excluded.original_text,"
            "update_time=excluded.update_time",
            (
                student_id, fields.get("leave_type", ""),
                fields.get("start_date", ""), fields.get("end_date", ""),
                fields.get("days"), fields.get("reason", ""), state,
                original_text[-1000:], _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_draft(student_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM leave_request_draft WHERE student_id=?", (student_id,))
        conn.commit()
    finally:
        conn.close()


def _extract_type(text):
    for leave_type in LEAVE_TYPES:
        if leave_type in text:
            return leave_type
    if any(word in text for word in ("生病", "感冒", "发烧", "不舒服", "医院", "复查")):
        return "病假"
    if any(word in text for word in ("家里", "家庭", "有事", "考试", "面试", "旅行")):
        return "事假"
    return ""


def _extract_reason(text):
    match = re.search(
        r"(?:因为|由于|原因是|原因[:：])\s*([^。！？\n]+)", text
    )
    if match:
        return match.group(1).strip()
    reason_words = (
        "感冒", "发烧", "生病", "不舒服", "医院", "复查", "身体不适",
        "家里有事", "家庭原因", "考试", "面试", "回家", "旅行", "休息",
    )
    hits = [word for word in reason_words if word in text]
    return "、".join(hits)


def _existing_overlap(student_id, start_date, end_date):
    """在追问缺失字段前先检查日期重叠，重复申请应立刻得到原单号。"""
    if not start_date or not end_date:
        return None
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id,start_time,end_time,status FROM student_admin_service "
            "WHERE student_id=? AND service_type='请假' AND status='待审批' "
            "AND start_time<=? AND end_time>=? "
            "ORDER BY id DESC LIMIT 1",
            (student_id, end_date, start_date),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _merge_fields(draft, text):
    # 只解析本轮表达，旧轮次已经结构化保存在 draft 中。不能把全部历史原文
    # 再次拼接解析，否则旧的非法日期或旧区间会污染最新一次修改。
    date_fields = time_parser.extract_leave_date_fields(text)
    current_has_date_expression = bool(
        date_fields.get("start")
        or date_fields.get("error")
        or time_parser.has_leave_date_expression(text)
    )
    date_error = date_fields.get("error", "")
    start_date = date_fields.get("start", "") if current_has_date_expression else (
        (draft or {}).get("start_date", "")
    )
    end_date = date_fields.get("end", "") if current_has_date_expression else (
        (draft or {}).get("end_date", "")
    )
    days = date_fields.get("days") if current_has_date_expression else (
        (draft or {}).get("days")
    )
    if date_error:
        start_date = ""
        end_date = ""
        days = None
    if not date_error and start_date and end_date:
        date_error = time_parser.validate_leave_window(start_date, end_date)
    leave_type = _extract_type(text) or (draft or {}).get("leave_type", "")
    reason = _extract_reason(text) or (draft or {}).get("reason", "")
    return text, {
        "leave_type": leave_type,
        "start_date": start_date or "",
        "end_date": end_date or "",
        "days": days,
        "reason": reason,
        "date_ambiguous": bool(
            current_has_date_expression and date_fields.get("ambiguous")
        ),
        "error": date_error,
    }


def _missing_answer(fields):
    questions = []
    if not fields.get("start_date") or fields.get("date_ambiguous"):
        questions.append("请告诉我具体开始日期，例如“2026-11-25”或“下周一”")
    if fields.get("start_date") and not fields.get("days"):
        questions.append("请告诉我请假天数，例如“1天”或“3天”")
    if not fields.get("leave_type"):
        questions.append("请说明请假类型：病假、事假或其他")
    if not fields.get("reason"):
        questions.append("请补充具体请假原因")
    if not questions:
        return ""
    return "还需补充以下信息才能提交请假：\n" + "\n".join(
        str(index) + ". " + question
        for index, question in enumerate(questions, 1)
    )


def _confirmation_answer(fields):
    return (
        "请确认请假信息：\n"
        "类型：" + fields["leave_type"] + "\n"
        "日期：" + fields["start_date"] + " 至 " + fields["end_date"] + "\n"
        "天数：" + str(fields["days"]) + " 天\n"
        "原因：" + fields["reason"] + "\n"
        "确认提交请回复“确认”，需要修改请回复“修改”。"
    )


def _normalize_action(value):
    text = str(value or "").strip().lower()
    if text in ("confirm", "confirmed", "确认", "确认提交"):
        return "confirm"
    if text in ("cancel", "cancelled", "取消", "撤销"):
        return "cancel"
    if text in ("modify", "update", "修改", "更正"):
        return "modify"
    return "collect"


def _prepared_fields(fields):
    return {
        "leave_type": str(fields.get("leave_type") or ""),
        "start_date": str(fields.get("start_date") or ""),
        "end_date": str(fields.get("end_date") or ""),
        "days": fields.get("days"),
        "reason": str(fields.get("reason") or ""),
    }


def _prepare_state(status_token, message="", **extra):
    result = {
        "status_token": status_token,
        "message": message,
        "missing_fields": [],
        "fields": {},
        "confirmation_token": "",
    }
    result.update(extra)
    return result


def prepare_leave_from_extraction(user, extraction):
    """接收 Dify 的字段提取结果，只准备草稿，不创建正式请假单。

    Dify 负责意图、字段提取、追问和复述；本函数负责日期归一化、缺失字段
    汇总、重叠检查和确认令牌。正式写入只允许由 confirm_prepared_leave 完成。
    """
    if not user or user.get("user_type") != "STUDENT":
        return _prepare_state("STATE_ERROR", "只有学生账号可以办理请假。")
    allowed, deny_msg = lifecycle.check(user["id"], "leave")
    if not allowed:
        return _prepare_state("STATE_ERROR", deny_msg)

    data = extraction or {}
    query = str(data.get("query") or "").strip()
    action = _normalize_action(data.get("action"))
    draft = _load_draft(user["id"])
    if action == "cancel":
        _clear_draft(user["id"])
        return _prepare_state(
            "STATE_CANCELLED", "已取消本次请假草稿，没有生成请假单。"
        )

    if action == "confirm":
        if not draft or draft.get("state") != "CONFIRMING":
            return _prepare_state(
                "STATE_ERROR", "当前没有待确认的请假信息，请先说明请假需求。"
            )
        fields = _prepared_fields(draft)
        if not fields["leave_type"] or not fields["start_date"] or not fields["end_date"] or not fields["reason"]:
            return _prepare_state(
                "STATE_MISSING", "请假信息不完整，请先补齐。",
                missing_fields=["leave_type", "dates", "reason"],
                fields=fields,
            )
        confirmation_token = secrets.token_urlsafe(32)
        expires_at = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        try:
            conn.execute(
                "UPDATE leave_request_draft SET state='CONFIRMING',confirmation_token=?,"
                "confirmation_expires_at=?,request_version=COALESCE(request_version,0)+1,"
                "update_time=? WHERE student_id=?",
                (confirmation_token, expires_at, _now(), user["id"]),
            )
            conn.commit()
        finally:
            conn.close()
        return _prepare_state(
            "STATE_READY", "用户已确认，允许调用正式写库工具。",
            fields=fields, confirmation_token=confirmation_token,
            expires_at=expires_at,
        )

    if action == "modify" and draft and draft.get("state") == "CONFIRMING":
        # “修改”本身只表示进入重新收集状态，不能沿用旧内容再次要求确认。
        fields = _prepared_fields(draft)
        _save_draft(user["id"], fields, "COLLECTING", query)
        return _prepare_state(
            "STATE_MISSING", "请说明要修改的日期、类型或原因。",
            missing_fields=["修改内容"], fields=fields,
        )

    # 用户明确重新说“我要请假/我想请假/帮我请假”时，视为开启一张
    # 新申请，必须丢弃上一轮遗留日期、类型和原因。否则旧草稿会把
    # 已过期日期带入新一轮，造成“我只说要请假，却显示上个月日期”。
    if _is_leave_request(query) and action == "collect":
        _clear_draft(user["id"])
        draft = None

    date_text = " ".join(filter(None, (
        str(data.get("date_text") or "").strip(),
        str(data.get("start_date") or "").strip(),
        str(data.get("end_date") or "").strip(),
        query,
    )))
    date_fields = time_parser.extract_leave_date_fields(date_text)
    if date_fields.get("error"):
        fields = {
            "leave_type": str(data.get("leave_type") or _extract_type(query)
                              or (draft or {}).get("leave_type", "")),
            "start_date": "",
            "end_date": "",
            "days": None,
            "reason": str(data.get("reason") or _extract_reason(query)
                          or (draft or {}).get("reason", "")),
        }
        _save_draft(user["id"], fields, "COLLECTING", query)
        return _prepare_state(
            "STATE_ERROR", str(date_fields["error"]),
            missing_fields=["valid_start_date"], fields=_prepared_fields(fields),
            error=str(date_fields["error"]),
        )

    current_has_date = bool(
        date_fields.get("start")
        or time_parser.has_leave_date_expression(date_text)
    )
    start_date = date_fields.get("start", "") if current_has_date else (
        (draft or {}).get("start_date", "")
    )
    end_date = date_fields.get("end", "") if current_has_date else (
        (draft or {}).get("end_date", "")
    )
    days = date_fields.get("days") if current_has_date else (draft or {}).get("days")
    fields = {
        "leave_type": str(data.get("leave_type") or _extract_type(query)
                          or (draft or {}).get("leave_type", "")),
        "start_date": str(start_date or ""),
        "end_date": str(end_date or ""),
        "days": days,
        "reason": str(data.get("reason") or _extract_reason(query)
                      or (draft or {}).get("reason", "")),
    }
    missing_fields = []
    if not fields["start_date"] or (
        current_has_date and date_fields.get("ambiguous")
    ):
        missing_fields.append("start_date")
    if not fields["end_date"]:
        missing_fields.append("end_date")
    if not fields["leave_type"]:
        missing_fields.append("leave_type")
    if not fields["reason"]:
        missing_fields.append("reason")
    if missing_fields:
        _save_draft(user["id"], fields, "COLLECTING", query)
        return _prepare_state(
            "STATE_MISSING", "请补齐缺失的请假字段。",
            missing_fields=missing_fields, fields=_prepared_fields(fields),
        )

    window_error = time_parser.validate_leave_window(
        fields["start_date"], fields["end_date"]
    )
    if window_error:
        _save_draft(user["id"], fields, "COLLECTING", query)
        return _prepare_state(
            "STATE_ERROR", window_error,
            missing_fields=["valid_start_date"], fields=_prepared_fields(fields),
            error=window_error,
        )

    overlap = _existing_overlap(user["id"], fields["start_date"], fields["end_date"])
    if overlap:
        _clear_draft(user["id"])
        return _prepare_state(
            "STATE_DUPLICATE", "本次日期范围与已有待审批请假重叠。",
            fields=_prepared_fields(fields),
            duplicate={
                "id": overlap["id"],
                "start_date": overlap["start_time"],
                "end_date": overlap["end_time"],
                "status": overlap["status"],
            },
        )

    _save_draft(user["id"], fields, "CONFIRMING", query)
    return _prepare_state(
        "STATE_CONFIRM", "请假字段已齐全，请让用户确认识别结果。",
        fields=_prepared_fields(fields),
    )


def confirm_prepared_leave(user, confirmation_token):
    """消费用户确认令牌，执行最终业务校验并写入正式请假单。"""
    if not user or user.get("user_type") != "STUDENT":
        return _prepare_state("STATE_ERROR", "只有学生账号可以办理请假。")
    allowed, deny_msg = lifecycle.check(user["id"], "leave")
    if not allowed:
        return _prepare_state("STATE_ERROR", deny_msg)
    draft = _load_draft(user["id"])
    if not draft or draft.get("state") != "CONFIRMING":
        return _prepare_state("STATE_ERROR", "当前没有待提交的请假草稿。")
    expected = str(draft.get("confirmation_token") or "")
    if not expected or not hmac.compare_digest(
        expected, str(confirmation_token or "")
    ):
        return _prepare_state("STATE_ERROR", "确认信息已失效，请重新确认请假内容。")
    expires_at = str(draft.get("confirmation_expires_at") or "")
    try:
        expired = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S") < datetime.now()
    except ValueError:
        expired = True
    if expired:
        _clear_draft(user["id"])
        return _prepare_state("STATE_ERROR", "确认已超时，请重新提交请假信息。")
    fields = _prepared_fields(draft)
    try:
        result = _submit_draft(user, fields)
    except ValueError as exc:
        return _prepare_state("STATE_ERROR", str(exc), fields=fields)
    return _prepare_state(
        "STATE_DUPLICATE" if result.get("duplicate") else "STATE_SUBMITTED",
        result.get("answer", ""),
        fields=fields,
        leave=result,
    )


def _submit_draft(user, fields):
    reason = fields["leave_type"] + "；原因：" + fields["reason"]
    result = student_logic.handle_leave(
        user["id"], reason,
        start_time=fields["start_date"],
        end_time=fields["end_date"],
    )
    _clear_draft(user["id"])
    if result.get("duplicate"):
        answer = (
            "你已有一条日期范围重叠的待审批请假，不能重复提交。\n"
            "本次申请：" + str(fields.get("start_date")) + " 至 "
            + str(fields.get("end_date")) + "\n"
            "待审批单号：#" + str(result.get("id")) + "\n"
            "已有申请：" + str(result.get("开始")) + " 至 " + str(result.get("结束")) + "\n"
            "当前状态：" + str(result.get("状态") or "待审批")
        )
    else:
        answer = (
            "请假申请已提交。\n"
            "单号：#" + str(result.get("id")) + "\n"
            "日期：" + str(result.get("开始")) + " 至 " + str(result.get("结束")) + "\n"
            "天数：" + str(result.get("天数")) + " 天\n"
            "当前状态：待审批"
        )
    return {
        "answer": answer,
        "changed": not bool(result.get("duplicate")),
        "duplicate": bool(result.get("duplicate")),
        "leave_id": result.get("id", 0),
        "start_time": result.get("开始", ""),
        "end_time": result.get("结束", ""),
        "days": result.get("天数", 0),
    }


def handle_leave_request(user, query):
    """处理多轮请假；不是请假相关消息时返回 None。"""
    if not user or user.get("user_type") != "STUDENT":
        return None
    text = str(query or "").strip()
    draft = _load_draft(user["id"])
    if not draft and not _is_leave_request(text):
        return None

    allowed, deny_msg = lifecycle.check(user["id"], "leave")
    if not allowed:
        return {"answer": deny_msg, "changed": False, "duplicate": False}

    if draft and any(word in text for word in CANCEL_WORDS):
        _clear_draft(user["id"])
        return {"answer": "已取消本次请假草稿，没有生成请假单。", "changed": False}

    if draft and draft.get("state") == "CONFIRMING":
        if any(word in text for word in YES_WORDS):
            try:
                return _submit_draft(user, draft)
            except ValueError as exc:
                return {"answer": str(exc), "changed": False}
        if any(word in text for word in NO_WORDS):
            # 修改时保留类型和原因，重置日期和天数，避免旧日期继续覆盖新表达。
            fields = {
                "leave_type": draft.get("leave_type", ""),
                "reason": draft.get("reason", ""),
                "start_date": "", "end_date": "", "days": None,
                "date_ambiguous": False,
            }
            _save_draft(user["id"], fields, "COLLECTING", text)
            answer = _missing_answer(fields)
            return {"answer": answer, "changed": False, "awaiting_info": True}

    combined, fields = _merge_fields(draft, text)
    if fields.get("error"):
        _save_draft(user["id"], fields, "COLLECTING", combined)
        return {
            "answer": str(fields["error"]),
            "changed": False,
            "awaiting_info": True,
            "error": True,
        }
    overlap = _existing_overlap(
        user["id"], fields.get("start_date"), fields.get("end_date")
    )
    if overlap:
        _clear_draft(user["id"])
        return {
            "answer": (
                "你已经提交过与本次日期重叠的请假，不能重复提交。\n"
                "本次申请：" + str(fields.get("start_date")) + " 至 "
                + str(fields.get("end_date")) + "\n"
                "待审批单号：#" + str(overlap["id"]) + "\n"
                "已有申请：" + str(overlap["start_time"]) + " 至 "
                + str(overlap["end_time"]) + "\n"
                "当前状态：" + str(overlap["status"])
            ),
            "changed": False,
            "duplicate": True,
            "leave_id": overlap["id"],
        }
    answer = _missing_answer(fields)
    if answer:
        _save_draft(user["id"], fields, "COLLECTING", combined)
        return {"answer": answer, "changed": False, "awaiting_info": True}

    # 信息齐全后先确认，绝不直接把模型/规则猜测的结果落库。
    _save_draft(user["id"], fields, "CONFIRMING", combined)
    return {
        "answer": _confirmation_answer(fields),
        "changed": False,
        "awaiting_confirmation": True,
    }
