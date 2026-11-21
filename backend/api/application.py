"""
ai - 留学申请进度接口
==============================
把"申请到哪一步了"从口头询问变成可视化时间轴：
选校定位 → 材料准备 → 递交申请 → 面试/补充 → 收到Offer → 办理签证 → 行前准备 → 已入学

员工创建申请并推进阶段，每一步写入 application_history 留痕，同时通知学生。
学生可查看自己的全部申请及时间轴。
"""
import base64
import binascii
from datetime import datetime
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import uuid

from fastapi import APIRouter, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from db import get_db
from utils import format_data
from api.auth import (can_access_student, require_capability, request_token,
                      require_user, student_scope)
from service.notify_logic import notify
from service import lifecycle
from service import knowledge_logic
from service import rbac

router = APIRouter()

# 标准申请阶段由生命周期模块统一维护，避免前后端或不同接口使用两套顺序。
STAGES = lifecycle.APPLICATION_STAGES


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


UPLOAD_ROOT = Path(os.getenv(
    "YUEJIAO_UPLOAD_DIR",
    str(Path(__file__).resolve().parent.parent / "uploads" / "application_materials"),
))
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_UPLOAD_SUFFIXES = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg",
    ".png", ".webp", ".txt", ".zip",
}
MATERIAL_STATUSES = {"待提交", "待审核", "已通过", "已退回"}


def _json_dict(raw):
    """把数据库中的 JSON 文本安全转换为对象；坏数据不能拖垮整个档案页。"""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _ensure_stage_records(conn, application_id):
    """为一次申请补齐 8 个阶段档案。

    新申请会得到完整节点；旧申请缺失的节点按历史回填处理，并明确标记
    BACKFILL。这里不伪造院校、申请编号、Offer 或签证结果。
    """
    app = conn.execute(
        "SELECT * FROM application_progress WHERE id=?", (application_id,)
    ).fetchone()
    if not app:
        return []
    current_stage = app["stage"] if app["stage"] in STAGES else STAGES[0]
    current_index = STAGES.index(current_stage)
    app_done = app["status"] == "已完成"
    history_rows = conn.execute(
        "SELECT * FROM application_history WHERE application_id=? ORDER BY id",
        (application_id,),
    ).fetchall()
    history_by_stage = {}
    for history in history_rows:
        history_by_stage.setdefault(history["stage"], []).append(dict(history))
    existing_rows = {
        row["stage"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM application_stage_record WHERE application_id=?",
            (application_id,),
        ).fetchall()
    }
    timestamp = _now()
    for index, stage in enumerate(STAGES):
        existing = existing_rows.get(stage)
        stage_history = history_by_stage.get(stage, [])
        if existing and existing.get("status") == "待补充" and index == current_index:
            desired_status = "待补充"
        elif index < current_index or app_done:
            desired_status = "已完成"
        elif index == current_index:
            desired_status = "进行中"
        else:
            desired_status = "待进行"

        manual_history = [
            item for item in stage_history
            if (item.get("source_type") or "MANUAL") != "BACKFILL"
        ]
        if manual_history:
            source_type = "MANUAL"
        elif stage_history or index < current_index or app_done:
            source_type = "BACKFILL"
        else:
            source_type = "SYSTEM"

        summary = (existing or {}).get("summary") or ""
        if not summary and stage_history:
            summary = str(stage_history[-1].get("note") or "")
        if not summary and index == current_index:
            summary = str(app["stage_detail"] or "")
        if not summary and (index < current_index or app_done):
            summary = "阶段状态由历史流程判定为已完成；当时明细未留存，请负责人补录。"

        detail = _json_dict((existing or {}).get("detail_json"))
        if not detail and stage == STAGES[0]:
            # 选校定位的基础信息来自申请单本身，可以安全恢复，不依赖知识库副本。
            detail = {
                "country": str(app["country"] or ""),
                "university": str(app["university"] or ""),
                "program": str(app["program"] or ""),
                "application_mode": str(app["application_mode"] or ""),
            }
            detail = {key: value for key, value in detail.items() if value}

        started_at = (existing or {}).get("started_at") or ""
        completed_at = (existing or {}).get("completed_at") or ""
        if not started_at and stage_history:
            started_at = str(stage_history[0].get("create_time") or "")
        if not completed_at and stage_history and (
            index < current_index or app_done
        ):
            completed_at = str(stage_history[-1].get("create_time") or "")
        if not started_at and index <= current_index:
            started_at = str(app["create_time"] or timestamp)
        if not completed_at and app_done:
            completed_at = str(app["update_time"] or timestamp)

        detail_json = json.dumps(detail, ensure_ascii=False) if detail else ""
        if existing:
            conn.execute(
                "UPDATE application_stage_record SET status=?,summary=?,detail_json=?,"
                "started_at=CASE WHEN started_at IS NULL OR started_at='' THEN ? ELSE started_at END,"
                "completed_at=CASE WHEN completed_at IS NULL OR completed_at='' THEN ? ELSE completed_at END,"
                "source_type=CASE WHEN source_type='MANUAL' THEN source_type ELSE ? END,"
                "update_time=? WHERE id=?",
                (desired_status, summary, detail_json, started_at, completed_at,
                 source_type, timestamp, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO application_stage_record "
                "(application_id,stage,status,summary,detail_json,started_at,completed_at,"
                "source_type,operator_id,create_time,update_time) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (application_id, stage, desired_status, summary, detail_json,
                 started_at, completed_at, source_type,
                 app["operator_id"], timestamp, timestamp),
            )
    conn.commit()
    return [
        dict(row) for row in conn.execute(
            "SELECT * FROM application_stage_record WHERE application_id=?",
            (application_id,),
        ).fetchall()
    ]


