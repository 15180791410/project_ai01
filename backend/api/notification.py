"""
ai - 站内通知中心接口
============================
学生/员工都能查看自己的通知、未读数，标记单条或全部已读。
通知由各业务模块通过 service.notify_logic.notify() 写入。
"""
from fastapi import APIRouter, Header
from pydantic import BaseModel

from db import get_db
from utils import format_data
from api.auth import require_user, request_token

router = APIRouter()


def _guard(token):
    user, err = require_user(token)
    if err:
        return None, err
    return {"uid": user["id"], "utype": user["user_type"]}, None


@router.get("/list", summary="我的通知列表（含未读数）")
def my_notifications(limit: int = 50,
                     authorization: str = Header(default=""), token: str = ""):
    payload, err = _guard(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM notification WHERE user_id=? "
            "ORDER BY is_read ASC, create_time DESC LIMIT ?",
            (payload["uid"], limit)).fetchall()
        unread = conn.execute(
            "SELECT COUNT(*) FROM notification WHERE user_id=? AND is_read=0",
            (payload["uid"],)).fetchone()[0]
        return format_data.ok({"list": [dict(r) for r in rows], "unread": unread})
    finally:
        conn.close()


@router.get("/unread", summary="未读通知数（铃铛角标用）")
def unread_count(authorization: str = Header(default=""), token: str = ""):
    payload, err = _guard(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM notification WHERE user_id=? AND is_read=0",
            (payload["uid"],)).fetchone()[0]
        return format_data.ok({"unread": n})
    finally:
        conn.close()


class ReadData(BaseModel):
    token: str
    notify_id: int = 0   # 0 表示全部已读


@router.post("/read", summary="标记通知已读（单条或全部）")
def mark_read(data: ReadData):
    payload, err = _guard(data.token)
    if err:
        return err
    conn = get_db()
    try:
        if data.notify_id:
            conn.execute(
                "UPDATE notification SET is_read=1 WHERE id=? AND user_id=?",
                (data.notify_id, payload["uid"]))
            msg = "已标记为已读"
        else:
            conn.execute(
                "UPDATE notification SET is_read=1 WHERE user_id=? AND is_read=0",
                (payload["uid"],))
            msg = "全部通知已读"
        conn.commit()
        return format_data.ok(None, msg)
    finally:
        conn.close()
