"""
学生生命周期（学籍阶段）与能力门禁
====================================
一个学生从注册到入学，能用的服务是渐进解锁的：

    CONSULT  咨询期  刚注册，还没定方向 → 只能了解与报名
    APPLYING 申请期  已建档，材料在走     → 申请进度持续可查
    ENROLLED 在读    已入学              → 解锁请假、售后、成绩
    ALUMNI   已毕业  只读                → 保留查询，关闭请假与申请

设计原则：**可读的开放，可写的收紧**。
- 查询类接口一律不拦（没数据就是空列表，不打扰用户）
- 产生业务单据的提交类接口（请假、售后）必须到 ENROLLED
- 心情记录属于在读学生档案，咨询期/申请期禁止写入，避免把潜在客户数据
  混入学生心理画像；智能助手仍保留安全风险检测，但不会落心情历史。

阶段来源：
- 员工创建申请 → 自动升到 APPLYING
- 申请推进到「已入学」→ 自动升到 ENROLLED
- 员工后台可手动设置（含降级、标记毕业）
"""
import calendar
import json
import re
from datetime import date, datetime

from db import get_db

# 顺序即阶段推进顺序，不能随意调换
STAGES = ["CONSULT", "APPLYING", "ENROLLED", "ALUMNI"]

STAGE_LABEL = {
    "CONSULT": "咨询期",
    "APPLYING": "申请期",
    "ENROLLED": "在读",
    "ALUMNI": "已毕业",
}

STAGE_DESC = {
    "CONSULT": "刚注册，正在了解留学方案与院校，还没有启动申请",
    "APPLYING": "申请档案已建立，材料与院校正在推进",
    "ENROLLED": "已入学，可使用请假、成绩、售后等在校服务",
    "ALUMNI": "已完成学业，服务转为只读",
}

STAGE_INDEX = {s: i for i, s in enumerate(STAGES)}

# 留学申请内部的八个阶段。与学籍生命周期分开维护：
# 学籍表示学生状态，申请阶段表示申请材料实际走到哪一步。
APPLICATION_STAGES = [
    "选校定位",
    "材料准备",
    "递交申请",
    "面试/补充",
    "收到Offer",
    "办理签证",
    "行前准备",
    "已入学",
]

# 每个能力（对应前端模块）要求的最低阶段
MIN_STAGE = {
    "chat": "CONSULT",         # 智能助手：任何阶段都能问
    "programs": "CONSULT",     # 看留学方案
    "activities": "CONSULT",   # 报名活动 / 讲座
    "emotion": "ENROLLED",     # 心情记录：仅已入学学生可写入和查看
    "application": "CONSULT",  # 咨询期即可由学生主动提交就读申请
    "leave": "ENROLLED",       # 请假申请与记录
    "feedback": "ENROLLED",    # 售后反馈
    "scores": "ENROLLED",      # 成绩查询
}

ACTION_LABEL = {
    "chat": "智能助手",
    "programs": "留学方案",
    "activities": "活动报名",
    "emotion": "心情记录",
    "application": "申请进度",
    "leave": "请假",
    "feedback": "售后反馈",
    "scores": "成绩查询",
}

# 门禁提示语：说明缺什么、下一步该做什么，而不是干巴巴一句"无权限"
DENY_MSG = {
    "application": "「申请进度」当前不可用，请先完善账号状态或联系顾问。",
    "leave": "「请假」是在读学生的服务，你目前处于「{stage}」。入学后会自动解锁，在那之前可以让助手帮你规划入学准备。",
    "feedback": "「售后反馈」面向已入学的在读学生，你目前处于「{stage}」。有疑问可以先用智能助手或左上角的客服咨询。",
    "scores": "「成绩查询」会在你入学后自动开启，你目前处于「{stage}」。",
    "emotion": "「心情记录」面向已入学的学生，你目前处于「{stage}」。新注册账号请先完成申请和入学；如有紧急心理风险，仍可通过智能助手获得安全提醒和紧急求助信息。",
}

# 毕业后的只读约束
ALUMNI_BLOCKED = ("leave", "application", "feedback", "activities")

