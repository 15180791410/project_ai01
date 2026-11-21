import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
db_file = os.getenv("YUEJIAO_DB_FILE", str(BASE_DIR / "yuejiao.db"))
sql_file = os.getenv("YUEJIAO_SCHEMA_FILE", str(BASE_DIR / "schema.sql"))


def get_db():
    conn = sqlite3.connect(db_file, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# ------------------------------------------------------------------
# 增量迁移：老数据库缺列时自动补齐，避免"跑了很久的表结构"报错。
# 每条都是幂等的：先查 PRAGMA table_info，不存在才 ALTER。
# ------------------------------------------------------------------
MIGRATIONS = {
    "sys_user": [
        ("target_country", "TEXT"),              # 意向留学国家
        ("lifecycle_stage", "TEXT DEFAULT 'CONSULT'"),  # 学籍阶段：咨询期/申请期/在读/已毕业
        ("token_version", "INTEGER DEFAULT 0"),  # 改密/撤销旧 token
        ("staff_role", "TEXT"),
        ("department", "TEXT"),
        ("manager_id", "INTEGER"),
        ("advisor_id", "INTEGER"),
        ("homeroom_id", "INTEGER"),
        ("security_question", "TEXT"),
        ("security_answer_hash", "TEXT"),
    ],
    "student_admin_service": [
        ("approver_comment", "TEXT"),            # 审批意见 / 驳回理由
        ("approve_time", "TEXT"),                # 审批时间
    ],
    "student_psych_alert": [
        ("handler_id", "INTEGER"),               # 处理人
        ("handle_note", "TEXT"),                 # 处理备注
        ("emotion_score", "INTEGER"),            # 触发预警时的情绪分值
    ],
    "event_lecture": [
        ("event_type", "TEXT"),
        ("description", "TEXT"),                 # 活动简介
        ("country", "TEXT"),                     # 关联国家
        ("location", "TEXT"),                    # 地点
        ("audience_scope", "TEXT DEFAULT 'PUBLIC'"),  # 活动可见对象
        ("create_time", "TEXT"),                 # 创建时间
    ],
    "crm_lead": [
        ("background_info", "TEXT"),             # 客户背景（官网留资带入）
        ("target_country", "TEXT"),              # 意向国家
        ("follow_up_history", "TEXT"),           # 跟进历史（JSON 文本）
        ("source_event_id", "INTEGER"),          # 首次获客活动，便于追溯线索来源
        ("update_time", "TEXT"),                 # 最近跟进时间
    ],
    "event_registration": [
        ("contact_info", "TEXT"),                # 报名联系方式
        ("target_country", "TEXT"),              # 意向国家
        ("lead_id", "INTEGER"),                   # 关联 CRM 线索，活动报名可直接进入跟进
    ],
    "student_psych_profile": [
        ("last_interaction_time", "TEXT"),       # 最近一次情绪交互时间
    ],
    "conversation_log": [
        ("emotion_tag", "TEXT"),                 # 本次对话的情绪标签
    ],
    "application_progress": [
        ("initiator_type", "TEXT DEFAULT 'STAFF'"),
        ("application_mode", "TEXT"),
        ("student_remark", "TEXT"),
    ],
    "application_history": [
        ("action", "TEXT"),
        ("source_type", "TEXT DEFAULT 'MANUAL'"),
        ("related_material_id", "INTEGER"),
        ("detail_json", "TEXT"),
    ],
    "leave_request_draft": [
        ("confirmation_token", "TEXT"),
        ("confirmation_expires_at", "TEXT"),
        ("request_version", "INTEGER DEFAULT 0"),
    ],
}


INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_sys_user_username ON sys_user(username)",
    "CREATE INDEX IF NOT EXISTS ix_sys_user_type_status ON sys_user(user_type, status)",
    "CREATE INDEX IF NOT EXISTS ix_student_leave_student_status ON student_admin_service(student_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_student_alert_student_status ON student_psych_alert(student_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_crm_lead_owner_status ON crm_lead(owner_employee_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_crm_lead_source_event ON crm_lead(source_event_id)",
    "CREATE INDEX IF NOT EXISTS ix_notification_user_read ON notification(user_id, is_read)",
    "CREATE INDEX IF NOT EXISTS ix_application_student ON application_progress(student_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_application_stage_record "
    "ON application_stage_record(application_id, stage)",
    "CREATE INDEX IF NOT EXISTS ix_application_material_app_stage "
    "ON application_material(application_id, stage, is_active)",
    "CREATE INDEX IF NOT EXISTS ix_application_history_app "
    "ON application_history(application_id, stage)",
    "CREATE INDEX IF NOT EXISTS ix_enrollment_expected_graduation ON student_enrollment(expected_graduation_date, status)",
    "CREATE INDEX IF NOT EXISTS ix_lifecycle_student_date ON student_lifecycle_event(student_id, effective_date)",
    "CREATE INDEX IF NOT EXISTS ix_graduation_student_status ON student_graduation_request(student_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_leave_draft_state ON leave_request_draft(state, update_time)",
    "CREATE INDEX IF NOT EXISTS ix_business_action_token "
    "ON business_action_draft(confirmation_token)",
    "CREATE INDEX IF NOT EXISTS ix_event_registration_event ON event_registration(event_id)",
    "CREATE INDEX IF NOT EXISTS ix_event_registration_lead ON event_registration(lead_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_event_registration_contact "
    "ON event_registration(event_id, contact_info) "
    "WHERE contact_info IS NOT NULL AND contact_info <> ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_event_registration_student "
    "ON event_registration(event_id, customer_id) WHERE customer_id > 0",
    # 防止并发双击或重复请求同时创建两条相同的有效就读申请。
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_application_active "
    "ON application_progress(student_id, country, program) "
    "WHERE status IN ('待受理','进行中')",
]


