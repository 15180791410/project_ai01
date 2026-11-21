"""
ai - 内容管理与数据看板接口
==================================
职责：
1. 活动/讲座的增删改查（仅管理员可写，访客可读）
2. 留学方案/项目只从 Dify 实时读取，不在后端写入
3. 学生按意向国家分组（员工按数据范围查询）
4. 学生设置意向国家
5. 心理预警的处理（标记已解决 + 处理备注）
"""
import sqlite3

from fastapi import APIRouter, Header
from pydantic import BaseModel
from datetime import datetime

from db import get_db
from utils import format_data
from api.auth import (current_user, require_admin, require_business_subroles,
                      require_capability, require_user, request_token, staff_role,
                      student_scope)
from service import lifecycle
from service import event_chat_logic
from service import knowledge_logic

router = APIRouter()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _guard_employee(token):
    """学生名单类接口统一要求“查看学生”权限。

    维护人员没有该权限，顾问/班主任只能看到负责范围内的学生，管理员和领导
    可以查看全部学生。
    """
    user, err = require_capability(token, "student.read")
    if err:
        return None, err
    return user, None


# ==================== 活动管理 ====================
class EventData(BaseModel):
    token: str = ""
    event_name: str = ""
    event_type: str = ""
    start_time: str = ""
    end_time: str = ""
    location: str = ""
    country: str = ""
    description: str = ""
    audience_scope: str = "PUBLIC"
    max_participants: int = 0
    event_id: int = 0


@router.get("/events", summary="活动列表（公开可读）")
def list_events(event_type: str = "", audience: str = "",
                authorization: str = Header(default=""), token: str = ""):
    """按登录身份过滤活动。

    匿名访客只能看 PUBLIC/ALL；学生可看 PUBLIC/STUDENT/ALL；
    员工可看所有范围；维护人员仍只返回公开活动。
    """
    viewer = None
    raw_token = request_token(authorization, token)
    if raw_token:
        viewer, err = current_user(raw_token)
        if err:
            return err
    visible_scopes: tuple[str, ...]
    if viewer and viewer.get("user_type") in ("EMPLOYEE", "TEACHER", "ADMIN"):
        visible_scopes = ("PUBLIC", "STUDENT", "EMPLOYEE", "ALL")
    elif viewer and viewer.get("user_type") == "STUDENT":
        visible_scopes = ("PUBLIC", "STUDENT", "ALL")
    else:
        visible_scopes = ("PUBLIC", "ALL")
    if audience:
        requested = audience.strip().upper()
        if requested not in visible_scopes:
            return format_data.forbidden("当前身份无权查看该类活动")
        visible_scopes = (requested,)
    conn = get_db()
    try:
        marks = ",".join("?" for _ in visible_scopes)
        sql = (
            "SELECT * FROM event_lecture "
            "WHERE COALESCE(NULLIF(audience_scope,''),'PUBLIC') IN (" + marks + ")"
        )
        args = list(visible_scopes)
        if event_type:
            sql += " AND event_type=?"
            args.append(event_type)
        sql += " ORDER BY start_time DESC"
        rows = conn.execute(sql, args).fetchall()
        return format_data.ok([dict(r) for r in rows])
    finally:
        conn.close()


@router.post("/events/save", summary="新增/编辑活动（仅管理员）")
def save_event(data: EventData):
    _, err = require_admin(data.token)
    if err:
        return err
    if not data.event_name.strip():
        return format_data.fail("活动名称不能为空")
    scope = data.audience_scope.strip().upper() or "PUBLIC"
    if scope not in ("PUBLIC", "STUDENT", "EMPLOYEE", "ALL"):
        return format_data.fail("活动可见对象无效")
    if data.max_participants < 0:
        return format_data.fail("活动名额不能小于 0")
    conn = get_db()
    try:
        if data.event_id:
            current = conn.execute(
                "SELECT current_participants FROM event_lecture WHERE id=?",
                (data.event_id,),
            ).fetchone()
            if not current:
                return format_data.not_found("活动不存在")
            if data.max_participants and data.max_participants < (
                current["current_participants"] or 0
            ):
                return format_data.fail("名额上限不能低于当前报名人数")
            cursor = conn.execute(
                "UPDATE event_lecture SET event_name=?, start_time=?, location=?, country=?, "
                "event_type=?, description=?, audience_scope=?, max_participants=? WHERE id=?",
                (data.event_name.strip(), data.start_time, data.location, data.country,
                 data.event_type.strip(),
                 data.description, scope, data.max_participants, data.event_id),
            )
            if cursor.rowcount == 0:
                return format_data.not_found("活动不存在")
            msg = "活动已更新"
        else:
            conn.execute(
                "INSERT INTO event_lecture "
                "(event_name, event_type, start_time, location, country, description, audience_scope, "
                "max_participants, current_participants, create_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                (data.event_name.strip(), data.event_type.strip(), data.start_time,
                 data.location, data.country, data.description,
                 scope, data.max_participants, _now()),
            )
            msg = "活动已创建"
        conn.commit()
        return format_data.ok(None, msg)
    finally:
        conn.close()