# 正常学生主流程只允许逐级前进。管理员纠错可以 force，但必须填写原因，
# 并会在生命周期流水中留下 ADMIN_CORRECTION 记录。
ALLOWED_TRANSITIONS = {
    "CONSULT": {"APPLYING"},
    "APPLYING": {"ENROLLED"},
    "ENROLLED": {"ALUMNI"},
    "ALUMNI": set(),
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_application_history(conn, application_id, final_index,
                               operator_id=None, final_note=""):
    """补齐申请时间轴中缺失的阶段记录。

    正常逐级推进时每个阶段都会单独留痕。如果管理员直接把学籍设为在读，
    或修复历史脏数据导致阶段跨越，则用本函数补记前置节点。补记内容明确
    写出“系统按最终状态补记”，不会伪装成当时的真实操作时间。
    """
    existing = {
        row["stage"]
        for row in conn.execute(
            "SELECT stage FROM application_history WHERE application_id=?",
            (application_id,),
        ).fetchall()
    }
    timestamp = _now()
    for index, stage in enumerate(APPLICATION_STAGES[:final_index + 1]):
        if stage in existing:
            continue
        if index == 0:
            note = "系统补记：申请档案已建立"
        elif index == final_index:
            note = final_note or "系统按最终状态补记：该阶段已完成"
        else:
            note = "系统按最终流程状态补记：该前置阶段已完成"
        source_type = "MANUAL" if index == final_index and final_note else "BACKFILL"
        conn.execute(
            "INSERT INTO application_history "
            "(application_id,stage,note,operator_id,action,source_type,create_time) "
            "VALUES (?,?,?,?,?,?,?)",
            (application_id, stage, note, operator_id,
             "推进阶段" if source_type == "MANUAL" else "历史回填",
             source_type, timestamp),
        )


def get_stage(user_id):
    """取学生当前阶段；查不到或非学生时按 CONSULT 处理。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT lifecycle_stage FROM sys_user WHERE id=?", (user_id,)).fetchone()
        if not row:
            return "CONSULT"
        stage = (row["lifecycle_stage"] or "").strip().upper()
        return stage if stage in STAGE_INDEX else "CONSULT"
    finally:
        conn.close()


def get_stage_info(user_id):
    """给前端的完整阶段信息：当前阶段、进度、各能力是否可用、下一步指引。"""
    stage = get_stage(user_id)
    idx = STAGE_INDEX[stage]
    caps = {}
    for action, need in MIN_STAGE.items():
        caps[action] = (idx >= STAGE_INDEX[need]) and not (stage == "ALUMNI" and action in ALUMNI_BLOCKED)
    conn = get_db()
    try:
        enrollment_row = conn.execute(
            "SELECT * FROM student_enrollment WHERE student_id=?", (user_id,)
        ).fetchone()
        enrollment = dict(enrollment_row) if enrollment_row else None
        events = [
            dict(row) for row in conn.execute(
                "SELECT * FROM student_lifecycle_event WHERE student_id=? "
                "ORDER BY COALESCE(NULLIF(effective_date,''),create_time), id",
                (user_id,),
            ).fetchall()
        ]
        graduation_requests = [
            dict(row) for row in conn.execute(
                "SELECT * FROM student_graduation_request WHERE student_id=? "
                "ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    if enrollment:
        start = _parse_date(enrollment.get("enrollment_date"))
        finish = _parse_date(enrollment.get("expected_graduation_date"))
        if start and finish and finish >= start:
            total_days = max(1, (finish - start).days)
            used_days = max(0, min(total_days, (date.today() - start).days))
            enrollment["study_progress_pct"] = round(used_days * 100 / total_days)
            enrollment["remaining_days"] = max(0, (finish - date.today()).days)
        else:
            enrollment["study_progress_pct"] = 0
            enrollment["remaining_days"] = None

    missing = []
    if stage == "ENROLLED":
        if not enrollment:
            missing.append("入学档案")
        else:
            if not enrollment.get("enrollment_date"):
                missing.append("入学日期")
            if not enrollment.get("standard_duration_months"):
                missing.append("标准学制")
            if not enrollment.get("expected_graduation_date"):
                missing.append("预计毕业日期")
    if stage == "ALUMNI":
        if not enrollment or not enrollment.get("actual_graduation_date"):
            missing.append("实际毕业日期")
        if not any(item.get("status") == "已通过" for item in graduation_requests):
            missing.append("毕业审核记录")

    return {
        "stage": stage,
        "stage_label": STAGE_LABEL[stage],
        "stage_index": idx,
        "stage_desc": STAGE_DESC[stage],
        "capabilities": caps,
        "next_step": _next_step(stage),
        "timeline": [
            {"key": s, "label": STAGE_LABEL[s], "desc": STAGE_DESC[s],
             "done": i < idx, "current": i == idx}
            for i, s in enumerate(STAGES)
        ],
        "enrollment": enrollment,
        "lifecycle_events": events,
        "graduation_requests": graduation_requests,
        "completeness": {
            "complete": not missing,
            "missing": missing,
        },
    }


def _next_step(stage):
    return {
        "CONSULT": "先和智能助手聊清目标国家与专业，再从官网或活动页报名一场宣讲会。",
        "APPLYING": "在「申请进度」里跟进材料清单，缺什么尽快补齐。",
        "ENROLLED": "已解锁全部在校服务，请假、成绩、售后都可以直接用。",
        "ALUMNI": "服务已转为只读，如需开具证明请联系顾问。",
    }.get(stage, "")


def check(user_id, action):
    """门禁判定，返回 (是否允许, 提示语)。"""
    need = MIN_STAGE.get(action)
    if not need:
        return True, ""
    stage = get_stage(user_id)
    if stage == "ALUMNI" and action in ALUMNI_BLOCKED:
        return False, "你已毕业，该服务已关闭，如需帮助请联系顾问。"
    if STAGE_INDEX[stage] >= STAGE_INDEX[need]:
        return True, ""
    msg = DENY_MSG.get(action, "该功能需要{need}后使用。").format(stage=STAGE_LABEL[stage])
    return False, msg


def _parse_date(value):
    """只接受 YYYY-MM-DD；无效或空值返回 None，不伪造日期。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _add_months(base, months):
    """按自然月计算预计毕业日期，月末日期自动取目标月最后一天。"""
    if not base or not months:
        return ""
    month_index = base.month - 1 + int(months)
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day).isoformat()


