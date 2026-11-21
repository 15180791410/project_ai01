"""清空数据库并生成班主任端演示数据。

执行：
    python seed_homeroom_demo.py --reset

安全规则：
1. 删除数据库前先用 sqlite3.backup() 生成一致性快照；
2. 只允许操作 backend/yuejiao.db、yuejiao.db-wal、yuejiao.db-shm；
3. 密码只保存 PBKDF2 哈希，终端打印的密码仅用于本地演示登录。
"""
import argparse
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from db import db_file, init_db
from service import knowledge_logic

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
DEMO_PASSWORD = "Demo@2026"
BACKEND_DIR = Path(__file__).resolve().parent
DB_PATH = Path(db_file)


def hash_password(password):
    """与正式登录代码一致：随机盐 + PBKDF2-SHA256。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return "pbkdf2_sha256$120000$%s$%s" % (salt.hex(), digest.hex())


def backup_database():
    """在删除数据库前保留完整一致快照。"""
    db_path = Path(DB_PATH)
    if not db_path.exists():
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    target = backup_dir / ("before_homeroom_demo_%s.db" % STAMP)
    source = sqlite3.connect(str(db_path))
    dest = sqlite3.connect(str(target))
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    return target


def remove_database():
    """只删除明确的三个 SQLite 运行文件，不使用目录级删除。"""
    backend = Path(BACKEND_DIR).resolve()
    for name in ("yuejiao.db", "yuejiao.db-wal", "yuejiao.db-shm"):
        path = (backend / name).resolve()
        if path.parent != backend:
            raise RuntimeError("拒绝删除工作目录外文件: " + str(path))
        if path.exists():
            path.unlink()


def insert_user(conn, username, name, user_type, staff_role="", department="",
                advisor_id=None, homeroom_id=None, lifecycle="CONSULT",
                contact="", manager_id=None, password=DEMO_PASSWORD):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.execute(
        "INSERT INTO sys_user "
        "(username,real_name,user_type,contact_info,status,password_hash,"
        "target_country,lifecycle_stage,token_version,staff_role,department,"
        "manager_id,advisor_id,homeroom_id,security_question,security_answer_hash) "
        "VALUES (?,?,?,?, '正常', ?, '', ?, 0, ?, ?, ?, ?, ?, ?, ?)",
        (username, name, user_type, contact, hash_password(password), lifecycle,
         staff_role, department, manager_id, advisor_id, homeroom_id,
         "你的出生城市是？", hash_password("广州")),
    )
    return cur.lastrowid


def seed():
    """重建后写入一套可完整操作的班主任演示数据。"""
    init_db()
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")
    now = datetime.now()
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        admin_id = insert_user(
            conn, "admin", "系统管理员", "ADMIN", "LEADER",
            "信息技术部", password="Admin@2026",
        )
        teacher_id = insert_user(
            conn, "teacher01", "林班主任", "EMPLOYEE", "HOMEROOM",
            "国际教育部", manager_id=admin_id, password="Teacher@2026",
        )
        advisor_id = insert_user(
            conn, "advisor01", "陈顾问", "EMPLOYEE", "ADVISOR",
            "国际教育部", manager_id=admin_id, password="Advisor@2026",
        )
        leader_id = insert_user(
            conn, "leader01", "周主管", "EMPLOYEE", "LEADER",
            "国际教育部", manager_id=admin_id, password="Leader@2026",
        )

        students = [
            ("student01", "林子涵", "德国", "中德精英人才共建计划(双元制)"),
            ("student02", "陈雨桐", "新加坡", "新加坡国际本硕升学计划"),
            ("student03", "周浩然", "德国", "中德精英人才共建计划(双元制)"),
            ("student04", "许安娜", "新加坡", "新加坡国际本硕升学计划"),
            ("student05", "李思远", "德国", "中德精英人才共建计划(双元制)"),
            ("student06", "王嘉怡", "新加坡", "新加坡国际本硕升学计划"),
            ("student07", "张子墨", "新加坡", "新加坡国际本硕升学计划"),
            ("student08", "刘欣妍", "德国", "中德精英人才共建计划(双元制)"),
            ("student09", "赵一鸣", "新加坡", "新加坡国际本硕升学计划"),
            ("student10", "孙若溪", "新加坡", "新加坡国际本硕升学计划"),
        ]
        student_ids = []
        for index, (username, name, country, program) in enumerate(students, 1):
            sid = insert_user(
                conn, username, name, "STUDENT",
                advisor_id=advisor_id, homeroom_id=teacher_id,
                lifecycle="ENROLLED", contact="1380000%04d" % index,
            )
            conn.execute("UPDATE sys_user SET target_country=? WHERE id=?", (country, sid))
            student_ids.append((sid, name, country, program))

        programs = {
            item["project_name"]: (item["project_name"], item["country"], item["category"])
            for item in knowledge_logic.program_catalog()["programs"]
        }
        app_ids = []
        for index, (sid, name, country, preferred_program) in enumerate(student_ids):
            chosen = preferred_program if preferred_program in programs else "中德精英人才共建计划(双元制)"
            if programs[chosen][1] != country:
                chosen = next((p for p, item in programs.items() if item[1] == country), chosen)
            category = programs[chosen][2]
            # 演示学生均已入学，因此申请流程必须已经走完到“已入学”。
            stage = "已入学"
            status = "已完成"
            app_id = conn.execute(
                "INSERT INTO application_progress "
                "(student_id,country,university,program,stage,stage_detail,status,operator_id,"
                "initiator_type,application_mode,student_remark,create_time,update_time) "
                "VALUES (?,?,?,?,?,?,?,?,'STAFF',?,?,?,?)",
                (sid, country, "", chosen, stage, "班主任演示申请档案", status,
                 advisor_id, category, "由顾问建立并持续跟进",
                 (now - timedelta(days=15 - index)).strftime("%Y-%m-%d %H:%M:%S"),
                 now_text),
            ).lastrowid
            app_ids.append(app_id)
            conn.execute(
                "INSERT INTO application_history "
                "(application_id,stage,note,operator_id,create_time) VALUES (?,?,?,?,?)",
                (app_id, stage, "进入" + stage, advisor_id, now_text),
            )

        leave_status = ["待审批", "待审批", "待审批", "待审批",
                        "已通过", "已通过", "已通过", "已驳回", "已驳回", "已驳回"]
        for index, (sid, name, country, program) in enumerate(student_ids):
            status = leave_status[index]
            start = (now + timedelta(days=2 + index)).strftime("%Y-%m-%d")
            end = (now + timedelta(days=3 + index)).strftime("%Y-%m-%d")
            approver = teacher_id if status != "待审批" else None
            comment = "注意休息，保持联系" if status == "已通过" else (
                "请补充医院证明" if status == "已驳回" else ""
            )
            conn.execute(
                "INSERT INTO student_admin_service "
                "(student_id,service_type,start_time,end_time,reason,status,"
                "approver_id,approver_comment,approve_time,create_time) "
                "VALUES (?, '请假', ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, start, end, ["身体不适需休息", "家庭事务", "参加升学面试",
                                   "办理证件", "校园活动", "医院复查",
                                   "课程调整", "个人事务", "交通延误", "家庭安排"][index],
                 status, approver, comment,
                 now_text if status != "待审批" else None, now_text),
            )

        for index, (sid, name, country, program) in enumerate(student_ids):
            score1 = 82 + index
            score2 = 76 + index
            conn.execute(
                "INSERT INTO student_score (student_id,course_name,score,semester) VALUES (?,?,?,?)",
                (sid, "学术英语", score1, "2026秋季"),
            )
            conn.execute(
                "INSERT INTO student_score (student_id,course_name,score,semester) VALUES (?,?,?,?)",
                (sid, "海外学习适应", score2, "2026秋季"),
            )
            history = [
                {"time": now_text, "source": "班主任导入", "tag": "正常",
                 "score": 20, "text": "学习状态稳定"},
            ]
            conn.execute(
                "INSERT INTO student_psych_profile "
                "(student_id,latest_emotion_tag,emotion_score,emotion_history,last_interaction_time) "
                "VALUES (?,?,?,?,?)",
                (sid, "正常" if index >= 3 else "关注", 20 if index >= 3 else 58,
                 json.dumps(history, ensure_ascii=False), now_text),
            )

        for index in (1, 4, 8):
            sid = student_ids[index][0]
            conn.execute(
                "INSERT INTO student_psych_alert "
                "(student_id,trigger_reason,risk_level,emotion_score,status,create_time) "
                "VALUES (?,?,?,?,?,?)",
                (sid, "近期学习压力较大，需要班主任关注", "中", 58, "未处理", now_text),
            )

        ticket_data = [
            (student_ids[0][0], "课程资料未收到", "已解决", "已补发资料", 1),
            (student_ids[3][0], "活动报名信息有误", "待处理", "", 0),
            (student_ids[6][0], "希望调整住宿安排", "待处理", "", 0),
        ]
        for sid, content, status, solution, notified in ticket_data:
            conn.execute(
                "INSERT INTO student_feedback_ticket "
                "(student_id,content,detail,solution,status,is_notified,create_time) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, content, content, solution, status, notified, now_text),
            )

        # 不再自动生成活动报名。活动由负责内容/运营的人员在后台按真实日程
        # 创建后，客户或学生再自行报名；旧版自动指向 1/2/3 号活动的演示数据
        # 已属于无知识库来源内容。

        for index, (sid, name, country, program) in enumerate(student_ids[:6], 1):
            conn.execute(
                "INSERT INTO crm_lead "
                "(customer_name,contact_info,background_info,follow_up_history,status,"
                "owner_employee_id,target_country,create_time,update_time) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("咨询客户%02d" % index, "1390000%04d" % index,
                 "意向国家：" + country, now_text + " 首次电话沟通",
                 ["新增意向", "跟进中", "跟进中", "已签约", "新增意向", "已流失"][index - 1],
                 advisor_id, country, now_text, now_text),
            )

        for index, (report_date, content) in enumerate([
            ((now - timedelta(days=2)).strftime("%Y-%m-%d"), "完成德国项目学生材料检查"),
            ((now - timedelta(days=1)).strftime("%Y-%m-%d"), "跟进新加坡项目家长咨询"),
            (now.strftime("%Y-%m-%d"), "处理班级请假与心理关怀事项"),
        ], 1):
            conn.execute(
                "INSERT INTO employee_daily_report "
                "(employee_id,report_date,content,create_time) VALUES (?,?,?,?)",
                (teacher_id if index != 2 else advisor_id, report_date, content, now_text),
            )

        for index, (sid, name, country, program) in enumerate(student_ids):
            conn.execute(
                "INSERT INTO notification "
                "(user_id,type,title,content,is_read,link_page,create_time) "
                "VALUES (?,?,?,?,0,?,?)",
                (teacher_id, "系统", "班主任演示数据",
                 f"{name} 的申请与请假档案已建立", "overview", now_text),
            )

        conn.commit()
    finally:
        conn.close()

    return {
        "students": 10,
        "teacher": "teacher01 / Teacher@2026",
        "advisor": "advisor01 / Advisor@2026",
        "leader": "leader01 / Leader@2026",
        "admin": "admin / Admin@2026",
        "student_examples": "student01 ... student10 / Demo@2026",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="先清空并重新建表")
    args = parser.parse_args()
    backup = backup_database() if args.reset else None
    if args.reset:
        remove_database()
    result = seed()
    print("备份文件:", backup or "无需备份")
    for key, value in result.items():
        print("%-16s %s" % (key, value))
    print("班主任端可用学生数量: 10")


if __name__ == "__main__":
    main()