@router.post("/events/delete", summary="删除活动（仅管理员）")
def delete_event(data: EventData):
    _, err = require_admin(data.token)
    if err:
        return err
    conn = get_db()
    try:
        reg_count = conn.execute(
            "SELECT COUNT(*) FROM event_registration WHERE event_id=?",
            (data.event_id,),
        ).fetchone()[0]
        if reg_count:
            return format_data.conflict("该活动已有报名记录，不能删除；请先保留记录或改为停招")
        cursor = conn.execute("DELETE FROM event_lecture WHERE id=?", (data.event_id,))
        if cursor.rowcount == 0:
            return format_data.not_found("活动不存在")
        conn.commit()
        return format_data.ok(None, "活动已删除")
    finally:
        conn.close()


# ==================== 留学方案/项目（Dify 实时只读） ====================
@router.get("/programs", summary="从 Dify 知识库实时读取留学方案")
def list_programs(category: str = ""):
    try:
        catalog = knowledge_logic.program_catalog()
        programs = catalog["programs"]
        if category:
            programs = [item for item in programs if item.get("category") == category]
        return format_data.ok(programs)
    except Exception as exc:
        print("Dify 方案目录读取失败:", repr(exc))
        return format_data.unavailable("公司业务资料暂时不可用，留学方案请稍后重试或联系顾问")


@router.get("/programs/{project_id}", summary="留学项目详情（Dify 实时）")
def program_detail(project_id: str):
    try:
        programs = knowledge_logic.program_catalog()["programs"]
        row = next((item for item in programs if item.get("id") == project_id), None)
        if not row:
            return format_data.not_found("项目不存在")
        return format_data.ok(row)
    except Exception as exc:
        print("Dify 方案详情读取失败:", repr(exc))
        return format_data.unavailable("公司业务资料暂时不可用，项目详情请稍后重试或联系顾问")


@router.get("/company", summary="从 Dify 知识库实时读取公司公开内容")
def company_content():
    try:
        structured = knowledge_logic.company_profile()
        live = knowledge_logic.retrieve(
            "广东省教育服务有限公司 企业简介 成立时间 主营业务 企业使命 企业价值观 联系方式",
            top_k=8,
        )
        return format_data.ok({"structured": structured, **live})
    except Exception as exc:
        print("Dify知识库读取失败:", repr(exc))
        return format_data.unavailable("公司业务资料暂时不可用，公司资料请稍后重试或联系顾问")


# ==================== 学生意向国家 ====================
class CountryData(BaseModel):
    token: str = ""
    country: str = ""


@router.post("/student/country", summary="学生设置意向国家")
def set_my_country(data: CountryData):
    user, err = current_user(data.token)
    if err:
        return err
    if user["user_type"] != "STUDENT":
        return format_data.fail("仅学生可设置意向国家")
    conn = get_db()
    try:
        conn.execute("UPDATE sys_user SET target_country=? WHERE id=?",
                     (data.country.strip(), user["id"]))
        conn.commit()
        return format_data.ok(None, "意向国家已保存")
    finally:
        conn.close()


