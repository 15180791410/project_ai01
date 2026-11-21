"""
ai - 咨询线索（CRM）闭环接口
====================================
1. 官网/客服页访客提交咨询留资 → 自动进入 crm_lead，分配给负载最轻的员工
2. 员工查看线索、更新销售阶段、追加跟进记录
3. 关键节点给负责员工写站内通知
"""
from datetime import datetime

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from db import get_db
from utils import format_data
from api.auth import require_capability, request_token, staff_role
from service.crm_logic import pick_lead_owner
from service.notify_logic import notify
from utils.rate_limit import rate_limit

router = APIRouter()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pick_owner(conn):
    """兼容旧调用名，实际规则由 service.crm_logic 统一维护。"""
    return pick_lead_owner(conn) or None


class LeadSubmitData(BaseModel):
    customer_name: str = ""
    contact_info: str = ""
    target_country: str = ""
    background_info: str = ""


@router.post("/submit", summary="官网咨询留资（免登录，公开）")
def submit_lead(data: LeadSubmitData, request: Request):
    limited = rate_limit(request, "lead_submit", 10, 60)
    if limited:
        return limited
    name = (data.customer_name or "").strip()
    contact = (data.contact_info or "").strip()
    if not name:
        return format_data.fail("请填写您的称呼")
    if not contact:
        return format_data.fail("请留下联系方式，方便顾问联系您")
    conn = get_db()
    try:
        owner = _pick_owner(conn)
        if not owner:
            return format_data.server_error("暂无可接收客户线索的顾问，请稍后再试")
        background = (data.background_info or "").strip()
        if data.target_country:
            background = ("意向国家：" + data.target_country + "；" + background).strip("；")
        cur = conn.execute(
            "INSERT INTO crm_lead (customer_name, contact_info, background_info, target_country, "
            "status, owner_employee_id, follow_up_history, create_time, update_time) "
            "VALUES (?, ?, ?, ?, '新增意向', ?, ?, ?, ?)",
            (name, contact, background, (data.target_country or "").strip(),
             owner, _now() + " 客户通过官网提交咨询", _now(), _now()),
        )
        conn.commit()
        lid = cur.lastrowid
        notify(owner, "线索", "新咨询线索：" + name,
               "客户 " + name + " 提交了留学咨询，联系方式：" + contact, "leads")
        return format_data.ok({"lead_id": lid, "owner_employee_id": owner},
                              "提交成功，顾问会尽快与您联系")
    finally:
        conn.close()


@router.get("/list", summary="全部线索（员工）")
def list_leads(authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "crm.read"
    )
    if err:
        return err
    conn = get_db()
    try:
        sql = (
            "SELECT l.*, u.real_name AS owner_name FROM crm_lead l "
            "LEFT JOIN sys_user u ON u.id = l.owner_employee_id "
        )
        args = []
        if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
            sql += "WHERE l.owner_employee_id=? "
            args.append(user["id"])
        sql += "ORDER BY l.update_time DESC, l.id DESC"
        rows = conn.execute(sql, args).fetchall()
        return format_data.ok([dict(r) for r in rows])
    finally:
        conn.close()


class FollowUpData(BaseModel):
    token: str
    lead_id: int
    status: str = ""      # 新增意向/已联系/意向明确/已签约/已流失
    note: str = ""        # 本次跟进记录


@router.post("/follow-up", summary="员工跟进线索（更新阶段 + 追加记录）")
def follow_up(data: FollowUpData):
    payload, err = require_capability(data.token, "crm.manage")
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM crm_lead WHERE id=?", (data.lead_id,)).fetchone()
        if not row:
            return format_data.not_found("线索不存在")
        if payload["user_type"] != "ADMIN" and staff_role(payload) != "LEADER":
            if row["owner_employee_id"] != payload["id"]:
                return format_data.forbidden("只能跟进本人负责的客户")
        status = (data.status or "").strip() or (row["status"] or "新增意向")
        if status not in ("新增意向", "跟进中", "已签约", "已流失"):
            return format_data.fail("状态不合法，只能是：新增意向/跟进中/已签约/已流失")
        history = row["follow_up_history"] or ""
        note = (data.note or "").strip()
        entry = "\n" + _now() + " [员工#" + str(payload["id"]) + "] "
        if note:
            entry += note + "（阶段：" + status + "）"
        else:
            entry += "阶段更新为：" + status
        conn.execute(
            "UPDATE crm_lead SET status=?, follow_up_history=?, update_time=? WHERE id=?",
            (status, (history + entry).strip(), _now(), data.lead_id))
        conn.commit()
        return format_data.ok(None, "跟进记录已保存")
    finally:
        conn.close()


@router.get("/{lead_id}", summary="客户线索详情")
def lead_detail(lead_id: int, authorization: str = Header(default=""),
                token: str = ""):
    """返回单个客户详情；普通顾问仍只能查看本人负责的线索。"""
    user, err = require_capability(
        request_token(authorization, token), "crm.read"
    )
    if err:
        return err
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT l.*, u.real_name AS owner_name FROM crm_lead l "
            "LEFT JOIN sys_user u ON u.id=l.owner_employee_id WHERE l.id=?",
            (lead_id,),
        ).fetchone()
        if not row:
            return format_data.not_found("线索不存在")
        if user["user_type"] != "ADMIN" and staff_role(user) != "LEADER":
            if row["owner_employee_id"] != user["id"]:
                return format_data.forbidden("只能查看本人负责的客户")
        return format_data.ok(dict(row))
    finally:
        conn.close()