def infer_duration(program):
    """从当前申请项目名称推导标准月数。

    返回值 (月数, 学制说明, 是否完整)。不能识别的项目返回 None，
    页面会标记“待补录”，要求班主任或管理员确认，而不是猜一个日期。
    """
    text = re.sub(r"\s+", "", str(program or ""))
    if not text:
        return None, "", False
    if "2+2+1" in text:
        return 60, "2+2+1 本硕连读，按 5 年培养计划", True
    if "0.5/1+2+1" in text:
        return 48, "0.5/1+2+1 本硕连读，按 4 年上限培养计划", True
    if "2+2" in text:
        return 48, "2+2 本科，按 4 年培养计划", True
    if "0.5/1+2" in text:
        return 36, "0.5/1+2 本科，按 3 年培养计划", True
    if "一年制" in text:
        return 12, "一年制项目", True
    if "9+6" in text:
        return 15, "9+6 培养项目，按 15 个月", True
    if "6+6" in text:
        return 12, "6+6 培养项目，按 12 个月", True
    return None, "项目学制待确认", False


def _record_event(conn, student_id, from_stage, to_stage, effective_date,
                  reason, operator_id=None, operator_role="", source_type="MANUAL",
                  related_id=None, snapshot=None):
    conn.execute(
        "INSERT INTO student_lifecycle_event "
        "(student_id,from_stage,to_stage,effective_date,reason,operator_id,"
        " operator_role,source_type,related_id,snapshot_json,create_time) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            student_id, from_stage or "", to_stage, effective_date or "",
            reason or "", operator_id, operator_role or "", source_type,
            related_id, json.dumps(snapshot or {}, ensure_ascii=False), _now(),
        ),
    )


def _latest_application(conn, student_id):
    return conn.execute(
        "SELECT * FROM application_progress WHERE student_id=? "
        "ORDER BY CASE WHEN status='已完成' THEN 0 ELSE 1 END, update_time DESC, id DESC "
        "LIMIT 1",
        (student_id,),
    ).fetchone()


