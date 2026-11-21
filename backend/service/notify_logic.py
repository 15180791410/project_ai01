"""
ai - 站内通知服务
========================
统一入口：任何业务（审批、预警、活动报名、申请进度）给学生发消息，
都调用 notify()，避免各处重复写 SQL、格式不一致。

通知会出现在学生的「通知中心」铃铛里，未读数实时统计。
"""
from datetime import datetime

from db import get_db


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def notify(user_id, ntype, title, content, link_page=""):
    """给指定用户写一条站内通知。失败不影响主业务，故捕获异常。"""
    try:
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO notification (user_id, type, title, content, is_read, link_page, create_time) "
                "VALUES (?, ?, ?, ?, 0, ?, ?)",
                (int(user_id), ntype, title, content, link_page, _now()),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as exc:
        # 通知失败不能反向破坏审批/预警主事务，但必须留下可排查记录，
        # 不能静默吞掉后仍向用户宣称“已经通知”。
        print("站内通知写入失败:", repr(exc))
        return False


def notify_many(user_ids, ntype, title, content, link_page=""):
    """批量通知（如给所有员工广播）。"""
    ok = 0
    for uid in user_ids:
        if notify(uid, ntype, title, content, link_page):
            ok += 1
    return ok