def _stage_records(conn, application_id):
    records = _ensure_stage_records(conn, application_id)
    order = {stage: index for index, stage in enumerate(STAGES)}
    for record in records:
        record["detail"] = _json_dict(record.get("detail_json"))
    return sorted(records, key=lambda row: order.get(row.get("stage"), len(order)))


def _material_rows(conn, application_id, include_hidden=True):
    sql = (
        "SELECT m.*,u.real_name AS uploaded_name,u.username AS uploaded_username,"
        "r.real_name AS reviewer_name "
        "FROM application_material m "
        "LEFT JOIN sys_user u ON u.id=m.uploaded_by "
        "LEFT JOIN sys_user r ON r.id=m.reviewed_by "
        "WHERE m.application_id=? AND m.is_active=1"
    )
    args = [application_id]
    if not include_hidden:
        sql += " AND m.visible_to_student=1"
    sql += " ORDER BY CASE m.stage "
    sql += " ".join("WHEN ? THEN %d" % index for index, _ in enumerate(STAGES))
    sql += " ELSE 99 END,m.id"
    args.extend(STAGES)
    rows = []
    for row in conn.execute(sql, args).fetchall():
        item = dict(row)
        item["download_available"] = bool(item.get("storage_name"))
        rows.append(item)
    return rows


def _application_access(conn, user, application_id, manage=False):
    """返回 (申请, 错误)。学生只能访问本人；员工继续校验数据归属。"""
    app_row = conn.execute(
        "SELECT * FROM application_progress WHERE id=?", (application_id,)
    ).fetchone()
    if not app_row:
        return None, format_data.not_found("申请记录不存在")
    if user["user_type"] == "STUDENT":
        if app_row["student_id"] != user["id"]:
            return None, format_data.forbidden("只能查看本人的申请档案")
        return app_row, None
    needed = "application.manage" if manage else "application.read"
    if not rbac.has_capability(user, needed):
        return None, format_data.forbidden("当前角色无权访问申请档案")
    if not can_access_student(conn, user, app_row["student_id"]):
        return None, format_data.forbidden("只能访问本人负责学生的申请档案")
    return app_row, None


def _insert_history(conn, application_id, stage, note, operator_id,
                    action="", source_type="MANUAL", related_material_id=None,
                    detail=None):
    conn.execute(
        "INSERT INTO application_history "
        "(application_id,stage,note,operator_id,action,source_type,"
        "related_material_id,detail_json,create_time) VALUES (?,?,?,?,?,?,?,?,?)",
        (application_id, stage, note, operator_id, action, source_type,
         related_material_id,
         json.dumps(detail or {}, ensure_ascii=False) if detail else "",
         _now()),
    )


def _guard(token):
    user, err = require_user(token)
    if err:
        return None, err
    return {"uid": user["id"], "utype": user["user_type"]}, None


def _guard_employee(token):
    """申请建档和推进只允许顾问、管理员和领导。

    班主任可以查看本人班级的申请进度并做入学后的学生服务，但不替顾问
    创建或推进申请。管理员和领导可查看并推进全部学生。
    """
    user, err = require_capability(token, "application.manage")
    if err:
        return None, err
    return user, None


def _history(conn, app_id):
    rows = conn.execute(
        "SELECT h.*,u.real_name AS operator_name,u.username AS operator_username "
        "FROM application_history h LEFT JOIN sys_user u ON u.id=h.operator_id "
        "WHERE h.application_id=? ORDER BY h.id ASC",
        (app_id,)).fetchall()
    history = [dict(r) for r in rows]
    # 历史数据修复可能晚于最终节点写入，数据库 id 顺序会变成“已入学”
    # 排在中间。对展示和 Dify 总结统一按申请阶段顺序排序，保证时间轴合理。
    return sorted(
        history,
        key=lambda item: (
            STAGES.index(item["stage"]) if item.get("stage") in STAGES else len(STAGES),
            item.get("id", 0),
        ),
    )