@router.get("/students-by-country", summary="按意向国家分组的学生（仅员工）")
def students_by_country(authorization: str = Header(default=""), token: str = ""):
    user, err = _guard_employee(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        scope, args = student_scope(user)
        rows = conn.execute(
            "SELECT u.id,u.real_name,u.username,u.contact_info,"
            "u.target_country AS intended_country,"
            "COALESCE(u.lifecycle_stage,'CONSULT') AS lifecycle_stage,"
            "e.country AS study_country,e.program AS enrollment_program,e.enrollment_date,"
            "e.expected_graduation_date,e.actual_graduation_date,"
            "e.standard_duration_months,e.completeness_status,"
            "a.country AS application_country,a.application_mode,"
            "a.program AS application_program "
            "FROM sys_user u LEFT JOIN student_enrollment e ON e.student_id=u.id "
            "LEFT JOIN application_progress a ON a.id=("
            "  SELECT a2.id FROM application_progress a2 "
            "  WHERE a2.student_id=u.id AND COALESCE(a2.status,'进行中')<>'已撤销' "
            "  ORDER BY CASE COALESCE(a2.status,'进行中') "
            "    WHEN '进行中' THEN 0 WHEN '待受理' THEN 1 "
            "    WHEN '已完成' THEN 2 ELSE 3 END, a2.id DESC LIMIT 1"
            ") "
            "WHERE u.user_type='STUDENT'" + scope +
            " ORDER BY u.id", tuple(args)).fetchall()
        groups: dict[str, list[dict]] = {}
        for r in rows:
            d = dict(r)
            st = (d.get("lifecycle_stage") or "CONSULT").upper()
            d["lifecycle_stage"] = st
            d["stage_label"] = lifecycle.STAGE_LABEL.get(st, st)
            intended_country = str(d.get("intended_country") or "").strip()
            study_country = str(d.get("study_country") or "").strip()
            application_country = str(d.get("application_country") or "").strip()

            # 在读/毕业后以培养档案中的就读国家为准；申请期以申请国家为准；
            # 咨询期才使用学生填写的意向国家。这样旧数据也不会把“意向”
            # 错当成学生当前实际所在地。
            if st in ("ENROLLED", "ALUMNI"):
                display_country = study_country or application_country or intended_country
                country_source = "就读国家" if study_country else (
                    "申请国家" if application_country else "意向国家"
                )
            elif st == "APPLYING":
                display_country = application_country or intended_country
                country_source = "申请国家" if application_country else "意向国家"
            else:
                display_country = intended_country
                country_source = "意向国家"

            enrollment_program = str(d.get("enrollment_program") or "").strip()
            application_program = str(d.get("application_program") or "").strip()
            display_program = enrollment_program or application_program
            d["country"] = display_country or "未填写"
            d["country_source"] = country_source
            d["program"] = display_program or "未关联方案"
            d["program_source"] = (
                "入学档案" if enrollment_program else
                ("申请项目" if application_program else "未关联")
            )
            d["duration_pending"] = bool(
                st in ("ENROLLED", "ALUMNI")
                and d.get("completeness_status") != "COMPLETE"
            )
            d["duration_status"] = (
                "待补录" if d["duration_pending"] else
                ("已完整" if st in ("ENROLLED", "ALUMNI") else "未到培养阶段")
            )
            groups.setdefault(d["country"], []).append(d)
        result = [
            {"country": k, "count": len(v), "students": v}
            for k, v in sorted(groups.items())
        ]
        return format_data.ok(result)
    finally:
        conn.close()


# ==================== 学籍阶段管理（员工） ====================
class StudentStageData(BaseModel):
    token: str = ""
    student_id: int = 0
    stage: str = ""      # CONSULT / APPLYING / ENROLLED / ALUMNI
    note: str = ""
    effective_date: str = ""
    duration_months: int | None = None
    duration_text: str = ""
    force: bool = False


@router.get("/students/list", summary="学生名单（按国家、阶段、姓名筛选）")
def students_list(stage: str = "", keyword: str = "", country: str = "",
                  authorization: str = Header(default=""), token: str = ""):
    user, err = _guard_employee(request_token(authorization, token))
    if err:
        return err
    return format_data.ok(
        lifecycle.list_students(
            stage or None, keyword or None, user, country or None
        )
    )


@router.post("/students/stage", summary="设置学生学籍阶段（仅员工）")
def set_student_stage(data: StudentStageData):
    payload, err = require_capability(data.token, "student.stage.manage")
    if err:
        return err
    if not data.student_id:
        return format_data.fail("请指定学生")
    ok, msg = lifecycle.set_stage(
        data.student_id, data.stage, operator_id=payload["id"],
        note=data.note, operator_role=staff_role(payload),
        effective_date=data.effective_date,
        duration_months=data.duration_months,
        duration_text=data.duration_text,
        force=data.force,
    )
    if not ok:
        return format_data.fail(msg)
    return format_data.ok(lifecycle.get_stage_info(data.student_id), msg)


@router.get("/stages", summary="学籍阶段字典（前端渲染下拉用）")
def stage_dict():
    return format_data.ok([
        {"key": s, "label": lifecycle.STAGE_LABEL[s], "desc": lifecycle.STAGE_DESC[s]}
        for s in lifecycle.STAGES
    ])


# ==================== 心理预警处理 ====================
class AlertHandleData(BaseModel):
    token: str = ""
    alert_id: int = 0
    note: str = ""


@router.post("/alert/handle", summary="处理心理预警（仅员工）")
def handle_alert(data: AlertHandleData):
    payload, err = require_business_subroles(data.token, ("HOMEROOM",))
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT a.id,a.student_id,u.advisor_id,u.homeroom_id "
            "FROM student_psych_alert a JOIN sys_user u ON u.id=a.student_id WHERE a.id=?",
            (data.alert_id,)).fetchone()
        if not row:
            return format_data.not_found("预警记录不存在")
        if payload["user_type"] != "ADMIN" and staff_role(payload) != "LEADER":
            if row["homeroom_id"] != payload["id"]:
                return format_data.forbidden("只能处理本人负责学生的预警")
        conn.execute(
            "UPDATE student_psych_alert SET status='已解决', handler_id=?, handle_note=? WHERE id=?",
            (payload["id"], data.note.strip(), data.alert_id))
        conn.commit()
        # 通知触发预警的学生：其预警已被处理
        student_id = conn.execute(
            "SELECT student_id FROM student_psych_alert WHERE id=?", (data.alert_id,)).fetchone()
        if student_id:
            from service.notify_logic import notify
            notify(student_id["student_id"], "预警", "老师已关注你的情况",
                   "你之前的情绪提醒，老师已处理：" + (data.note.strip() or "已记录并持续关注"), "emotion")
        return format_data.ok(None, "预警已标记为已解决")
    finally:
        conn.close()


