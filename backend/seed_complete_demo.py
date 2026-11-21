"""生成覆盖全部角色和业务模块的完整演示数据。

执行前提：数据库为空时直接运行；已有基础演示账号时重复运行也不会重复插入。
知识库中的公司和留学方案不写入 SQLite，仍由 Dify 实时提供。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

from db import db_file, ensure_migrations
from seed_full_demo_data import seed as seed_extended
from seed_homeroom_demo import DEMO_PASSWORD, insert_user, seed as seed_base
from service import crm_logic


def _now(offset_days=0):
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d %H:%M:%S")


def _user_id(conn, username):
    row = conn.execute("SELECT id FROM sys_user WHERE username=?", (username,)).fetchone()
    return row["id"] if row else None


def _ensure_account(conn, username, real_name, user_type, staff_role,
                    department, manager_id, password):
    uid = _user_id(conn, username)
    if uid:
        return uid
    return insert_user(
        conn, username, real_name, user_type, staff_role, department,
        manager_id=manager_id, lifecycle="CONSULT", password=password,
    )


def _ensure_event(conn, name, event_type, start_time, location, country,
                  description, max_participants, audience_scope="ALL"):
    row = conn.execute(
        "SELECT id FROM event_lecture WHERE event_name=?", (name,)
    ).fetchone()
    if row:
        event_id = row["id"]
        conn.execute(
            "UPDATE event_lecture SET event_type=?,start_time=?,location=?,"
            "country=?,description=?,audience_scope=?,max_participants=? WHERE id=?",
            (event_type, start_time, location, country, description,
             audience_scope, max_participants, event_id),
        )
        return event_id
    return conn.execute(
        "INSERT INTO event_lecture "
        "(event_name,event_type,start_time,location,country,description,"
        "audience_scope,max_participants,current_participants,create_time) "
        "VALUES (?,?,?,?,?,?,?,?,0,?)",
        (name, event_type, start_time, location, country, description,
         audience_scope, max_participants, _now()),
    ).lastrowid


def _ensure_registration(conn, event_id, customer_id, contact, country):
    if customer_id:
        exists = conn.execute(
            "SELECT 1 FROM event_registration WHERE event_id=? AND customer_id=?",
            (event_id, customer_id),
        ).fetchone()
    else:
        exists = conn.execute(
            "SELECT 1 FROM event_registration WHERE event_id=? AND contact_info=?",
            (event_id, contact),
        ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO event_registration "
            "(event_id,customer_id,status,contact_info,target_country,create_time) "
            "VALUES (?,?,'已报名',?,?,?)",
            (event_id, customer_id, contact, country, _now()),
        )


def _backfill_event_leads(conn):
    """把历史访客报名补成 CRM 线索，并回填报名表 lead_id。

    只处理没有登录学生账号且尚未关联线索的记录；重复执行会跳过已有数据。
    """
    rows = conn.execute(
        "SELECT r.*, e.event_name, e.country AS event_country "
        "FROM event_registration r "
        "JOIN event_lecture e ON e.id=r.event_id "
        "WHERE COALESCE(r.customer_id,0)=0 "
        "AND COALESCE(r.lead_id,0)=0 "
        "AND TRIM(COALESCE(r.contact_info,''))<>'' "
        "ORDER BY r.id"
    ).fetchall()
    for row in rows:
        event = {
            "id": row["event_id"],
            "event_name": row["event_name"],
            "country": row["event_country"],
        }
        lead = crm_logic.ensure_event_lead(
            conn, event, "活动访客", row["contact_info"],
            str(row["target_country"] or ""),
        )
        if lead["lead_id"]:
            conn.execute(
                "UPDATE event_registration SET lead_id=? WHERE id=?",
                (lead["lead_id"], row["id"]),
            )

    # 旧报名表没有保存姓名，但对话日志保留了“张三 151...”这类原话。
    # 仅在客户名仍是占位值时尝试恢复，不覆盖人工维护过的真实姓名。
    placeholder_leads = conn.execute(
        "SELECT id, contact_info FROM crm_lead "
        "WHERE customer_name='活动访客' AND TRIM(COALESCE(contact_info,''))<>''"
    ).fetchall()
    for lead in placeholder_leads:
        contact = str(lead["contact_info"] or "")
        message = conn.execute(
            "SELECT user_input FROM conversation_log "
            "WHERE user_input LIKE ? ORDER BY id DESC LIMIT 1",
            ("%" + contact + "%",),
        ).fetchone()
        if not message:
            continue
        cleaned = str(message["user_input"] or "").replace(contact, " ")
        cleaned = re.sub(r"报名|参加|选择|手机号|电话|联系方式|姓名|名字", " ", cleaned)
        match = re.search(r"[\u4e00-\u9fa5·]{2,12}", cleaned)
        if match:
            conn.execute(
                "UPDATE crm_lead SET customer_name=? WHERE id=?",
                (match.group(0), lead["id"]),
            )


def _ensure_notification(conn, user_id, title, content, link_page="overview", is_read=0):
    exists = conn.execute(
        "SELECT 1 FROM notification WHERE user_id=? AND title=?", (user_id, title)
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO notification "
            "(user_id,type,title,content,is_read,link_page,create_time) "
            "VALUES (?, '系统', ?, ?, ?, ?, ?)",
            (user_id, title, content, is_read, link_page, _now()),
        )


def _normalize_emotion_data(conn):
    """把演示分数统一到当前需求分值：55/90/100/15/20。"""
    profiles = conn.execute(
        "SELECT student_id,emotion_history FROM student_psych_profile"
    ).fetchall()
    for profile in profiles:
        try:
            history = json.loads(profile["emotion_history"] or "[]")
        except Exception:
            history = []
        for entry in history:
            tag = entry.get("tag", "正常")
            text = entry.get("text", "")
            if tag == "高危":
                entry["score"] = 100 if any(
                    word in text for word in ("报复", "暴力", "干掉", "杀", "伤害")
                ) else 90
            elif tag == "关注":
                entry["score"] = 55
            else:
                entry["score"] = 15 if any(
                    word in text for word in ("开心", "顺利", "进步", "稳定")
                ) else 20
        latest = history[-1] if history else {"tag": "正常", "score": 20}
        conn.execute(
            "UPDATE student_psych_profile SET latest_emotion_tag=?,emotion_score=?,"
            "emotion_history=?,last_interaction_time=? WHERE student_id=?",
            (latest.get("tag", "正常"), latest.get("score", 20),
             json.dumps(history, ensure_ascii=False),
             latest.get("time") or _now(), profile["student_id"]),
        )
    for alert in conn.execute(
        "SELECT id,risk_level,trigger_reason FROM student_psych_alert"
    ).fetchall():
        if alert["risk_level"] == "高":
            score = 100 if any(
                word in (alert["trigger_reason"] or "")
                for word in ("报复", "暴力", "干掉", "杀", "伤害")
            ) else 90
        else:
            score = 55
        conn.execute(
            "UPDATE student_psych_alert SET emotion_score=? WHERE id=?",
            (score, alert["id"]),
        )


def seed_complete():
    """组合基础演示数据，并补齐维护人员、活动、报名和角色范围。"""
    # 兼容旧数据库：先补齐活动线索关联字段，再执行幂等种子数据。
    ensure_migrations()
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        user_count = conn.execute(
            "SELECT COUNT(*) FROM sys_user WHERE user_type IN "
            "('ADMIN','EMPLOYEE','TEACHER','MAINTAINER')"
        ).fetchone()[0]
    finally:
        conn.close()

    # 基础脚本只能用于空库；已有员工时跳过，避免 username 唯一键冲突。
    if user_count == 0:
        seed_base()
    seed_extended()

    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    try:
        admin_id = _user_id(conn, "admin")
        advisor1 = _user_id(conn, "advisor01")
        teacher1 = _user_id(conn, "teacher01")
        leader1 = _user_id(conn, "leader01")

        advisor2 = _ensure_account(
            conn, "advisor02", "梁顾问", "EMPLOYEE", "ADVISOR",
            "国际教育部", admin_id, "Advisor@2026",
        )
        teacher2 = _ensure_account(
            conn, "teacher02", "黄班主任", "EMPLOYEE", "HOMEROOM",
            "国际教育部", admin_id, "Teacher@2026",
        )
        maintainer = _ensure_account(
            conn, "maintainer01", "系统维护员", "MAINTAINER", "",
            "信息技术部", admin_id, "Maintainer@2026",
        )

        # 让两个顾问和两个班主任都有独立数据范围。
        second_scope_users = (
            "student06", "student07", "student08", "student09", "student10",
            "demo_student04", "demo_student05", "demo_student06",
            "demo_student07", "demo_student08",
        )
        marks = ",".join("?" for _ in second_scope_users)
        conn.execute(
            f"UPDATE sys_user SET advisor_id=?,homeroom_id=? WHERE username IN ({marks})",
            (advisor2, teacher2, *second_scope_users),
        )

        # CRM 线索均匀分配给两名顾问，便于演示数据范围。
        leads = conn.execute("SELECT id FROM crm_lead ORDER BY id").fetchall()
        for index, lead in enumerate(leads):
            conn.execute(
                "UPDATE crm_lead SET owner_employee_id=? WHERE id=?",
                (advisor1 if index % 2 == 0 else advisor2, lead["id"]),
            )

        events = [
            _ensure_event(
                conn, "新加坡招生官见面会", "招生宣讲会", "2026-12-01",
                "线上直播", "新加坡",
                "面向高二、高三及同等学历家庭，介绍新加坡本科升学路径并安排答疑。",
                50, "ALL",
            ),
            _ensure_event(
                conn, "德国双元制宣讲会", "项目宣讲会", "2026-12-05",
                "广州校区", "德国",
                "介绍德国双元制职业培训、企业实践、德语要求和就业升学路径。",
                30, "ALL",
            ),
            _ensure_event(
                conn, "2026秋季多国名校联展", "教育联展", "2026-12-20",
                "深圳会展中心", "新加坡、德国",
                "集中展示新加坡与德国升学项目，可现场咨询申请条件和材料要求。",
                200, "ALL",
            ),
            _ensure_event(
                conn, "留学生校园经验分享会", "分享会", "2026-11-28",
                "广州校区", "新加坡、德国",
                "在读学生分享课程适应、住宿生活和跨文化沟通经验。",
                60, "STUDENT",
            ),
            _ensure_event(
                conn, "在读学生签证续签说明会", "说明会", "2026-12-08",
                "线上直播", "新加坡、德国",
                "面向在读学生说明签证续签材料、时间节点和常见问题。",
                100, "STUDENT",
            ),
            _ensure_event(
                conn, "员工招生业务内训", "内部培训", "2026-11-27",
                "公司会议室", "",
                "内部员工招生口径、CRM 跟进规范和服务流程培训。",
                40, "EMPLOYEE",
            ),
        ]
        registrations = [
            (events[0], "student02", "新加坡"),
            (events[0], "student04", "新加坡"),
            (events[0], "demo_student03", "新加坡"),
            (events[1], "student01", "德国"),
            (events[1], "student03", "德国"),
            (events[1], "demo_student04", "德国"),
            (events[2], "student05", "德国"),
            (events[2], "demo_student05", "新加坡"),
            (events[2], "demo_student07", "新加坡"),
        ]
        for event_id, username, country in registrations:
            sid = _user_id(conn, username)
            if sid:
                contact = conn.execute(
                    "SELECT contact_info FROM sys_user WHERE id=?", (sid,)
                ).fetchone()["contact_info"] or username
                _ensure_registration(conn, event_id, sid, contact, country)
        _ensure_registration(
            conn, events[0], 0, "demo_guest_20261201", "新加坡"
        )
        _ensure_registration(
            conn, events[2], 0, "demo_guest_20261220", "德国"
        )

        # 公开聊天选择活动后的草稿，用于演示多轮补充姓名和手机号。
        conn.execute(
            "INSERT INTO event_registration_draft "
            "(visitor_key,event_id,event_name,customer_name,contact_info,"
            "target_country,update_time) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(visitor_key) DO UPDATE SET event_id=excluded.event_id,"
            "event_name=excluded.event_name,update_time=excluded.update_time",
            ("demo_activity_visitor", events[1], "德国双元制宣讲会",
             "演示访客", "", "德国", _now()),
        )

        conn.execute(
            "UPDATE event_lecture SET current_participants=("
            "SELECT COUNT(*) FROM event_registration r WHERE r.event_id=event_lecture.id)"
        )

        # 每种角色和每种业务都有通知样本。
        for uid in (admin_id, advisor1, advisor2, teacher1, teacher2, leader1, maintainer):
            if uid:
                _ensure_notification(
                    conn, uid, "演示数据已就绪",
                    "当前账号已配置对应角色和数据范围，可进入工作台查看。",
                    "overview", 0,
                )
        for username in (
            "student01", "student04", "student05", "student07",
            "demo_student03", "demo_student05", "demo_student07",
        ):
            sid = _user_id(conn, username)
            if sid:
                _ensure_notification(
                    conn, sid, "学习与服务提醒",
                    "近期成绩、活动和服务安排可在工作台查看。",
                    "overview", 0 if sid % 2 else 1,
                )

        # 对话记录覆盖三个 Agent，含正常、关注和高危。
        conversations = [
            (teacher1, "employee_agent", "查询今天待审批请假", "已返回待审批请假列表。", ""),
            (advisor2, "employee_agent", "查询赵女士跟进记录", "已返回客户档案与跟进历史。", ""),
            (leader1, "employee_agent", "统计各国家学生人数", "已按国家统计学生分布。", ""),
            (_user_id(conn, "student05"), "student_agent", "期末考试有点焦虑", "已记录你的情绪，可以联系老师进一步沟通。", "关注"),
            (_user_id(conn, "demo_student05"), "student_agent", "我想请假一天", "请假申请已提交，当前等待审批。", ""),
            (_user_id(conn, "demo_student08"), "student_agent", "最近压力很大，不想活了", "已触发高危预警并通知老师。", "高危"),
        ]
        for uid, agent_type, query, answer, tag in conversations:
            if uid and not conn.execute(
                "SELECT 1 FROM conversation_log WHERE user_id=? AND user_input=?",
                (uid, query),
            ).fetchone():
                conn.execute(
                    "INSERT INTO conversation_log "
                    "(user_id,agent_type,user_input,ai_output,emotion_tag,create_time) "
                    "VALUES (?,?,?,?,?,?)",
                    (uid, agent_type, query, answer, tag, _now()),
                )

        _normalize_emotion_data(conn)
        _backfill_event_leads(conn)
        conn.commit()
        counts = {
            table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in (
                "sys_user", "application_progress", "application_history",
                "student_admin_service", "student_score", "student_psych_profile",
                "student_psych_alert", "student_feedback_ticket", "crm_lead",
                "employee_daily_report", "event_lecture", "event_registration",
                "event_registration_draft", "notification", "conversation_log",
            )
        }
        return counts
    finally:
        conn.close()


if __name__ == "__main__":
    result = seed_complete()
    print("完整演示数据已生成：")
    for table, count in result.items():
        print("%-28s %s" % (table, count))
    print("\n登录账号：")
    print("管理员       admin / Admin@2026")
    print("领导         leader01 / Leader@2026")
    print("顾问          advisor01、advisor02 / Advisor@2026")
    print("班主任        teacher01、teacher02 / Teacher@2026")
    print("维护人员     maintainer01 / Maintainer@2026")
    print("学生         student01～student10、demo_student01～08 / Demo@2026")
