"""升级活动报名 → CRM 关联的一次性幂等脚本。

执行内容：
1. 为旧数据库补充 lead_id、source_event_id 等字段和索引；
2. 将历史上没有登录学生账号的访客报名补成 CRM 线索；
3. 回填 event_registration.lead_id，使顾问能在活动板块看到并跟进。

脚本可重复执行；已经关联过的报名会被跳过，不会重复创建客户。
"""
from db import get_db, ensure_migrations
from seed_complete_demo import _backfill_event_leads


def main():
    changed = ensure_migrations()
    conn = get_db()
    try:
        _backfill_event_leads(conn)
        conn.commit()
        total = conn.execute(
            "SELECT COUNT(*) FROM event_registration "
            "WHERE COALESCE(customer_id,0)=0 AND COALESCE(lead_id,0)>0"
        ).fetchone()[0]
        unlinked = conn.execute(
            "SELECT COUNT(*) FROM event_registration "
            "WHERE COALESCE(customer_id,0)=0 AND COALESCE(lead_id,0)=0 "
            "AND TRIM(COALESCE(contact_info,''))<>''"
        ).fetchone()[0]
    finally:
        conn.close()
    print("数据库字段变更：", changed or "无（已是最新）")
    print("已关联 CRM 的访客报名：", total)
    print("仍待关联的访客报名：", unlinked)


if __name__ == "__main__":
    main()
