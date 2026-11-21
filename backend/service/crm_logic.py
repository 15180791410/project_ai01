"""CRM 线索的共享业务规则。

活动报名、官网留资和员工录入都可能产生客户线索。把“联系方式归一化、
选择负责人、复用已有线索”集中在这里，避免三个入口各写一套判断逻辑。
本模块只读写 SQLite 运营数据，不保存任何公司介绍、留学方案或知识库内容。
"""
from __future__ import annotations

import re
from datetime import datetime


def now_text() -> str:
    """返回数据库统一使用的时间文本格式。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_contact(value: str) -> str:
    """统一手机号和邮箱格式，降低同一客户被重复建档的概率。

    手机号会去掉空格、短横线及中国区号 +86；邮箱统一转小写。
    其他联系方式只做首尾空白清理，避免擅自删改用户输入。
    """
    text = str(value or "").strip()
    if "@" in text:
        return text.lower()
    digits = re.sub(r"\D", "", text)
    if 7 <= len(digits) <= 15:
        if len(digits) == 13 and digits.startswith("86"):
            digits = digits[2:]
        return digits
    return re.sub(r"\s+", " ", text)


def pick_lead_owner(conn) -> int:
    """按当前线索量选择在职顾问；没有顾问时回退到管理员。

    只返回有 CRM 业务能力的账号，避免线索分配给班主任或维护人员。
    """
    row = conn.execute(
        "SELECT u.id, COUNT(l.id) AS lead_count "
        "FROM sys_user u "
        "LEFT JOIN crm_lead l ON l.owner_employee_id=u.id "
        "WHERE u.user_type='EMPLOYEE' AND u.staff_role='ADVISOR' "
        "AND u.status='正常' "
        "GROUP BY u.id ORDER BY lead_count ASC, u.id ASC LIMIT 1"
    ).fetchone()
    if row:
        return int(row["id"])
    fallback = conn.execute(
        "SELECT id FROM sys_user "
        "WHERE user_type='ADMIN' AND status='正常' ORDER BY id LIMIT 1"
    ).fetchone()
    return int(fallback["id"]) if fallback else 0


def ensure_event_lead(conn, event, customer_name: str, contact_info: str,
                      target_country: str = "") -> dict:
    """为活动报名创建或复用 CRM 线索。

    调用方必须处于同一 SQLite 事务中。这样活动名额、报名记录和 CRM 线索
    要么一起成功，要么一起回滚，不会出现“报名成功但顾问看不到”的半成品。

    Args:
        conn: 已开启事务的数据库连接。
        event: event_lecture 查询结果，需包含 id、event_name、country。
        customer_name: 报名人姓名。
        contact_info: 手机号或邮箱。
        target_country: 报名时选择的意向国家；为空时使用活动关联国家。

    Returns:
        包含 lead_id、owner_employee_id、owner_name、created 的字典。
        若系统没有任何可接单账号，lead_id 为 0，报名本身仍可继续落库。
    """
    contact = normalize_contact(contact_info)
    stamp = now_text()
    event_name = str(event["event_name"] or "活动")
    country = str(target_country or event["country"] or "").strip()
    source_note = stamp + " 通过活动报名留资：" + event_name + "（来源：粤晓助手/官网）"

    # 已有线索必须复用，既保留原销售阶段和跟进历史，也避免同一手机号多次报名
    # 被拆成多个互不相干的客户档案。
    existing = conn.execute(
        "SELECT * FROM crm_lead "
        "WHERE TRIM(COALESCE(contact_info,''))<>'' "
        "AND (contact_info=? OR LOWER(TRIM(contact_info))=LOWER(?)) "
        "ORDER BY id ASC LIMIT 1",
        (contact, contact),
    ).fetchone()
    if existing:
        old_history = str(existing["follow_up_history"] or "").strip()
        old_background = str(existing["background_info"] or "").strip()
        event_background = "活动报名：" + event_name
        new_background = old_background
        if event_background not in old_background:
            new_background = (old_background + "；" + event_background).strip("；")
        conn.execute(
            "UPDATE crm_lead SET "
            "background_info=?, "
            "target_country=CASE WHEN TRIM(COALESCE(target_country,''))='' THEN ? "
            "                    ELSE target_country END, "
            "source_event_id=COALESCE(source_event_id, ?), "
            "follow_up_history=?, update_time=? "
            "WHERE id=?",
            (
                new_background,
                country,
                int(event["id"]),
                (old_history + ("\n" if old_history else "") + source_note).strip(),
                stamp,
                int(existing["id"]),
            ),
        )
        owner_id = int(existing["owner_employee_id"] or 0)
        owner = conn.execute(
            "SELECT real_name, username FROM sys_user WHERE id=?", (owner_id,)
        ).fetchone()
        owner_name = (
            str(owner["real_name"] or owner["username"] or "")
            if owner else ""
        )
        return {
            "lead_id": int(existing["id"]),
            "owner_employee_id": owner_id,
            "owner_name": owner_name,
            "created": False,
        }

    owner_id = pick_lead_owner(conn)
    if not owner_id:
        return {
            "lead_id": 0,
            "owner_employee_id": 0,
            "owner_name": "",
            "created": False,
        }
    cursor = conn.execute(
        "INSERT INTO crm_lead "
        "(customer_name, contact_info, background_info, target_country, "
        " follow_up_history, status, owner_employee_id, source_event_id, "
        " create_time, update_time) "
        "VALUES (?, ?, ?, ?, ?, '新增意向', ?, ?, ?, ?)",
        (
            str(customer_name or "活动访客").strip() or "活动访客",
            contact,
            "活动报名：" + event_name,
            country,
            source_note,
            owner_id,
            int(event["id"]),
            stamp,
            stamp,
        ),
    )
    owner = conn.execute(
        "SELECT real_name, username FROM sys_user WHERE id=?", (owner_id,)
    ).fetchone()
    owner_name = (
        str(owner["real_name"] or owner["username"] or "")
        if owner else ""
    )
    return {
        "lead_id": int(cursor.lastrowid),
        "owner_employee_id": owner_id,
        "owner_name": owner_name,
        "created": True,
    }