def _ensure_enrollment(conn, student_id, application=None, enrollment_date="",
                       duration_months=None, duration_text="", source_type="SYSTEM"):
    """创建或补全入学档案；已有完整人工确认值优先，不被自动任务覆盖。"""
    application = application or _latest_application(conn, student_id)
    existing = conn.execute(
        "SELECT * FROM student_enrollment WHERE student_id=?", (student_id,)
    ).fetchone()
    program = str(application["program"] or "") if application else ""
    inferred_months, inferred_text, inferred_complete = infer_duration(program)
    confirmed_months = int(duration_months) if duration_months else None
    months = confirmed_months or (
        int(existing["standard_duration_months"])
        if existing and existing["standard_duration_months"] else inferred_months
    )
    enrollment_day = (
        _parse_date(enrollment_date)
        or (_parse_date(existing["enrollment_date"]) if existing else None)
        or (_parse_date(application["update_time"]) if application else None)
    )
    expected = _add_months(enrollment_day, months)
    completeness = "COMPLETE" if enrollment_day and months else (
        "PENDING_DURATION" if enrollment_day else "PENDING_APPLICATION"
    )
    snapshot = {
        "student_id": student_id,
        "application_id": application["id"] if application else None,
        "country": application["country"] if application else "",
        "university": application["university"] if application else "",
        "program": program,
        "study_mode": application["application_mode"] if application else "",
        "enrollment_date": enrollment_day.isoformat() if enrollment_day else "",
        "standard_duration_months": months,
        "duration_text": duration_text or inferred_text,
        "expected_graduation_date": expected,
        "snapshot_at": _now(),
    }
    values = (
        student_id,
        application["id"] if application else None,
        application["country"] if application else "",
        application["university"] if application else "",
        program,
        application["application_mode"] if application else "",
        enrollment_day.isoformat() if enrollment_day else "",
        months,
        duration_text or inferred_text,
        expected,
        (existing["actual_graduation_date"] if existing else ""),
        json.dumps(snapshot, ensure_ascii=False),
        completeness,
        _now(),
    )
    if existing:
        conn.execute(
            "UPDATE student_enrollment SET application_id=COALESCE(NULLIF(application_id,''),?),"
            " country=CASE WHEN ?<>'' THEN ? ELSE country END,"
            " university=CASE WHEN ?<>'' THEN ? ELSE university END,"
            " program=CASE WHEN ?<>'' THEN ? ELSE program END,"
            " study_mode=CASE WHEN ?<>'' THEN ? ELSE study_mode END,"
            " enrollment_date=CASE WHEN ?<>'' THEN ? ELSE enrollment_date END,"
            " standard_duration_months=COALESCE(?,standard_duration_months),"
            " duration_text=CASE WHEN ?<>'' THEN ? ELSE duration_text END,"
            " expected_graduation_date=CASE WHEN ?<>'' THEN ? ELSE expected_graduation_date END,"
            " snapshot_json=?, completeness_status=?, update_time=? "
            "WHERE student_id=?",
            (
                values[1], values[2], values[2], values[3], values[3],
                values[4], values[4], values[5], values[5],
                values[6], values[6], values[7], values[8], values[8],
                values[9], values[9], values[11], values[12], values[13],
                student_id,
            ),
        )
        enrollment_id = existing["id"]
    else:
        cursor = conn.execute(
            "INSERT INTO student_enrollment "
            "(student_id,application_id,country,university,program,study_mode,"
            " enrollment_date,standard_duration_months,duration_text,"
            " expected_graduation_date,actual_graduation_date,snapshot_json,"
            " completeness_status,update_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        enrollment_id = cursor.lastrowid
    return {
        "id": enrollment_id,
        "enrollment_date": enrollment_day.isoformat() if enrollment_day else "",
        "standard_duration_months": months,
        "duration_text": duration_text or inferred_text,
        "expected_graduation_date": expected,
        "completeness_status": completeness,
    }


def set_stage(user_id, stage, operator_id=None, note="", *, operator_role="",
              effective_date="", duration_months=None, duration_text="",
              force=False, source_type="MANUAL"):
    """执行受控阶段流转。返回 (是否成功, 提示语)。

    正常路径只允许逐级前进；管理员纠错必须 force=True 且填写原因。
    学籍更新、入学档案和生命周期流水在同一事务内提交。
    """
    stage = (stage or "").strip().upper()
    if stage not in STAGE_INDEX:
        return False, "阶段无效，可选：" + " / ".join(STAGES)
    if force and not (note or "").strip():
        return False, "管理员纠错必须填写变更原因"
    if force and operator_role not in ("ADMIN", "LEADER"):
        return False, "只有管理员或领导可以执行历史阶段纠错"
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT user_type, lifecycle_stage FROM sys_user WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False, "学生不存在"
        if row["user_type"] != "STUDENT":
            return False, "该账号不是学生，无法设置学籍阶段"
        old = (row["lifecycle_stage"] or "CONSULT").upper()
        if old == stage:
            return True, "阶段未变化（" + STAGE_LABEL[stage] + "）"
        if not force and stage not in ALLOWED_TRANSITIONS.get(old, set()):
            next_stages = "/".join(STAGE_LABEL[s] for s in ALLOWED_TRANSITIONS.get(old, set()))
            return False, (
                "不能从「" + STAGE_LABEL[old] + "」直接变更为「" + STAGE_LABEL[stage]
                + "」。" + ("下一阶段应为：" + next_stages if next_stages else "当前阶段没有后续主流程")
            )

        if stage == "APPLYING" and not force:
            has_application = conn.execute(
                "SELECT 1 FROM application_progress WHERE student_id=? "
                "AND status IN ('待受理','进行中','已完成') LIMIT 1",
                (user_id,),
            ).fetchone()
            if not has_application:
                return False, "进入申请期前必须先建立就读申请档案"

        application = _latest_application(conn, user_id)
        if stage == "ENROLLED" and not application and not force:
            return False, "进入在读前必须存在已完成的就读申请档案"
        if stage == "ALUMNI" and not force:
            approved = conn.execute(
                "SELECT 1 FROM student_graduation_request "
                "WHERE student_id=? AND status='已通过' LIMIT 1",
                (user_id,),
            ).fetchone()
            if not approved:
                return False, "毕业前必须完成毕业申请并通过审核"

        change_date = effective_date or date.today().isoformat()
        conn.execute(
            "UPDATE sys_user SET lifecycle_stage=? WHERE id=?", (stage, user_id))
        if stage in ("ENROLLED", "ALUMNI"):
            # 学籍已入学/毕业时，所有仍有效的申请必须同步完成。
            applications = conn.execute(
                "SELECT id, stage FROM application_progress "
                "WHERE student_id=? AND status IN ('待受理','进行中')",
                (user_id,),
            ).fetchall()
            for active_app in applications:
                conn.execute(
                    "UPDATE application_progress SET stage='已入学', status='已完成', "
                    "stage_detail=?, operator_id=?, update_time=? WHERE id=?",
                    ("学籍状态已设为" + STAGE_LABEL[stage] + "，申请流程同步完成",
                     operator_id, _now(), active_app["id"]),
                )
                ensure_application_history(
                    conn, active_app["id"], len(APPLICATION_STAGES) - 1,
                    operator_id,
                    "学籍状态变更，申请已自动完成",
                )
        enrollment = None
        if stage in ("ENROLLED", "ALUMNI"):
            enrollment = _ensure_enrollment(
                conn, user_id, application=application,
                enrollment_date=change_date,
                duration_months=duration_months,
                duration_text=duration_text,
                source_type=source_type,
            )
            if stage == "ALUMNI":
                conn.execute(
                    "UPDATE student_enrollment SET actual_graduation_date=?,"
                    " status='GRADUATED', completeness_status=CASE "
                    "WHEN enrollment_date<>'' AND standard_duration_months IS NOT NULL "
                    "THEN 'COMPLETE' ELSE completeness_status END,"
                    " update_time=? WHERE student_id=?",
                    (change_date, _now(), user_id),
                )
        _record_event(
            conn, user_id, old, stage, change_date,
            (note or "").strip() or ("进入" + STAGE_LABEL[stage]),
            operator_id, operator_role,
            "ADMIN_CORRECTION" if force else source_type,
            application["id"] if application else None,
            {
                "enrollment": enrollment,
                "application_stage": application["stage"] if application else "",
                "application_id": application["id"] if application else None,
            },
        )
        conn.commit()
    finally:
        conn.close()

    # 阶段变化主动通知学生，并说明解锁了什么
    from service.notify_logic import notify
    extra = (note or "").strip()
    body = "你的学籍阶段已更新为「" + STAGE_LABEL[stage] + "」。" + STAGE_DESC[stage]
    if stage == "ENROLLED":
        body += " 请假、成绩、售后反馈已解锁。"
        if enrollment:
            if enrollment.get("expected_graduation_date"):
                body += " 预计毕业日期：" + enrollment["expected_graduation_date"] + "。"
            else:
                body += " 项目学制尚未确认，请等待班主任补录培养计划。"
    if extra:
        body += " 备注：" + extra
    notify(user_id, "系统", "学籍阶段更新：" + STAGE_LABEL[stage], body, "overview")
    return True, "已更新为「" + STAGE_LABEL[stage] + "」"


def repair_application_consistency():
    """修复历史脏数据，让申请主状态与完整时间轴保持一致。"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT a.id, a.student_id, a.stage FROM application_progress a "
            "JOIN sys_user u ON u.id=a.student_id "
            "WHERE u.lifecycle_stage IN ('ENROLLED','ALUMNI') "
            "AND a.status IN ('待受理','进行中')"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE application_progress SET stage='已入学', status='已完成', "
                "stage_detail=?, update_time=? WHERE id=?",
                ("学籍状态为在读或已毕业，申请流程自动完成", _now(), row["id"]),
            )
            ensure_application_history(
                conn, row["id"], len(APPLICATION_STAGES) - 1, None,
                "历史数据一致性修复：申请已随入学状态完成",
            )

        # 已完成申请也必须拥有完整时间轴。旧版本可能只写了“选校定位”
        # 和“已入学”，导致 100% 进度下中间节点仍显示“待进行”。
        completed = conn.execute(
            "SELECT id, stage FROM application_progress "
            "WHERE status='已完成'"
        ).fetchall()
        for row in completed:
            current_index = (
                APPLICATION_STAGES.index(row["stage"])
                if row["stage"] in APPLICATION_STAGES else 0
            )
            if current_index != len(APPLICATION_STAGES) - 1:
                conn.execute(
                    "UPDATE application_progress SET stage='已入学', status='已完成' "
                    "WHERE id=?",
                    (row["id"],),
                )
            ensure_application_history(
                conn, row["id"], len(APPLICATION_STAGES) - 1, None,
                "历史数据一致性修复：申请流程已完成",
            )
        conn.commit()
        return len(rows) + len(completed)
    finally:
        conn.close()


def backfill_lifecycle_data():
    """给老库补生命周期流水和入学档案。

    能从申请记录得到的时间直接使用；得不到的日期保持为空并标记待补录，
    不用当前日期伪造历史。函数幂等，可每次启动执行。
    """
    conn = get_db()
    changed = 0
    try:
        students = conn.execute(
            "SELECT id, lifecycle_stage FROM sys_user WHERE user_type='STUDENT'"
        ).fetchall()
        for student in students:
            stage = (student["lifecycle_stage"] or "CONSULT").upper()
            application = _latest_application(conn, student["id"])
            has_event = conn.execute(
                "SELECT 1 FROM student_lifecycle_event WHERE student_id=? LIMIT 1",
                (student["id"],),
            ).fetchone()
            if not has_event:
                applying_date = str(application["create_time"] or "")[:10] if application else ""
                enrolled_date = str(application["update_time"] or "")[:10] if application else ""
                _record_event(
                    conn, student["id"], "", "CONSULT", "",
                    "历史账号注册记录待补录", source_type="BACKFILL",
                )
                if application:
                    _record_event(
                        conn, student["id"], "CONSULT", "APPLYING", applying_date,
                        "历史申请档案已建立", source_type="BACKFILL",
                        related_id=application["id"],
                    )
                if STAGE_INDEX[stage] >= STAGE_INDEX["ENROLLED"]:
                    _record_event(
                        conn, student["id"],
                        "APPLYING" if application else "CONSULT", "ENROLLED",
                        enrolled_date, "历史在读状态回填",
                        source_type="BACKFILL",
                        related_id=application["id"] if application else None,
                    )
                if stage == "ALUMNI":
                    _record_event(
                        conn, student["id"], "ENROLLED", "ALUMNI", "",
                        "历史毕业状态回填，实际毕业日期待补录",
                        source_type="BACKFILL",
                    )
                changed += 1
            if stage in ("ENROLLED", "ALUMNI"):
                existing = conn.execute(
                    "SELECT 1 FROM student_enrollment WHERE student_id=?",
                    (student["id"],),
                ).fetchone()
                if not existing:
                    _ensure_enrollment(
                        conn, student["id"], application=application,
                        source_type="BACKFILL",
                    )
                    changed += 1
        conn.commit()
        return changed
    finally:
        conn.close()


def request_graduation(student_id, graduation_date, completion_summary="",
                       document_ref="", student_note=""):
    """在读学生发起毕业审核；返回 (成功, 消息, 记录ID)。"""
    stage = get_stage(student_id)
    if stage != "ENROLLED":
        return False, "只有在读学生可以发起毕业审核", 0
    graduation_day = _parse_date(graduation_date)
    if not graduation_day:
        return False, "请选择有效的毕业日期", 0
    conn = get_db()
    try:
        enrollment = conn.execute(
            "SELECT * FROM student_enrollment WHERE student_id=?", (student_id,)
        ).fetchone()
        if not enrollment:
            return False, "入学档案不存在，请先让班主任补录培养计划", 0
        start = _parse_date(enrollment["enrollment_date"])
        if start and graduation_day < start:
            return False, "毕业日期不能早于入学日期", 0
        active = conn.execute(
            "SELECT id FROM student_graduation_request "
            "WHERE student_id=? AND status IN ('待审核','已通过') LIMIT 1",
            (student_id,),
        ).fetchone()
        if active:
            return False, "已有待审核或已通过的毕业申请，不能重复提交", active["id"]
        cursor = conn.execute(
            "INSERT INTO student_graduation_request "
            "(student_id,enrollment_id,graduation_date,completion_summary,"
            " document_ref,status,student_note,create_time) "
            "VALUES (?,?,?,?,?,'待审核',?,?)",
            (
                student_id, enrollment["id"], graduation_day.isoformat(),
                completion_summary.strip(), document_ref.strip(),
                student_note.strip(), _now(),
            ),
        )
        conn.commit()
        from service.notify_logic import notify_many
        staff_ids = [
            row["id"] for row in conn.execute(
                "SELECT id FROM sys_user WHERE status='正常' AND ("
                "user_type='ADMIN' OR "
                "(user_type='EMPLOYEE' AND staff_role IN ('HOMEROOM','LEADER')))"
            ).fetchall()
        ]
        notify_many(
            staff_ids, "申请", "毕业审核申请",
            "学生 #" + str(student_id) + " 提交了毕业审核申请，请核实材料。",
            "students",
        )
        return True, "毕业申请已提交，等待审核", cursor.lastrowid
    finally:
        conn.close()


def update_enrollment(student_id, enrollment_date, duration_months,
                      duration_text="", note="", operator_id=None,
                      operator_role=""):
    """补录或修正学生培养计划；仅允许在读/毕业学生。"""
    stage = get_stage(student_id)
    if stage not in ("ENROLLED", "ALUMNI"):
        return False, "只有已入学学生可以维护入学档案"
    enrollment_day = _parse_date(enrollment_date)
    if not enrollment_day:
        return False, "请填写有效的入学日期"
    try:
        months = int(duration_months)
    except (TypeError, ValueError):
        months = 0
    if months <= 0 or months > 120:
        return False, "标准学制必须是 1-120 个月"
    conn = get_db()
    try:
        application = _latest_application(conn, student_id)
        enrollment = _ensure_enrollment(
            conn, student_id, application=application,
            enrollment_date=enrollment_day.isoformat(),
            duration_months=months,
            duration_text=duration_text or str(months) + " 个月",
            source_type="ENROLLMENT_UPDATE",
        )
        _record_event(
            conn, student_id, stage, stage, date.today().isoformat(),
            note.strip() or "补录入学日期和标准学制",
            operator_id, operator_role, "ENROLLMENT_UPDATE",
            application["id"] if application else None, enrollment,
        )
        conn.commit()
        return True, "入学档案已更新"
    finally:
        conn.close()


def review_graduation(request_id, reviewer_id, approved, review_note=""):
    """审核毕业申请；通过后在同一业务动作中把学籍转为已毕业。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM student_graduation_request WHERE id=?", (request_id,)
        ).fetchone()
        if not row:
            return False, "毕业申请不存在"
        if row["status"] != "待审核":
            return False, "该毕业申请已经处理"
        status = "已通过" if approved else "已驳回"
        conn.execute(
            "UPDATE student_graduation_request SET status=?,reviewer_id=?,"
            " review_note=?,review_time=? WHERE id=?",
            (status, reviewer_id, review_note.strip(), _now(), request_id),
        )
        conn.commit()
    finally:
        conn.close()
    if approved:
        ok, msg = set_stage(
            row["student_id"], "ALUMNI", operator_id=reviewer_id,
            note=review_note.strip() or "毕业审核通过",
            effective_date=row["graduation_date"],
            source_type="GRADUATION_REVIEW",
        )
        if not ok:
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE student_graduation_request SET status='待审核',"
                    " reviewer_id=NULL,review_note='',review_time=NULL WHERE id=?",
                    (request_id,),
                )
                conn.commit()
            finally:
                conn.close()
            return False, msg
        return True, "毕业审核已通过，学籍已更新为已毕业"
    from service.notify_logic import notify
    notify(
        row["student_id"], "申请", "毕业审核未通过",
        review_note.strip() or "毕业材料需要补充，请联系班主任处理。",
        "application",
    )
    return True, "毕业申请已驳回"


