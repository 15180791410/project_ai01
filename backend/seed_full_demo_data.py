"""幂等补齐全模块演示数据。

执行：
    python seed_full_demo_data.py

脚本不会清空数据库，也不会修改 student01～student10 的现有记录。
新增账号统一使用 demo_student01～demo_student08 前缀，密码 Demo@2026。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta

from db import db_file, ensure_migrations
from service import lifecycle
from service import knowledge_logic


DEMO_PASSWORD = "Demo@2026"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return "pbkdf2_sha256$120000$%s$%s" % (salt.hex(), digest.hex())


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_demo_student(conn, staff_ids, item):
    username, name, age, country, stage, contact = item
    row = conn.execute(
        "SELECT id FROM sys_user WHERE username=?", (username,)
    ).fetchone()
    values = (
        name, "STUDENT", contact, stage, country,
        staff_ids["advisor"], staff_ids["homeroom"],
    )
    if row:
        student_id = row["id"]
        conn.execute(
            "UPDATE sys_user SET real_name=?, user_type=?, contact_info=?, "
            "lifecycle_stage=?, target_country=?, advisor_id=?, homeroom_id=? "
            "WHERE id=?",
            values + (student_id,),
        )
        return student_id
    cursor = conn.execute(
        "INSERT INTO sys_user "
        "(username,real_name,user_type,contact_info,status,password_hash,"
        "target_country,lifecycle_stage,token_version,advisor_id,homeroom_id,"
        "security_question,security_answer_hash) "
        "VALUES (?,?,?,?, '正常', ?, ?, ?, 0, ?, ?, '你的出生城市是？', ?)",
        (
            username, name, "STUDENT", contact, hash_password(DEMO_PASSWORD),
            country, stage, staff_ids["advisor"], staff_ids["homeroom"],
            hash_password("广州"),
        ),
    )
    return cursor.lastrowid


def ensure_application(conn, student_id, country, stage, status, initiator, operator):
    project = next((
        item for item in knowledge_logic.program_catalog()["programs"]
        if item.get("country") == country
    ), None)
    if not project:
        return None
    app = conn.execute(
        "SELECT id FROM application_progress WHERE student_id=? "
        "ORDER BY id DESC LIMIT 1",
        (student_id,),
    ).fetchone()
    stamp = now_text()
    if app:
        app_id = app["id"]
        conn.execute(
            "UPDATE application_progress SET country=?, program=?, stage=?, status=?, "
            "stage_detail=?, operator_id=?, update_time=? WHERE id=?",
            (country, project["project_name"], stage, status,
             "演示申请流程", operator, stamp, app_id),
        )
    else:
        app_id = conn.execute(
            "INSERT INTO application_progress "
            "(student_id,country,university,program,stage,stage_detail,status,"
            "operator_id,initiator_type,application_mode,student_remark,"
            "create_time,update_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (student_id, country, "", project["project_name"], stage,
             "演示申请流程", status, operator, initiator, project["category"],
             "用于全模块预览的教学演示数据", stamp, stamp),
        ).lastrowid
    stage_index = (
        lifecycle.APPLICATION_STAGES.index(stage)
        if stage in lifecycle.APPLICATION_STAGES else 0
    )
    lifecycle.ensure_application_history(
        conn, app_id, stage_index, operator, "演示数据：" + stage + "阶段"
    )
    return app_id


def ensure_score(conn, student_id, course, score, semester="2026秋"):
    exists = conn.execute(
        "SELECT id FROM student_score WHERE student_id=? AND course_name=? AND semester=?",
        (student_id, course, semester),
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO student_score (student_id,course_name,score,semester) "
            "VALUES (?,?,?,?)",
            (student_id, course, score, semester),
        )


def ensure_alert(conn, student_id, reason, risk, score, status, note=""):
    exists = conn.execute(
        "SELECT id FROM student_psych_alert WHERE student_id=? AND trigger_reason=?",
        (student_id, reason),
    ).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO student_psych_alert "
            "(student_id,trigger_reason,risk_level,emotion_score,status,"
            "handler_id,handle_note,create_time) VALUES (?,?,?,?,?,?,?,?)",
            (student_id, reason, risk, score, status,
             None if status == "未处理" else 2,
             note, now_text()),
        )


def seed():
    ensure_migrations()
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    summary = {}
    try:
        users = {
            row["staff_role"] if row["user_type"] == "EMPLOYEE" else row["user_type"]: row["id"]
            for row in conn.execute(
                "SELECT id,user_type,staff_role FROM sys_user WHERE status='正常'"
            ).fetchall()
        }
        if "ADVISOR" not in users or "HOMEROOM" not in users:
            raise RuntimeError("缺少 advisor01 或 teacher01，请先初始化员工账号")
        staff_ids = {"advisor": users["ADVISOR"], "homeroom": users["HOMEROOM"]}
        admin_id = users.get("ADMIN", staff_ids["advisor"])
        leader_id = users.get("LEADER", admin_id)

        students = [
            ("demo_student01", "王思琪", 18, "新加坡", "CONSULT", "13600000001"),
            ("demo_student02", "李嘉豪", 17, "德国", "CONSULT", "13600000002"),
            ("demo_student03", "陈悦", 19, "新加坡", "APPLYING", "13600000003"),
            ("demo_student04", "周子轩", 20, "德国", "APPLYING", "13600000004"),
            ("demo_student05", "林晓彤", 19, "新加坡", "ENROLLED", "13600000005"),
            ("demo_student06", "张家豪", 21, "德国", "ENROLLED", "13600000006"),
            ("demo_student07", "吴雨晴", 22, "新加坡", "ALUMNI", "13600000007"),
            ("demo_student08", "黄俊杰", 23, "德国", "ALUMNI", "13600000008"),
        ]
        ids = {}
        for item in students:
            ids[item[0]] = ensure_demo_student(conn, staff_ids, item)

        # 申请阶段覆盖：咨询期无申请，申请期两步，在读/毕业完整八阶段。
        ensure_application(conn, ids["demo_student03"], "新加坡", "材料准备",
                           "进行中", "STUDENT", staff_ids["advisor"])
        ensure_application(conn, ids["demo_student04"], "德国", "面试/补充",
                           "进行中", "STAFF", staff_ids["advisor"])
        for username, country in (
            ("demo_student05", "新加坡"),
            ("demo_student06", "德国"),
            ("demo_student07", "新加坡"),
            ("demo_student08", "德国"),
        ):
            ensure_application(conn, ids[username], country, "已入学",
                               "已完成", "STAFF", staff_ids["advisor"])

        # 不同状态的请假。
        leave_data = [
            ("demo_student05", "2026-11-25", "2026-11-25", "身体不适需要休息", "待审批", None, ""),
            ("demo_student04", "2026-11-26", "2026-11-27", "参加院校线上面试", "待审批", None, ""),
            ("demo_student06", "2026-11-20", "2026-11-22", "家庭事务", "已通过", staff_ids["homeroom"], "注意安全，保持联系"),
            ("demo_student07", "2026-11-10", "2026-11-11", "毕业材料办理", "已驳回", staff_ids["homeroom"], "请补充证明材料"),
        ]
        for sid_name, start, end, reason, status, approver, comment in leave_data:
            sid = ids[sid_name]
            if not conn.execute(
                "SELECT id FROM student_admin_service WHERE student_id=? AND reason=?",
                (sid, reason),
            ).fetchone():
                conn.execute(
                    "INSERT INTO student_admin_service "
                    "(student_id,service_type,start_time,end_time,reason,status,"
                    "approver_id,approver_comment,approve_time,create_time) "
                    "VALUES (?, '请假', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sid, start, end, reason, status, approver, comment,
                     now_text() if approver else None, now_text()),
                )

        for username, courses in (
            ("demo_student05", [("学术英语", 86), ("新加坡文化", 90)]),
            ("demo_student06", [("德语基础", 78), ("职业实践", 84)]),
            ("demo_student07", [("毕业项目", 92), ("国际商务", 88)]),
            ("demo_student08", [("机械实训", 81), ("德语B2", 76)]),
        ):
            for course, score in courses:
                ensure_score(conn, ids[username], course, score)

        profile_data = [
            ("demo_student01", "关注", 45, "第一次咨询后对费用有些焦虑"),
            ("demo_student02", "高危", 75, "申请方向不明确，连续失眠"),
            ("demo_student03", "关注", 58, "担心材料审核时间"),
            ("demo_student04", "高危", 92, "面试受挫后情绪激动"),
            ("demo_student05", "高危", 100, "出现极端自伤表达"),
            ("demo_student06", "关注", 55, "学习节奏需要调整"),
            ("demo_student07", "正常", 20, "毕业阶段状态稳定"),
            ("demo_student08", "高危", 90, "已由班主任完成危机干预"),
        ]
        for username, tag, score, text in profile_data:
            sid = ids[username]
            history = [{"time": now_text(), "source": "全模块演示数据",
                        "tag": tag, "score": score, "text": text}]
            conn.execute(
                "INSERT INTO student_psych_profile "
                "(student_id,latest_emotion_tag,emotion_score,emotion_history,last_interaction_time) "
                "VALUES (?,?,?,?,?) ON CONFLICT(student_id) DO UPDATE SET "
                "latest_emotion_tag=excluded.latest_emotion_tag,"
                "emotion_score=excluded.emotion_score,"
                "emotion_history=excluded.emotion_history,"
                "last_interaction_time=excluded.last_interaction_time",
                (sid, tag, score, json.dumps(history, ensure_ascii=False), now_text()),
            )

        ensure_alert(conn, ids["demo_student01"], "第一次咨询后担心家庭费用压力",
                     "中", 45, "未处理")
        ensure_alert(conn, ids["demo_student02"], "最近持续失眠，压力很大",
                     "高", 75, "未处理")
        ensure_alert(conn, ids["demo_student03"], "担心材料审核时间",
                     "中", 58, "已解决", "已解释审核周期")
        ensure_alert(conn, ids["demo_student04"], "面试失败后想报复同学",
                     "高", 92, "未处理")
        ensure_alert(conn, ids["demo_student05"], "我不想活了",
                     "高", 100, "未处理")
        ensure_alert(conn, ids["demo_student06"], "学习节奏跟不上，焦虑",
                     "中", 55, "已解决", "已制定学习计划")
        ensure_alert(conn, ids["demo_student08"], "长期压力下有自伤念头",
                     "高", 90, "已解决", "已完成危机干预并持续跟进")

        feedback_data = [
            ("demo_student05", "课程资料未收到", "需要补发课程资料", "待处理", "", 0),
            ("demo_student06", "宿舍网络不稳定", "希望协助联系住宿方", "处理中", "", 0),
            ("demo_student07", "毕业证明咨询", "已说明办理流程", "已解决", "已发送办理指引", 1),
        ]
        for username, content, detail, status, solution, notified in feedback_data:
            sid = ids[username]
            if not conn.execute(
                "SELECT id FROM student_feedback_ticket WHERE student_id=? AND content=?",
                (sid, content),
            ).fetchone():
                conn.execute(
                    "INSERT INTO student_feedback_ticket "
                    "(student_id,content,detail,solution,status,is_notified,create_time) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (sid, content, detail, solution, status, notified, now_text()),
                )

        # 活动报名覆盖三场已发布活动。
        event_map = {
            row["event_name"]: row["id"]
            for row in conn.execute("SELECT id,event_name FROM event_lecture").fetchall()
        }
        for event_name, username in (
            ("新加坡招生官见面会", "demo_student01"),
            ("德国双元制宣讲会", "demo_student02"),
            ("2026秋季多国名校联展", "demo_student03"),
            ("新加坡招生官见面会", "demo_student05"),
        ):
            event_id = event_map.get(event_name)
            sid = ids[username]
            if not event_id or conn.execute(
                "SELECT id FROM event_registration WHERE event_id=? AND customer_id=?",
                (event_id, sid),
            ).fetchone():
                continue
            conn.execute(
                "INSERT INTO event_registration "
                "(event_id,customer_id,status,contact_info,target_country,create_time) "
                "VALUES (?,?,'已报名',?,?,?)",
                (event_id, sid, "136000000%02d" % int(username[-2:]),
                 "新加坡" if username in ("demo_student01", "demo_student05") else "德国",
                 now_text()),
            )
            conn.execute(
                "UPDATE event_lecture SET current_participants="
                "COALESCE(current_participants,0)+1 WHERE id=?",
                (event_id,),
            )

        crm_data = [
            ("赵女士", "13910000001", "孩子高二，意向新加坡本科", "新增意向"),
            ("陈先生", "13910000002", "本科毕业，关注德国双元制", "跟进中"),
            ("李女士", "13910000003", "初三家庭，关注新加坡2+2", "已签约"),
            ("周先生", "13910000004", "预算有限，咨询就业项目", "已流失"),
            ("吴女士", "13910000005", "高中毕业，目标快速本科", "跟进中"),
            ("黄先生", "13910000006", "德语B1，计划德国培训", "新增意向"),
        ]
        for name, contact, background, status in crm_data:
            if not conn.execute(
                "SELECT id FROM crm_lead WHERE contact_info=?", (contact,)
            ).fetchone():
                conn.execute(
                    "INSERT INTO crm_lead "
                    "(customer_name,contact_info,background_info,follow_up_history,status,"
                    "owner_employee_id,target_country,create_time,update_time) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (name, contact, background, now_text() + " 首次沟通",
                     status, staff_ids["advisor"],
                     "德国" if "德国" in background else "新加坡",
                     now_text(), now_text()),
                )

        for employee_id, content in (
            (staff_ids["homeroom"], "完成在读学生请假审批与心理预警跟进"),
            (staff_ids["advisor"], "跟进新加坡和德国项目意向客户"),
            (leader_id, "检查本周申请进度与团队线索质量"),
        ):
            if not conn.execute(
                "SELECT id FROM employee_daily_report WHERE employee_id=? AND content=?",
                (employee_id, content),
            ).fetchone():
                conn.execute(
                    "INSERT INTO employee_daily_report "
                    "(employee_id,report_date,content,create_time) VALUES (?,?,?,?)",
                    (employee_id, datetime.now().strftime("%Y-%m-%d"),
                     content, now_text()),
                )

        for username, title, content in (
            ("demo_student05", "请假申请待审批", "你的请假申请正在等待班主任审批"),
            ("demo_student06", "成绩已更新", "本学期职业实践成绩已录入"),
            ("demo_student07", "毕业服务提醒", "如需毕业证明请联系班主任"),
        ):
            sid = ids[username]
            if not conn.execute(
                "SELECT id FROM notification WHERE user_id=? AND title=?",
                (sid, title),
            ).fetchone():
                conn.execute(
                    "INSERT INTO notification "
                    "(user_id,type,title,content,is_read,link_page,create_time) "
                    "VALUES (?, '系统', ?, ?, 0, 'overview', ?)",
                    (sid, title, content, now_text()),
                )

        for username in ("demo_student01", "demo_student03", "demo_student05"):
            sid = ids[username]
            text = "全模块预览对话记录：" + username
            if not conn.execute(
                "SELECT id FROM conversation_log WHERE user_id=? AND user_input=?",
                (sid, text),
            ).fetchone():
                conn.execute(
                    "INSERT INTO conversation_log "
                    "(user_id,agent_type,user_input,ai_output,emotion_tag,create_time) "
                    "VALUES (?, 'student_agent', ?, ?, '关注', ?)",
                    (sid, text, "已记录本次演示对话", now_text()),
                )

        # 对齐活动报名人数，防止手工补数据后计数不一致。
        conn.execute(
            "UPDATE event_lecture SET current_participants=("
            "SELECT COUNT(*) FROM event_registration r WHERE r.event_id=event_lecture.id)"
        )
        conn.commit()
        summary = {
            username: {"id": ids[username], "stage": item[4], "country": item[3]}
            for username, item in zip(ids.keys(), students)
            if username
        }
    finally:
        conn.close()
    return summary


if __name__ == "__main__":
    result = seed()
    print("已补齐全模块演示数据：")
    for username, info in result.items():
        print("%-16s id=%-4s %-8s %s" % (
            username, info["id"], info["stage"], info["country"]
        ))
    print("演示学生密码：Demo@2026")