def ensure_migrations():
    """建新表 + 给已有数据库补列。可重复调用，幂等安全。

    先执行 schema.sql 里的 CREATE TABLE IF NOT EXISTS 建出新表（新表如申请进度、
    站内通知），再逐表补列。schema.sql 不含 INSERT，种子数据只在 init_db 里，
    因此这里执行不会造成重复数据。
    """
    conn = get_db()
    changed = []
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # 1) 建表：让运行中的老库也能自动获得新增的表
        try:
            with open(sql_file, "r", encoding="utf-8") as f:
                conn.executescript(f.read())
        except Exception as exc:
            # 建表失败必须终止，否则服务会在缺表状态下继续运行，错误更难定位。
            raise RuntimeError("执行 schema.sql 失败: " + str(exc)) from exc

        # 2) 补列
        existing_tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for table, cols in MIGRATIONS.items():
            if table not in existing_tables:
                continue
            have = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
            for col, typ in cols:
                if col not in have:
                    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
                    changed.append(table + "." + col)
        if changed:
            conn.commit()

        # 旧时间轴里的“系统补记”必须保留为历史回填标记，不能伪装成真实
        # 业务材料或真实办理结果。
        if "application_history" in existing_tables:
            conn.execute(
                "UPDATE application_history SET source_type='BACKFILL' "
                "WHERE COALESCE(source_type,'MANUAL')='MANUAL' "
                "AND (note LIKE '%系统按最终流程状态补记%' "
                " OR note LIKE '%历史%回填%' OR note LIKE '%系统补记%')"
            )
            conn.commit()

        # 3) 索引与唯一约束。放在补列之后，确保旧库也能升级。
        for statement in INDEXES:
            conn.execute(statement)
        conn.commit()

        # 知识库主数据只允许来自 Dify，后端不得保留副本表。
        # 历史申请记录仍保留业务流水中的项目名称，不受影响。
        for table in ("company_content", "course_project"):
            conn.execute("DROP TABLE IF EXISTS " + table)
        conn.commit()

        # 4) 学籍阶段回填（幂等）：老库里已有请假/成绩/售后记录的学生视为"在读"，
        #    否则加了门禁后这些老学生会突然被挡住。
        if "sys_user" in existing_tables:
            conn.execute(
                "UPDATE sys_user SET staff_role='ADVISOR' "
                "WHERE user_type IN ('EMPLOYEE','TEACHER') "
                "AND COALESCE(staff_role,'')=''"
            )
            try:
                conn.execute(
                    "UPDATE sys_user SET lifecycle_stage='ENROLLED' "
                    "WHERE user_type='STUDENT' "
                    "AND COALESCE(lifecycle_stage,'CONSULT')='CONSULT' "
                    "AND (id IN (SELECT student_id FROM student_admin_service) "
                    "  OR id IN (SELECT student_id FROM student_score) "
                    "  OR id IN (SELECT student_id FROM student_feedback_ticket))")
                conn.commit()
            except Exception:
                pass  # 业务表尚不存在时跳过，不影响启动
            try:
                # 旧预警没有分值列，按风险原文回填，保证后台可直接展示分数。
                conn.execute(
                    "UPDATE student_psych_alert SET emotion_score = CASE "
                    "WHEN risk_level='高' AND (trigger_reason LIKE '%干掉%' "
                    " OR trigger_reason LIKE '%干死%' "
                    " OR trigger_reason LIKE '%杀%' OR trigger_reason LIKE '%血洗%' "
                    " OR trigger_reason LIKE '%同归于尽%') THEN 100 "
                    "WHEN risk_level='高' THEN 92 "
                    "WHEN risk_level='中' THEN 58 ELSE 20 END "
                    "WHERE emotion_score IS NULL"
                )
                conn.commit()
            except Exception:
                pass
    finally:
        conn.close()
    return changed


def init_reference_data(conn=None):
    """保留启动钩子；不再写入任何知识库主数据。

    公司和留学方案必须通过 Dify Dataset API 实时读取。活动属于运营数据，
    由后台负责人维护，也不在启动时自动生成。
    """
    own = conn is None
    conn = conn or get_db()
    try:
        conn.commit()
    finally:
        if own:
            conn.close()


def init_db():
    # 1. 读建表代码
    with open(sql_file, "r", encoding="utf-8") as f:
        sql_text = f.read()

    conn = get_db()
    # 2. 建表
    conn.executescript(sql_text)

    init_reference_data(conn)
    conn.commit()
    conn.close()
    ensure_migrations()
    print("数据库初始化完成：业务表已就绪；知识库主数据由 Dify 实时提供。")


if __name__ == "__main__":
    init_db()
