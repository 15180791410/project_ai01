# 导入数据库连接方法
import json
from datetime import datetime

from db import get_db
# 导入时间解析工具
from utils import time_parser

# ==========================================
# 通用工具：记录对话日志（需求里的“对话记录沉淀”）
# ==========================================
def log_conversation(user_id, agent_type, user_input, ai_output, emotion_tag=""):
    conn = get_db()
    try:
        # SQLite 的 CURRENT_TIMESTAMP 固定写 UTC，和业务使用的本地时间差 8 小时。
        # 新记录统一显式写本地时间，避免聊天记录与审批记录时间口径不一致。
        conn.execute(
            "INSERT INTO conversation_log "
            "(user_id, agent_type, user_input, ai_output, emotion_tag, create_time) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, agent_type, user_input, ai_output, emotion_tag or "",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
    finally:
        conn.close()

# ==========================================
# 1. 请假处理逻辑
# ==========================================
def handle_leave(student_id, reason, start_time="", end_time=""):
    # 让“语文课代表”把“明天/三天”解析成具体日期
    start, end, days, error = time_parser.parse_leave_time(reason, start_time, end_time)
    if error:
        raise ValueError(error)
    
    # 连数据库
    conn = get_db()
    try:
        # 同一学生的提交串行处理，避免并发双击在“查询是否重复”和“插入”
        # 之间同时通过检查。
        conn.execute("BEGIN IMMEDIATE")
        duplicate = conn.execute(
            "SELECT id,start_time,end_time,reason,status,create_time "
            "FROM student_admin_service "
            "WHERE student_id=? AND service_type='请假' AND status='待审批' "
            "AND start_time=? AND end_time=? AND reason=? "
            "ORDER BY id DESC LIMIT 1",
            (student_id, start, end, reason),
        ).fetchone()
        if duplicate:
            conn.rollback()
            return {
                "duplicate": True,
                "id": duplicate["id"],
                "开始": start,
                "结束": end,
                "天数": days,
                "状态": duplicate["status"],
            }
        overlap = conn.execute(
            "SELECT id,start_time,end_time,status FROM student_admin_service "
            "WHERE student_id=? AND service_type='请假' AND status='待审批' "
            "AND start_time<=? AND end_time>=? "
            "ORDER BY id DESC LIMIT 1",
            (student_id, end, start),
        ).fetchone()
        if overlap:
            conn.rollback()
            raise ValueError(
                "你在 " + str(overlap["start_time"]) + " 至 "
                + str(overlap["end_time"]) + " 已有待审批请假，不能重复申请重叠日期"
            )
        cursor = conn.execute(
            "INSERT INTO student_admin_service "
            "(student_id, service_type, start_time, end_time, reason, create_time) "
            "VALUES (?, '请假', ?, ?, ?, ?)",
            (student_id, start, end, reason,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        leave_id = cursor.lastrowid
    finally:
        conn.close()
    
    # 记录对话日志
    log_conversation(student_id, "student_agent", reason, f"已帮你请假，从{start}到{end}，共{days}天。")
    
    # 返回给前端看
    return {
        "id": leave_id,
        "student_id": student_id,
        "service_type": "请假",
        "reason": reason,
        "开始": start,
        "结束": end,
        "天数": days,
        "状态": "待审批",
    }

# ==========================================
# 2. 情绪处理与预警逻辑（统一走 emotion_logic）
# ==========================================
def handle_emotion(student_id, text, source="心情记录",
                   emotion_score=None, emotion_tag="", risk_level=""):
    from service import emotion_logic
    r = emotion_logic.record_emotion(
        student_id, text, source=source,
        provided_score=emotion_score,
        provided_tag=emotion_tag,
        provided_risk=risk_level,
    )
    log_conversation(student_id, "student_agent", text, r["tip"])
    return {
        "分数": r["score"],
        "标签": r["tag"],
        "风险等级": r["risk_level"],
        "提示": r["tip"],
        "已触发预警": r["alerted"],
    }


def _time_distance(left, right):
    """返回两个业务时间字符串相差的秒数；格式异常时返回一个大值。"""
    try:
        return abs((
            datetime.strptime(left, "%Y-%m-%d %H:%M:%S")
            - datetime.strptime(right, "%Y-%m-%d %H:%M:%S")
        ).total_seconds())
    except Exception:
        return 10 ** 9


def get_emotion_records(student_id):
    """返回学生本人在心情记录页可见的情绪历史和关怀反馈。

    历史正文来自 student_psych_profile；预警状态和老师备注来自
    student_psych_alert；每次提交后系统返回的提示从 conversation_log 补齐。
    所有查询都固定使用当前登录学生 ID，不能读取其他学生数据。
    """
    conn = get_db()
    try:
        profile = conn.execute(
            "SELECT latest_emotion_tag,emotion_score,emotion_history,"
            "last_interaction_time FROM student_psych_profile WHERE student_id=?",
            (student_id,),
        ).fetchone()
        alerts = [dict(row) for row in conn.execute(
            "SELECT id,trigger_reason,risk_level,emotion_score,status,"
            "handle_note,create_time FROM student_psych_alert "
            "WHERE student_id=? ORDER BY create_time DESC",
            (student_id,),
        ).fetchall()]
        notifications = [dict(row) for row in conn.execute(
            "SELECT id,title,content,is_read,create_time FROM notification "
            "WHERE user_id=? AND type='预警' ORDER BY create_time DESC",
            (student_id,),
        ).fetchall()]
        logs = [dict(row) for row in conn.execute(
            "SELECT user_input,ai_output,create_time FROM conversation_log "
            "WHERE user_id=? ORDER BY create_time DESC LIMIT 100",
            (student_id,),
        ).fetchall()]
    finally:
        conn.close()

    history = []
    if profile and profile["emotion_history"]:
        try:
            history = json.loads(profile["emotion_history"])
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []

    used_alerts = set()
    used_logs = set()
    enriched = []
    for entry in reversed(history):
        item = dict(entry)
        entry_time = str(item.get("time") or "")
        entry_text = str(item.get("text") or "")

        best_alert = None
        best_delta = 10 ** 9
        for index, alert in enumerate(alerts):
            if index in used_alerts or alert.get("trigger_reason") != entry_text:
                continue
            delta = _time_distance(entry_time, str(alert.get("create_time") or ""))
            if delta <= 300 and delta < best_delta:
                best_alert = alert
                best_delta = delta
        if best_alert is not None:
            used_alerts.add(alerts.index(best_alert))

        best_log = None
        best_log_delta = 10 ** 9
        for index, log in enumerate(logs):
            if index in used_logs or log.get("user_input") != entry_text:
                continue
            delta = _time_distance(entry_time, str(log.get("create_time") or ""))
            if delta <= 60 and delta < best_log_delta:
                best_log = log
                best_log_delta = delta
        if best_log is not None:
            used_logs.add(logs.index(best_log))

        alert_status = str(best_alert.get("status") or "") if best_alert else ""
        teacher_note = str(best_alert.get("handle_note") or "") if best_alert else ""
        system_response = str(best_log.get("ai_output") or "") if best_log else ""
        if not system_response:
            if alert_status == "已解决":
                system_response = "老师已查看并处理本次情绪记录"
            elif alert_status == "未处理":
                system_response = "已通知老师，等待老师进一步跟进"
            elif item.get("tag") == "高危":
                system_response = "本次记录已保存，如风险持续请立即联系老师或紧急支持人员"
            elif item.get("tag") == "关注":
                system_response = "已记录你的情绪；如需要，可以联系老师进一步沟通"
            else:
                system_response = "已记录"
        item["alert_status"] = alert_status
        item["teacher_note"] = teacher_note
        item["system_response"] = system_response
        # 学生端“心情与关怀记录”只展示主动心情记录，以及智能助手中
        # 被识别为关注/高危的倾诉；普通“查成绩”等业务对话不混进来。
        if item.get("source") == "心情记录" or item.get("tag") in ("关注", "高危"):
            enriched.append(item)

    return {
        "student_id": student_id,
        "latest_emotion_tag": profile["latest_emotion_tag"] if profile else "正常",
        "emotion_score": profile["emotion_score"] if profile else 20,
        "last_interaction_time": profile["last_interaction_time"] if profile else "",
        "history": enriched,
        "alerts": alerts,
        "notifications": notifications,
    }

# ==========================================
# 3. 查询我的请假记录
# ==========================================
def get_my_leaves(student_id, status=None):
    conn = get_db()
    sql = "SELECT * FROM student_admin_service WHERE student_id = ?"
    args = [student_id]
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY create_time DESC"
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(row) for row in rows]

# ==========================================
# 4. 提交售后反馈
# ==========================================
def submit_feedback(student_id, content, detail=""):
    conn = get_db()
    try:
        cursor = conn.execute(
            "INSERT INTO student_feedback_ticket "
            "(student_id, content, detail, create_time) VALUES (?, ?, ?, ?)",
            (student_id, content, detail.strip() or "暂无详细描述",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        record = {
            "id": cursor.lastrowid,
            "student_id": student_id,
            "content": content,
            "detail": detail.strip() or "暂无详细描述",
            "status": "待处理",
            "solution": "",
        }
    finally:
        conn.close()
    return record

# ==========================================
# 5. 查询我的售后工单 【之前漏掉的，已补全】
# ==========================================
def get_my_feedbacks(student_id, status=None):
    conn = get_db()
    try:
        sql = "SELECT * FROM student_feedback_ticket WHERE student_id = ?"
        args = [student_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        else:
            # 学生主动撤回的工单保留在数据库供员工审计，但不再出现在
            # 学生“我的工单”列表里，效果等同于学生侧删除。
            sql += " AND COALESCE(status,'') <> '已撤销'"
        # 时间字段只精确到秒，同一秒可能创建多张工单。追加主键倒序，
        # 保证“最新工单”结果稳定，也避免测试或页面随机拿到旧工单。
        sql += " ORDER BY create_time DESC, id DESC"
        rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def cancel_feedback(student_id, ticket_id):
    """学生撤回自己的待处理售后工单。

    不执行物理 DELETE：工单可能已被员工查看，直接删除会丢失审计记录。
    改为标记“已撤销”，学生列表不再展示，员工侧仍可追溯。
    """
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, status FROM student_feedback_ticket "
            "WHERE id=? AND student_id=?",
            (ticket_id, student_id),
        ).fetchone()
        if not row:
            return "not_found", None
        if row["status"] != "待处理":
            return "not_pending", row["status"]
        cursor = conn.execute(
            "UPDATE student_feedback_ticket "
            "SET status='已撤销', solution='学生主动撤回' "
            "WHERE id=? AND student_id=? AND status='待处理'",
            (ticket_id, student_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return "handled", None
        conn.commit()
        return "success", dict(row)
    finally:
        conn.close()

# ==========================================
# 6. 查询我的成绩 【之前漏掉的，已补全】
# ==========================================
def get_my_scores(student_id):
    conn = get_db()
    # 去成绩表里查这个学生的所有科目分数
    rows = conn.execute("SELECT * FROM student_score WHERE student_id = ?", (student_id,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]