def _enrich(conn, row, viewer=None):
    d = dict(row)
    d["history"] = _history(conn, d["id"])
    d["stage_index"] = STAGES.index(d["stage"]) if d.get("stage") in STAGES else 0
    d["progress_pct"] = int((d["stage_index"] + 1) * 100 / len(STAGES))
    d["stage_records"] = _stage_records(conn, d["id"])
    include_hidden = bool(
        viewer and viewer.get("user_type") in ("EMPLOYEE", "TEACHER", "ADMIN")
    )
    d["materials"] = _material_rows(conn, d["id"], include_hidden)
    d["material_summary"] = {
        "total": len(d["materials"]),
        "pending_submit": sum(
            1 for item in d["materials"] if item.get("status") == "待提交"
        ),
        "pending_review": sum(
            1 for item in d["materials"] if item.get("status") == "待审核"
        ),
        "approved": sum(
            1 for item in d["materials"] if item.get("status") == "已通过"
        ),
        "returned": sum(
            1 for item in d["materials"] if item.get("status") == "已退回"
        ),
    }
    return d


# ---------------- 学生侧 ----------------
@router.get("/options", summary="申请就读可选国家、模式和项目")
def application_options():
    try:
        catalog = knowledge_logic.program_catalog()
        programs = catalog["programs"]
        countries = sorted({item["country"] for item in programs if item.get("country")})
        modes = sorted({item["category"] for item in programs if item.get("category")})
        return format_data.ok({
            "countries": countries,
            "modes": modes,
            "programs": programs,
            "source_mode": catalog["source_mode"],
            "retrieved_at": catalog["retrieved_at"],
        })
    except Exception as exc:
        print("Dify 申请方案读取失败:", repr(exc))
        return format_data.server_error("公司业务资料暂时不可用，请稍后重试")


class StudentApplyData(BaseModel):
    token: str
    application_id: int = 0
    country: str
    application_mode: str
    program: str
    university: str = ""
    remark: str = ""


class StudentApplicationActionData(BaseModel):
    token: str
    application_id: int


@router.post("/apply", summary="学生主动发起就读申请")
def student_apply(data: StudentApplyData):
    user, err = require_user(data.token, ("STUDENT",))
    if err:
        return err
    current_stage = lifecycle.get_stage(user["id"])
    if current_stage in ("ENROLLED", "ALUMNI"):
        return format_data.fail("你已是在读或毕业状态，无需重复申请就读")
    if not data.country.strip() or not data.application_mode.strip() or not data.program.strip():
        return format_data.fail("请选择国家、学习模式和申请项目")
    conn = get_db()
    try:
        catalog = knowledge_logic.program_catalog()["programs"]
        program = next((
            item for item in catalog
            if item.get("country") == data.country.strip()
            and item.get("category") == data.application_mode.strip()
            and item.get("project_name") == data.program.strip()
        ), None)
        if not program:
            return format_data.fail("所选国家、模式与项目组合不存在")
        duplicate = conn.execute(
            "SELECT id FROM application_progress WHERE student_id=? AND country=? "
            "AND program=? AND status IN ('待受理','进行中')",
            (user["id"], data.country.strip(), data.program.strip()),
        ).fetchone()
        if duplicate:
            return format_data.conflict("你已经提交过相同的就读申请，请等待顾问受理")
        advisor = conn.execute(
            "SELECT id FROM sys_user WHERE user_type='EMPLOYEE' AND staff_role='ADVISOR' "
            "AND status='正常' ORDER BY (SELECT COUNT(*) FROM application_progress a "
            "WHERE a.initiator_type='STUDENT' AND a.status IN ('待受理','进行中')"
            " AND EXISTS (SELECT 1 FROM sys_user su WHERE su.id=a.student_id "
            "AND su.advisor_id=sys_user.id)) ASC, id ASC LIMIT 1"
        ).fetchone()
        if not advisor:
            advisor = conn.execute(
                "SELECT id FROM sys_user WHERE user_type='ADMIN' AND status='正常' "
                "ORDER BY id LIMIT 1"
            ).fetchone()
        advisor_id = advisor["id"] if advisor else None
        cur = conn.execute(
            "INSERT INTO application_progress "
            "(student_id,country,university,program,stage,stage_detail,status,operator_id,"
            "initiator_type,application_mode,student_remark,create_time,update_time) "
            "VALUES (?,?,?,?,?,'学生已主动提交就读申请，等待顾问受理','待受理',?, "
            "'STUDENT',?,?,?,?)",
            (user["id"], data.country.strip(), data.university.strip(),
             data.program.strip(), STAGES[0], advisor_id,
             data.application_mode.strip(), data.remark.strip(), _now(), _now()),
        )
        app_id = cur.lastrowid
        _insert_history(
            conn, app_id, STAGES[0], "学生主动提交就读申请",
            user["id"], action="提交申请",
        )
        _ensure_stage_records(conn, app_id)
        conn.execute(
            "UPDATE sys_user SET lifecycle_stage='APPLYING',advisor_id=COALESCE(advisor_id,?) "
            "WHERE id=?",
            (advisor_id, user["id"]),
        )
        conn.commit()
        if advisor_id:
            notify(advisor_id, "申请", "新的学生就读申请",
                   f"{user['real_name'] or user['username']} 申请 {data.country} / "
                   f"{data.program}，请及时受理。", "application")
        return format_data.ok({"application_id": app_id, "advisor_id": advisor_id},
                              "就读申请已提交")
    except sqlite3.IntegrityError:
        # 唯一索引负责兜住并发重复提交，接口返回业务冲突而不是 500。
        return format_data.conflict("你已经提交过相同的就读申请，请勿重复提交")
    finally:
        conn.close()