# ==================== 活动报名闭环 ====================
class RegisterData(BaseModel):
    event_id: int
    token: str = ""            # 学生登录后可传，用于关联真实身份
    customer_name: str = ""    # 访客报名时填写
    contact_info: str = ""     # 访客报名时填写
    target_country: str = ""


@router.post("/events/register", summary="报名活动（学生登录 或 访客填联系方式）")
def register_event(data: RegisterData):
    # 识别报名人：优先登录学生，否则用访客填写的信息
    student_id = 0
    actor_type = "PUBLIC"
    name = (data.customer_name or "").strip()
    contact = (data.contact_info or "").strip()
    if data.token:
        payload, token_error = current_user(data.token)
        if token_error:
            return token_error
        if payload and payload["user_type"] == "STUDENT":
            student_id = payload["id"]
            actor_type = "STUDENT"
            allowed, deny_msg = lifecycle.check(student_id, "activities")
            if not allowed:
                return format_data.forbidden(deny_msg)
            conn0 = get_db()
            try:
                u = conn0.execute("SELECT real_name, contact_info FROM sys_user WHERE id=?", (student_id,)).fetchone()
                if u:
                    name = name or (u["real_name"] or "")
                    contact = contact or (u["contact_info"] or "")
            finally:
                conn0.close()
        elif payload:
            # 员工可以查看内部活动，但活动报名业务仍按客户/学生身份处理。
            actor_type = str(payload.get("user_type") or "PUBLIC")
    if not name or not contact:
        return format_data.fail("请填写姓名和联系方式")

    result = event_chat_logic.register_event_record(
        data.event_id, name, contact, student_id=student_id,
        target_country=(data.target_country or "").strip(),
        actor_type=actor_type,
    )
    if result["status"] == "not_found":
        return format_data.not_found("活动不存在")
    if result["status"] == "duplicate":
        return format_data.conflict("该联系方式已报名此活动，无需重复提交")
    if result["status"] == "full":
        return format_data.fail("活动名额已满")
    if result["status"] == "forbidden":
        return format_data.forbidden("该活动仅面向指定对象开放，当前身份不能报名")
    return format_data.ok(result, "报名成功，我们会尽快与你联系")