def on_application_created(student_id):
    """建档即进入申请期。只升不降：已在读/毕业的学生不受影响。"""
    stage = get_stage(student_id)
    if STAGE_INDEX[stage] > STAGE_INDEX["APPLYING"]:
        return stage
    if stage == "APPLYING":
        return stage
    ok, _ = set_stage(student_id, "APPLYING", note="顾问已为你的申请建档")
    return "APPLYING" if ok else stage


def on_application_stage(student_id, application_stage):
    """申请推进到「已入学」时，自动把学籍阶段升为在读。"""
    if application_stage != "已入学":
        return get_stage(student_id)
    stage = get_stage(student_id)
    if STAGE_INDEX[stage] >= STAGE_INDEX["ENROLLED"]:
        return stage
    ok, _ = set_stage(student_id, "ENROLLED", note="申请已推进到「已入学」")
    return "ENROLLED" if ok else stage


def list_students(stage=None, keyword=None, viewer=None, country=None):
    """员工侧：带阶段的学生名单，可按阶段筛选、按姓名/账号搜索。"""
    sql = ("SELECT u.id,u.username,u.real_name,u.contact_info,u.target_country,"
           "COALESCE(u.lifecycle_stage,'CONSULT') AS lifecycle_stage,"
           "e.program AS enrollment_program,e.enrollment_date,"
           "e.standard_duration_months,e.expected_graduation_date,"
           "e.actual_graduation_date,e.completeness_status "
           "FROM sys_user u LEFT JOIN student_enrollment e ON e.student_id=u.id "
           "WHERE u.user_type='STUDENT'")
    args = []
    if stage:
        sql += " AND COALESCE(u.lifecycle_stage,'CONSULT')=?"
        args.append(stage.strip().upper())
    if keyword:
        sql += " AND (u.username LIKE ? OR u.real_name LIKE ?)"
        args += ["%" + keyword.strip() + "%", "%" + keyword.strip() + "%"]
    if country:
        sql += " AND COALESCE(NULLIF(u.target_country,''),'未填写')=?"
        args.append(country.strip())
    if viewer:
        role = viewer.get("staff_role") or "ADVISOR"
        if viewer.get("user_type") != "ADMIN" and role != "LEADER":
            if role == "HOMEROOM":
                sql += " AND u.homeroom_id=?"
            else:
                sql += " AND u.advisor_id=?"
            args.append(viewer["id"])
    sql += " ORDER BY u.id ASC"
    conn = get_db()
    try:
        rows = conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            st = (d.get("lifecycle_stage") or "CONSULT").upper()
            d["lifecycle_stage"] = st
            d["stage_label"] = STAGE_LABEL.get(st, st)
            d["stage_index"] = STAGE_INDEX.get(st, 0)
            out.append(d)
        return out
    finally:
        conn.close()