@router.post("/update", summary="学生修改待受理的就读申请")
def update_student_application(data: StudentApplyData):
    user, err = require_user(data.token, ("STUDENT",))
    if err:
        return err
    if not data.application_id:
        return format_data.fail("缺少申请 ID")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM application_progress WHERE id=? AND student_id=?",
            (data.application_id, user["id"]),
        ).fetchone()
        if not row:
            return format_data.not_found("申请不存在")
        if row["initiator_type"] != "STUDENT" or row["status"] != "待受理":
            return format_data.fail("只有尚未受理的学生申请可以修改")
        catalog = knowledge_logic.program_catalog()["programs"]
        exists = any(
            item.get("country") == data.country.strip()
            and item.get("category") == data.application_mode.strip()
            and item.get("project_name") == data.program.strip()
            for item in catalog
        )
        if not exists:
            return format_data.fail("所选国家、模式与项目组合不存在")
        duplicate = conn.execute(
            "SELECT id FROM application_progress WHERE student_id=? AND id<>? "
            "AND country=? AND program=? AND status IN ('待受理','进行中')",
            (user["id"], data.application_id, data.country.strip(), data.program.strip()),
        ).fetchone()
        if duplicate:
            return format_data.conflict("已存在相同的有效申请")
        conn.execute(
            "UPDATE application_progress SET country=?,application_mode=?,program=?,"
            "university=?,student_remark=?,update_time=? WHERE id=?",
            (data.country.strip(), data.application_mode.strip(), data.program.strip(),
             data.university.strip(), data.remark.strip(), _now(), data.application_id),
        )
        _insert_history(
            conn, data.application_id, row["stage"], "学生修改了申请信息",
            user["id"], action="修改申请",
        )
        conn.commit()
        if row["operator_id"]:
            notify(row["operator_id"], "申请", "学生修改了就读申请",
                   f"{user['real_name'] or user['username']} 修改了就读申请，请重新确认。",
                   "application")
        return format_data.ok(None, "申请信息已修改")
    finally:
        conn.close()


@router.post("/cancel", summary="学生撤销待受理的就读申请")
def cancel_student_application(data: StudentApplicationActionData):
    user, err = require_user(data.token, ("STUDENT",))
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM application_progress WHERE id=? AND student_id=?",
            (data.application_id, user["id"]),
        ).fetchone()
        if not row:
            return format_data.not_found("申请不存在")
        if row["initiator_type"] != "STUDENT" or row["status"] != "待受理":
            return format_data.fail("只有尚未受理的学生申请可以撤销")
        conn.execute(
            "UPDATE application_progress SET status='已撤销',stage_detail=?,update_time=? "
            "WHERE id=?",
            ("学生主动撤销申请", _now(), data.application_id),
        )
        _insert_history(
            conn, data.application_id, row["stage"], "学生撤销了申请",
            user["id"], action="撤销申请",
        )
        active = conn.execute(
            "SELECT COUNT(*) FROM application_progress WHERE student_id=? "
            "AND status IN ('待受理','进行中')",
            (user["id"],),
        ).fetchone()[0]
        if not active:
            conn.execute(
                "UPDATE sys_user SET lifecycle_stage='CONSULT' "
                "WHERE id=? AND lifecycle_stage='APPLYING'",
                (user["id"],),
            )
        conn.commit()
        if row["operator_id"]:
            notify(row["operator_id"], "申请", "学生撤销了就读申请",
                   f"{user['real_name'] or user['username']} 撤销了 {row['country']} / {row['program']} 申请。",
                   "application")
        return format_data.ok(None, "申请已撤销")
    finally:
        conn.close()


@router.get("/my", summary="学生查看自己的申请进度（含时间轴）")
def my_applications(authorization: str = Header(default=""), token: str = ""):
    user, err = require_user(request_token(authorization, token), ("STUDENT",))
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM application_progress WHERE student_id=? ORDER BY id DESC",
            (user["id"],)).fetchall()
        return format_data.ok([_enrich(conn, r, user) for r in rows])
    finally:
        conn.close()


