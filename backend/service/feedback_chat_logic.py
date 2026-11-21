"""学生助手售后查询的确定性路由。

售后数据属于实时业务记录，不能交给大模型判断是否“收到反馈”，也不能让
大模型根据一句导航语句调用提交接口。本模块只处理明确的学生本人查询，
直接读取 student_feedback_ticket；真正的售后提交仍由售后反馈页面或 Dify
的显式提交分支完成。
"""
from __future__ import annotations

import re

from service import student_logic


FEEDBACK_WORDS = ("售后", "工单", "反馈记录", "投诉记录", "我的反馈", "我的投诉")

# 这些词代表“读取已有记录”，而不是描述一个需要新提交的问题。
QUERY_MARKERS = (
    "查询", "查看", "看看", "有哪些", "哪些", "多少", "几条", "记录",
    "进度", "状态", "结果", "到哪", "提交过", "列表", "详情",
    "处理得怎么样", "怎么办",
)

CANCEL_MARKERS = ("删除", "撤回", "取消", "撤销")


def _is_feedback_query(text):
    """判断是否为“查询我自己的售后记录”。

    “我的空调坏了，需要售后”包含‘我的’和‘售后’，但它是在描述新问题，
    不能因为这两个词就误提交工单；只有明确查询词，或整句就是“我的售后”
    等导航短语时才进入本路由。
    """
    query = (text or "").strip()
    compact = re.sub(r"\s+", "", query)
    if not any(word in compact for word in FEEDBACK_WORDS):
        return False
    if any(marker in compact for marker in QUERY_MARKERS):
        return True

    # “我的售后服务”“我的售后”“售后记录”等本身就是在请求查看记录。
    return bool(re.fullmatch(
        r"(?:我的)?(?:售后(?:服务|反馈|记录|工单)?|"
        r"反馈记录|投诉记录|工单(?:记录|列表)?)[？?。！!]*",
        compact,
    ))


def _feedback_answer(student_id):
    """把数据库中的真实工单整理成学生可直接看懂的答复。"""
    rows = student_logic.get_my_feedbacks(student_id)
    if not rows:
        return (
            "你目前没有售后工单。\n"
            "如需提交新问题，请进入「售后反馈」页面，填写具体遇到的问题后提交。"
        )

    solved = sum(1 for row in rows if row.get("status") == "已解决")
    pending = len(rows) - solved
    lines = [
        "你目前共有 " + str(len(rows)) + " 条售后工单"
        "（待处理 " + str(pending) + " 条，已解决 " + str(solved) + " 条）："
    ]
    for index, row in enumerate(rows, 1):
        line = (
            str(index) + ". 工单 #" + str(row.get("id"))
            + "｜" + str(row.get("content") or "未填写内容")
            + "｜状态：" + str(row.get("status") or "未知")
            + "｜提交时间：" + str(row.get("create_time") or "未知")
        )
        if row.get("solution"):
            line += "｜处理方案：" + str(row["solution"])
        lines.append(line)
    lines.append("如需提交新的问题，请进入「售后反馈」页面填写具体内容。")
    return "\n".join(lines)


def _ticket_ordinal(text):
    """解析“第2条”中的中文或阿拉伯数字序号。"""
    match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*(?:条|个|项)", text or "")
    if not match:
        return None
    value = match.group(1)
    digits = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    return int(value) if value.isdigit() else digits.get(value)


def _is_feedback_cancel(text):
    compact = re.sub(r"\s+", "", text or "")
    return (
        any(word in compact for word in FEEDBACK_WORDS)
        and any(word in compact for word in CANCEL_MARKERS)
    )


def _handle_feedback_cancel(user, query):
    """撤回本人待处理工单；缺少目标时只给提示，绝不创建新工单。"""
    rows = student_logic.get_my_feedbacks(user["id"])
    if not rows:
        return {
            "answer": "你目前没有可撤回的售后工单。",
            "changed": False,
        }

    compact = re.sub(r"\s+", "", query or "")
    target_id = 0
    id_match = re.search(
        r"(?:售后|工单|反馈)(?:记录)?(?:id|编号|#)?[:：]?(\d+)",
        compact,
        re.I,
    )
    if id_match:
        target_id = int(id_match.group(1))
    elif re.search(r"(?:id|编号)[:：]?(\d+)", compact, re.I):
        target_id = int(re.search(
            r"(?:id|编号)[:：]?(\d+)", compact, re.I
        ).group(1))
    else:
        ordinal = _ticket_ordinal(compact)
        if ordinal and 1 <= ordinal <= len(rows):
            target_id = int(rows[ordinal - 1]["id"])

    if not target_id:
        pending = [row for row in rows if row.get("status") == "待处理"]
        lines = [
            "为防止误撤回，请指定要处理的售后工单编号。"
        ]
        if pending:
            lines.append("你当前待处理的工单：")
            for row in pending:
                lines.append(
                    "工单 #" + str(row.get("id")) + "｜"
                    + str(row.get("content") or "未填写内容")
                )
            lines.append(
                '请回复“删除售后 ' + str(pending[0].get("id")) + '”这种格式。'
            )
        else:
            lines.append("目前没有待处理工单，已解决或已撤销的工单不能再次撤回。")
        return {"answer": "\n".join(lines), "changed": False}

    status, row = student_logic.cancel_feedback(user["id"], target_id)
    if status == "not_found":
        return {
            "answer": "没有找到工单 #" + str(target_id)
                      + "，或该工单不属于你。",
            "changed": False,
        }
    if status == "not_pending":
        return {
            "answer": "工单 #" + str(target_id) + " 当前状态是“"
                      + str(row or "非待处理") + "”，不能撤回。",
            "changed": False,
        }
    if status != "success":
        return {
            "answer": "工单状态刚刚发生变化，请刷新后重试。",
            "changed": False,
        }
    return {
        "answer": "已撤回售后工单 #" + str(target_id)
                  + "。该记录已从你的工单列表移除，后台仍保留审计记录。",
        "changed": True,
    }


def handle_feedback_query(user, query):
    """明确查询售后时返回真实结果；不是查询或不是学生时返回 None。"""
    if not user or user.get("user_type") != "STUDENT":
        return None
    if not _is_feedback_query(query):
        return None
    return {
        "answer": _feedback_answer(user["id"]),
        "changed": False,
    }


def handle_feedback_request(user, query):
    """学生助手售后入口：撤回请求优先，其次才是售后记录查询。"""
    if not user or user.get("user_type") != "STUDENT":
        return None
    if _is_feedback_cancel(query):
        return _handle_feedback_cancel(user, query)
    return handle_feedback_query(user, query)