@router.get("/events/registrations", summary="活动报名名单（仅员工）")
def event_registrations(event_id: int = 0,
                        authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "event_registration.read"
    )
    if err:
        return err
    conn = get_db()
    try:
        # 学生报名取学生姓名；访客报名取 CRM 姓名。顾问既能看到本人学生的
        # 报名，也能看到系统分配给本人的活动线索报名，两个数据范围取并集。
        base_sql = (
            "SELECT r.*, e.event_name, e.start_time, e.country, "
            "       COALESCE(NULLIF(u.real_name,''), NULLIF(u.username,''), "
            "                NULLIF(l.customer_name,''), NULLIF(r.contact_info,''), "
            "                '访客') AS participant_name, "
            "       l.customer_name AS lead_customer_name, "
            "       l.status AS lead_status, "
            "       l.owner_employee_id AS lead_owner_id, "
            "       ou.real_name AS lead_owner_name "
            "FROM event_registration r "
            "LEFT JOIN event_lecture e ON e.id=r.event_id "
            "LEFT JOIN sys_user u ON u.id=r.customer_id "
            "LEFT JOIN crm_lead l ON l.id=r.lead_id "
            "LEFT JOIN sys_user ou ON ou.id=l.owner_employee_id "
        )
        conditions = []
        args = []
        if event_id:
            conditions.append("r.event_id=?")
            args.append(event_id)
        if staff_role(user) == "ADVISOR":
            # 顾问的数据范围由两部分组成：本人负责的学生，以及本人负责的
            # CRM 线索。使用 OR 是为了不把访客线索错误排除在顾问工作台外。
            conditions.append("(u.advisor_id=? OR l.owner_employee_id=?)")
            args.extend([user["id"], user["id"]])
        sql = base_sql
        if conditions:
            sql += "WHERE " + " AND ".join(conditions) + " "
        sql += "ORDER BY r.create_time DESC"
        rows = conn.execute(sql, tuple(args)).fetchall()
        return format_data.ok([dict(r) for r in rows])
    finally:
        conn.close()


@router.get("/my-registrations", summary="我报名的活动（学生）")
def my_registrations(authorization: str = Header(default=""), token: str = ""):
    user, err = require_user(request_token(authorization, token), ("STUDENT",))
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT r.*, e.event_name, e.start_time, e.location FROM event_registration r "
            "LEFT JOIN event_lecture e ON e.id=r.event_id "
            "WHERE r.customer_id=? ORDER BY r.create_time DESC", (user["id"],)).fetchall()
        return format_data.ok([dict(r) for r in rows])
    finally:
        conn.close()


@router.get("/events/{event_id}", summary="活动详情")
def event_detail(event_id: int, authorization: str = Header(default=""), token: str = ""):
    """公开活动详情；不存在时返回 404，避免前端只能从列表猜数据。"""
    viewer = None
    raw_token = request_token(authorization, token)
    if raw_token:
        viewer, err = current_user(raw_token)
        if err:
            return err
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM event_lecture WHERE id=?", (event_id,)
        ).fetchone()
        if not row:
            return format_data.not_found("活动不存在")
        scope = row["audience_scope"] or "PUBLIC"
        if viewer and viewer.get("user_type") in ("EMPLOYEE", "TEACHER", "ADMIN"):
            allowed = True
        elif viewer and viewer.get("user_type") == "STUDENT":
            allowed = scope in ("PUBLIC", "STUDENT", "ALL")
        else:
            allowed = scope in ("PUBLIC", "ALL")
        if not allowed:
            return format_data.forbidden("当前身份无权查看该活动")
        return format_data.ok(dict(row))
    finally:
        conn.close()
