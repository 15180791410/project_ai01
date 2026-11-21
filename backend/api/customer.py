from fastapi import APIRouter, Header
from service import customer_logic
from utils import format_data
from api.auth import can_access_student, require_capability, request_token, staff_role

router = APIRouter()

@router.get("/events", summary="查询所有活动讲座")
def get_events():
    try:
        result = customer_logic.get_all_events()
        return format_data.ok(result)
    except Exception as e:
        print("客服活动查询失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：查课程
# ==========================================
@router.get("/courses", summary="查询课程项目")
def get_courses(keyword: str | None = None, category: str | None = None):
    try:
        # 调用业务逻辑层去查
        res = customer_logic.get_courses(keyword, category)
        return format_data.ok(res)
    except Exception as e:
        print("课程查询失败:", repr(e))
        return format_data.server_error()

# ==========================================
# 新增接口：查报名记录
# ==========================================
@router.get("/registrations", summary="查询报名记录")
def get_registrations(customer_id: int | None = None,
                      authorization: str = Header(default=""), token: str = ""):
    user, err = require_capability(
        request_token(authorization, token), "event_registration.read"
    )
    if err:
        return err
    try:
        if staff_role(user) == "ADVISOR" and not customer_id:
            return format_data.fail("顾问查询报名记录时请指定本人负责的学生")
        if customer_id and staff_role(user) == "ADVISOR":
            from db import get_db
            conn = get_db()
            try:
                if not can_access_student(conn, user, customer_id):
                    return format_data.forbidden("只能查询本人负责学生的报名")
            finally:
                conn.close()
        res = customer_logic.get_registrations(customer_id)
        return format_data.ok(res)
    except Exception as e:
        print("报名记录查询失败:", repr(e))
        return format_data.server_error()
