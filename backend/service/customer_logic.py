from db import get_db
from service import knowledge_logic

# 功能：查所有的活动
def get_all_events():
    """旧客服活动接口只暴露对外活动，不返回员工内部活动。"""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM event_lecture "
        "WHERE COALESCE(NULLIF(audience_scope,''),'PUBLIC') IN ('PUBLIC','ALL')"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]

# ==========================================
# 新增：查课程/项目 (对应需求 FR-023)
# ==========================================
def get_courses(keyword=None, category=None):
    programs = knowledge_logic.program_catalog()["programs"]
    if keyword:
        programs = [
            item for item in programs
            if keyword.lower() in item.get("project_name", "").lower()
        ]
    if category:
        programs = [
            item for item in programs if item.get("category") == category
        ]
    return programs

# ==========================================
# 新增：查报名记录 (对应需求 FR-027)
# ==========================================
def get_registrations(customer_id=None):
    conn = get_db()
    # 这里用到了连表查询（LEFT JOIN），因为报名表只有 event_id，我们要去活动表把 event_name 查出来
    sql = """
        SELECT r.id, r.event_id, e.event_name, r.customer_id, r.status, r.create_time
        FROM event_registration r
        LEFT JOIN event_lecture e ON r.event_id = e.id
    """
    if customer_id:
        # 如果传了客户ID，就只看这个客户的报名记录
        sql += " WHERE r.customer_id = ?"
        rows = conn.execute(sql, (customer_id,)).fetchall()
    else:
        # 如果没传客户ID，说明是管理员，看所有的报名记录
        rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(row) for row in rows]