# ---------------- 员工侧 ----------------
@router.get("/all", summary="员工查看全部申请进度")
def all_applications(authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "application.read"
    )
    if err:
        return err
    conn = get_db()
    try:
        scope, args = student_scope(user)
        rows = conn.execute(
            "SELECT a.*, u.real_name AS student_name, u.username AS student_username "
            "FROM application_progress a LEFT JOIN sys_user u ON u.id=a.student_id "
            "WHERE 1=1" + scope + " ORDER BY a.update_time DESC, a.id DESC",
            tuple(args),
        ).fetchall()
        return format_data.ok([_enrich(conn, r, user) for r in rows])
    finally:
        conn.close()


@router.get("/{application_id}/detail", summary="申请案件档案详情")
def application_detail(application_id: int,
                       authorization: str = Header(default=""),
                       token: str = ""):
    """管理端和学生端共用的完整案件档案。

    返回 8 个阶段的结构化明细、材料清单、附件状态、人工审核记录和操作日志。
    学生看不到内部隐藏材料；员工仍受学生归属范围限制。
    """
    user, err = require_user(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        app_row, err = _application_access(conn, user, application_id)
        if err:
            return err
        result = _enrich(conn, app_row, user)
        can_manage = (
            user["user_type"] != "STUDENT"
            and rbac.has_capability(user, "application.manage")
        )
        result["permissions"] = {
            "is_student": user["user_type"] == "STUDENT",
            "can_manage_stage": can_manage,
            "can_upload": user["user_type"] == "STUDENT" or can_manage,
            "can_review": can_manage,
            "can_download": True,
        }
        return format_data.ok(result)
    finally:
        conn.close()


class StageArchiveData(BaseModel):
    token: str
    summary: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


@router.post("/{application_id}/stages/{stage}/detail",
             summary="完善申请阶段档案")
def update_stage_archive(application_id: int, stage: str,
                         data: StageArchiveData):
    manager, err = require_capability(data.token, "application.manage")
    if err:
        return err
    if stage not in STAGES:
        return format_data.fail("申请阶段无效")
    summary = data.summary.strip()
    if len(summary) > 3000:
        return format_data.fail("阶段说明不能超过 3000 字")
    if len(data.fields) > 24:
        return format_data.fail("阶段明细字段过多")
    clean_fields = {}
    for key, value in data.fields.items():
        field_key = str(key or "").strip()
        field_value = str(value or "").strip()
        if not field_key or len(field_key) > 60:
            return format_data.fail("阶段明细字段名称不合法")
        if len(field_value) > 1500:
            return format_data.fail("阶段明细单项内容不能超过 1500 字")
        if field_value:
            clean_fields[field_key] = field_value
    if not summary and not clean_fields:
        return format_data.fail("请至少填写一项阶段明细")
    conn = get_db()
    try:
        app_row, err = _application_access(
            conn, manager, application_id, manage=True
        )
        if err:
            return err
        _ensure_stage_records(conn, application_id)
        record = conn.execute(
            "SELECT * FROM application_stage_record "
            "WHERE application_id=? AND stage=?",
            (application_id, stage),
        ).fetchone()
        if not record:
            return format_data.not_found("阶段档案不存在")
        conn.execute(
            "UPDATE application_stage_record SET summary=?,detail_json=?,"
            "source_type='MANUAL',operator_id=?,update_time=? WHERE id=?",
            (summary, json.dumps(clean_fields, ensure_ascii=False),
             manager["id"], _now(), record["id"]),
        )
        _insert_history(
            conn, application_id, stage, "完善阶段档案",
            manager["id"], action="完善阶段档案",
            detail={"summary": summary, "fields": clean_fields},
        )
        conn.commit()
        return format_data.ok(
            _enrich(conn, app_row, manager),
            "阶段档案已保存",
        )
    finally:
        conn.close()


class MaterialChecklistData(BaseModel):
    token: str
    stage: str
    material_name: str
    required_flag: bool = False
    note: str = ""
    visible_to_student: bool = True


@router.post("/{application_id}/materials/checklist",
             summary="添加申请材料清单")
def add_material_checklist(application_id: int, data: MaterialChecklistData):
    manager, err = require_capability(data.token, "application.manage")
    if err:
        return err
    if data.stage not in STAGES:
        return format_data.fail("申请阶段无效")
    material_name = data.material_name.strip()
    if not material_name or len(material_name) > 120:
        return format_data.fail("材料名称不能为空且不能超过 120 字")
    conn = get_db()
    try:
        _, err = _application_access(conn, manager, application_id, manage=True)
        if err:
            return err
        _ensure_stage_records(conn, application_id)
        cursor = conn.execute(
            "INSERT INTO application_material "
            "(application_id,stage,material_name,required_flag,status,content_note,"
            "visible_to_student,create_time,update_time) "
            "VALUES (?,?,?,?, '待提交', ?,?,?,?)",
            (application_id, data.stage, material_name,
             1 if data.required_flag else 0, data.note.strip(),
             1 if data.visible_to_student else 0, _now(), _now()),
        )
        material_id = cursor.lastrowid
        _insert_history(
            conn, application_id, data.stage, "新增材料清单：" + material_name,
            manager["id"], action="新增材料清单",
            related_material_id=material_id,
            detail={"required": bool(data.required_flag), "note": data.note.strip()},
        )
        conn.commit()
        return format_data.ok({"material_id": material_id}, "材料清单已添加")
    finally:
        conn.close()


class MaterialUploadData(BaseModel):
    token: str
    stage: str = ""
    material_id: int = 0
    material_name: str = ""
    file_name: str
    content_base64: str
    note: str = ""


@router.post("/{application_id}/materials/upload",
             summary="上传或补交申请材料")
def upload_application_material(application_id: int, data: MaterialUploadData):
    user, err = require_user(data.token)
    if err:
        return err
    safe_name = Path(data.file_name or "").name.strip()
    if not safe_name:
        return format_data.fail("请选择要上传的文件")
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        return format_data.fail(
            "不支持该文件类型，可上传 PDF、Word、Excel、图片、TXT 或 ZIP"
        )
    encoded = (data.content_base64 or "").strip()
    if "," in encoded and encoded.lower().startswith("data:"):
        encoded = encoded.split(",", 1)[1]
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return format_data.fail("文件内容不是有效的 Base64 数据")
    if not content:
        return format_data.fail("上传文件不能为空")
    if len(content) > MAX_UPLOAD_BYTES:
        return format_data.fail("单个文件不能超过 8 MB")

    conn = get_db()
    file_path = None
    try:
        app_row, err = _application_access(conn, user, application_id)
        if err:
            return err
        material = None
        if data.material_id:
            material = conn.execute(
                "SELECT * FROM application_material "
                "WHERE id=? AND application_id=? AND is_active=1",
                (data.material_id, application_id),
            ).fetchone()
            if not material:
                return format_data.not_found("材料记录不存在")
            if user["user_type"] == "STUDENT" and not material["visible_to_student"]:
                return format_data.forbidden("该材料当前不对学生开放")
            if user["user_type"] == "STUDENT" and material["status"] == "已通过":
                return format_data.conflict("该材料已审核通过，如需替换请联系顾问")
            stage = material["stage"]
            material_name = material["material_name"]
            if (
                user["user_type"] == "STUDENT"
                and stage != app_row["stage"]
                and material["status"] not in ("待提交", "已退回")
            ):
                return format_data.fail("该历史阶段材料只能查看，不能直接替换")
        else:
            stage = (data.stage or app_row["stage"] or "").strip()
            if stage not in STAGES:
                return format_data.fail("申请阶段无效")
            if user["user_type"] == "STUDENT" and stage != app_row["stage"]:
                stage_record = conn.execute(
                    "SELECT status FROM application_stage_record "
                    "WHERE application_id=? AND stage=?",
                    (application_id, stage),
                ).fetchone()
                if not stage_record or stage_record["status"] != "待补充":
                    return format_data.fail(
                        "学生只能在当前申请阶段或负责人标记为待补充的阶段上传材料"
                    )
            material_name = (
                data.material_name.strip() or Path(safe_name).stem
            )
            if len(material_name) > 120:
                return format_data.fail("材料名称不能超过 120 字")
            _ensure_stage_records(conn, application_id)
            cursor = conn.execute(
                "INSERT INTO application_material "
                "(application_id,stage,material_name,status,content_note,"
                "visible_to_student,create_time,update_time) "
                "VALUES (?,?,?, '待审核', ?,1,?,?)",
                (application_id, stage, material_name,
                 data.note.strip(), _now(), _now()),
            )
            material_id = cursor.lastrowid
            material = conn.execute(
                "SELECT * FROM application_material WHERE id=?",
                (material_id,),
            ).fetchone()

        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        storage_name = uuid.uuid4().hex + suffix
        file_path = UPLOAD_ROOT / storage_name
        file_path.write_bytes(content)
        mime_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        current_version = int(material["version"] or 1)
        if material["file_name"]:
            current_version += 1
        conn.execute(
            "UPDATE application_material SET status='待审核',file_name=?,storage_name=?,"
            "mime_type=?,file_size=?,content_note=?,review_note='',uploaded_by=?,"
            "version=?,visible_to_student=CASE WHEN ?='STUDENT' THEN 1 "
            "ELSE visible_to_student END,update_time=? WHERE id=?",
            (safe_name, storage_name, mime_type, len(content),
             data.note.strip() or material["content_note"],
             user["id"], current_version, user["user_type"], _now(),
             material["id"]),
        )
        _insert_history(
            conn, application_id, stage, "上传材料：" + material_name,
            user["id"], action="上传材料",
            related_material_id=material["id"],
            detail={
                "file_name": safe_name,
                "file_size": len(content),
                "version": current_version,
            },
        )
        conn.commit()
        if user["user_type"] == "STUDENT":
            target_id = app_row["operator_id"]
            if target_id:
                notify(
                    target_id, "申请", "学生补交了申请材料",
                    f"{user['real_name'] or user['username']} 上传了「{material_name}」，请审核。",
                    "application",
                )
        else:
            notify(
                app_row["student_id"], "申请", "申请材料已更新",
                "你的申请材料「" + material_name + "」已更新，可进入申请档案查看。",
                "application",
            )
        return format_data.ok(
            {"material_id": material["id"], "version": current_version},
            "材料已上传，等待审核",
        )
    except OSError as exc:
        print("申请材料保存失败:", repr(exc))
        if file_path and file_path.exists():
            file_path.unlink()
        return format_data.server_error("文件保存失败，请稍后重试")
    except Exception:
        # 数据库事务失败时不能留下没有元数据引用的孤儿附件。
        if file_path and file_path.exists():
            file_path.unlink()
        raise
    finally:
        conn.close()


class MaterialReviewData(BaseModel):
    token: str
    status: str
    review_note: str = ""


@router.post("/materials/{material_id}/review", summary="审核申请材料")
def review_application_material(material_id: int, data: MaterialReviewData):
    manager, err = require_capability(data.token, "application.manage")
    if err:
        return err
    if data.status not in {"已通过", "已退回"}:
        return format_data.fail("审核状态只能是已通过或已退回")
    review_note = data.review_note.strip()
    if data.status == "已退回" and not review_note:
        return format_data.fail("退回材料必须填写原因")
    conn = get_db()
    try:
        material = conn.execute(
            "SELECT * FROM application_material WHERE id=? AND is_active=1",
            (material_id,),
        ).fetchone()
        if not material:
            return format_data.not_found("材料记录不存在")
        app_row, err = _application_access(
            conn, manager, material["application_id"], manage=True
        )
        if err:
            return err
        conn.execute(
            "UPDATE application_material SET status=?,review_note=?,reviewed_by=?,"
            "update_time=? WHERE id=?",
            (data.status, review_note, manager["id"], _now(), material_id),
        )
        _insert_history(
            conn, material["application_id"], material["stage"],
            "材料审核：" + str(material["material_name"]) + " · " + data.status,
            manager["id"], action="审核材料",
            related_material_id=material_id,
            detail={"status": data.status, "review_note": review_note},
        )
        conn.commit()
        notify(
            app_row["student_id"], "申请", "申请材料审核结果",
            "「" + str(material["material_name"]) + "」审核结果：" + data.status
            + ("。原因：" + review_note if review_note else ""),
            "application",
        )
        return format_data.ok(None, "材料审核结果已保存")
    finally:
        conn.close()


@router.get("/materials/{material_id}/download", summary="下载本人或负责学生的材料")
def download_application_material(material_id: int,
                                  authorization: str = Header(default=""),
                                  token: str = ""):
    user, err = require_user(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        material = conn.execute(
            "SELECT * FROM application_material WHERE id=? AND is_active=1",
            (material_id,),
        ).fetchone()
        if not material:
            return format_data.not_found("材料文件不存在")
        _, err = _application_access(conn, user, material["application_id"])
        if err:
            return err
        if user["user_type"] == "STUDENT" and not material["visible_to_student"]:
            return format_data.forbidden("该材料当前不对学生开放")
        storage_name = str(material["storage_name"] or "")
        if not storage_name:
            return format_data.not_found("该材料还没有上传附件")
        path = (UPLOAD_ROOT / storage_name).resolve()
        root = UPLOAD_ROOT.resolve()
        if root not in path.parents or not path.is_file():
            return format_data.not_found("材料文件不存在")
        return FileResponse(
            str(path),
            media_type=material["mime_type"] or "application/octet-stream",
            filename=material["file_name"] or path.name,
        )
    finally:
        conn.close()


class CreateAppData(BaseModel):
    token: str
    student_id: int
    country: str = ""
    university: str = ""
    program: str = ""


@router.post("/create", summary="员工为学生创建申请")
def create_application(data: CreateAppData):
    manager, err = _guard_employee(data.token)
    if err:
        return err
    conn = get_db()
    try:
        if not can_access_student(conn, manager, data.student_id):
            return format_data.forbidden("只能为本人负责的学生创建申请")
        stu = conn.execute(
            "SELECT id, lifecycle_stage FROM sys_user WHERE id=? AND user_type='STUDENT'",
            (data.student_id,)).fetchone()
        if not stu:
            return format_data.fail("学生账号不存在")
        if (stu["lifecycle_stage"] or "CONSULT") in ("ENROLLED", "ALUMNI"):
            return format_data.fail("该学生已入学或毕业，不能重新建立就读申请")
        first = STAGES[0]
        cur = conn.execute(
            "INSERT INTO application_progress (student_id, country, university, program, stage, "
            "stage_detail, status, operator_id, create_time, update_time) "
            "VALUES (?, ?, ?, ?, ?, ?, '进行中', ?, ?, ?)",
            (data.student_id, (data.country or "").strip(), (data.university or "").strip(),
             (data.program or "").strip(), first, "申请档案已建立，开始选校定位",
             manager["id"], _now(), _now()),
        )
        app_id = cur.lastrowid
        _insert_history(
            conn, app_id, first, "申请档案已建立",
            manager["id"], action="创建申请",
        )
        _ensure_stage_records(conn, app_id)
        conn.commit()
        notify(data.student_id, "申请", "留学申请已启动",
               "顾问已为你的申请建档（" + (data.university or data.country or "待定") +
               "），可在「申请进度」查看。", "application")
        # 学籍联动：建档即进入申请期，解锁「申请进度」
        lifecycle.on_application_created(data.student_id)
        return format_data.ok({"application_id": app_id}, "申请已创建")
    finally:
        conn.close()


class AdvanceData(BaseModel):
    token: str
    application_id: int
    stage: str = ""       # 目标阶段；留空则推进到下一阶段
    note: str = ""


@router.post("/advance", summary="员工推进申请阶段（自动留痕 + 通知学生）")
def advance_application(data: AdvanceData):
    manager, err = _guard_employee(data.token)
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM application_progress WHERE id=?", (data.application_id,)).fetchone()
        if not row:
            return format_data.not_found("申请记录不存在")
        # 顾问只能推进本人负责学生的申请；管理员和领导可推进全部申请。
        if not can_access_student(conn, manager, row["student_id"]):
            return format_data.forbidden("只能推进本人负责学生的申请")
        if row["status"] == "已撤销":
            return format_data.fail("该申请已被学生撤销，不能继续修改进度")
        if row["status"] == "已完成":
            return format_data.conflict("该申请已完成，不能继续推进")
        cur_idx = STAGES.index(row["stage"]) if row["stage"] in STAGES else 0
        target = (data.stage or "").strip()
        if target:
            if target not in STAGES:
                return format_data.fail("阶段无效：" + target)
            new_idx = STAGES.index(target)
        else:
            new_idx = min(cur_idx + 1, len(STAGES) - 1)
        if new_idx < cur_idx:
            return format_data.fail("申请阶段只能向前推进；如需更正请联系管理员处理")
        if new_idx > cur_idx + 1:
            return format_data.fail(
                "申请必须按阶段顺序推进，下一阶段应为「" + STAGES[cur_idx + 1] + "」"
            )
        if new_idx == cur_idx:
            return format_data.conflict("申请已经处于该阶段")
        new_stage = STAGES[new_idx]
        status = "已完成" if new_idx == len(STAGES) - 1 else "进行中"
        note = (data.note or "").strip() or ("进入「" + new_stage + "」阶段")
        _ensure_stage_records(conn, data.application_id)
        conn.execute(
            "UPDATE application_progress SET stage=?, stage_detail=?, status=?, operator_id=?, update_time=? "
            "WHERE id=?",
            (new_stage, note, status, manager["id"], _now(), data.application_id))
        conn.execute(
            "UPDATE application_stage_record SET status='已完成',summary=?,"
            "completed_at=?,source_type='MANUAL',operator_id=?,update_time=? "
            "WHERE application_id=? AND stage=?",
            (note, _now(), manager["id"], _now(),
             data.application_id, row["stage"]),
        )
        conn.execute(
            "UPDATE application_stage_record SET status=?,started_at=?,"
            "source_type='MANUAL',operator_id=?,update_time=? "
            "WHERE application_id=? AND stage=?",
            ("已完成" if status == "已完成" else "进行中", _now(),
             manager["id"], _now(), data.application_id, new_stage),
        )
        # 正常流程每次只前进一格；若旧数据中间有缺口，这里一并补记，
        # 保证时间轴不会出现“已到后面阶段，前面仍待进行”。
        lifecycle.ensure_application_history(
            conn, data.application_id, new_idx, manager["id"], note
        )
        conn.commit()
        notify(row["student_id"], "申请", "申请进度更新：" + new_stage,
               note + "（" + (row["university"] or row["country"] or "你的申请") + "）", "application")
        # 学籍联动：推进到「已入学」时自动升级为在读，解锁请假/成绩/售后
        lifecycle.on_application_stage(row["student_id"], new_stage)
        return format_data.ok(None, "已推进到「" + new_stage + "」")
    finally:
        conn.close()
