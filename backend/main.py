import os
import traceback
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

# 把各业务接待员叫过来
from api import student, employee, customer, admin, auth, teacher, content
from api import lead, application, notification, recommend, lifecycle_api, actions
from db import ensure_migrations, get_db, init_reference_data
from service.lifecycle import backfill_lifecycle_data, repair_application_consistency

# 启动即做增量迁移：给老数据库补上审批意见、意向国家、活动方案等新列
ensure_migrations()
_ref_conn = get_db()
try:
    init_reference_data(_ref_conn)
finally:
    _ref_conn.close()
repair_application_consistency()
backfill_lifecycle_data()

# 创建公司（应用）
app = FastAPI(title="ai智能助手")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """为所有响应补上基础浏览器安全头。

    页面仍有一小部分内联脚本，因此 CSP 暂时允许 `unsafe-inline`；至少已经
    禁止外部脚本、object 嵌入和跨站 iframe，后续可继续收紧到 nonce。
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'self'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
    return response


# 【非常重要】解决跨域问题。如果不加这段，前端网页会无法访问后端。
app.add_middleware(
    CORSMiddleware, 
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "YUEJIAO_CORS_ORIGINS",
            "http://127.0.0.1:8000,http://127.0.0.2:8000,"
            "http://localhost:8000,http://127.0.0.1:8080,"
            "http://localhost:8080",
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# 分配办公区（前缀）：/api/student 的活交给学生接待员...
app.include_router(student.router, prefix="/api/student", tags=["学生"])
app.include_router(employee.router, prefix="/api/employee", tags=["员工"])
app.include_router(customer.router, prefix="/api/customer", tags=["客服"])
app.include_router(admin.router, prefix="/api/admin", tags=["管理后台"])
app.include_router(auth.router, prefix="/api/auth", tags=["登录认证"])
app.include_router(teacher.router, prefix="/api/teacher", tags=["请假审批"])
app.include_router(content.router, prefix="/api/content", tags=["内容管理"])
app.include_router(lead.router, prefix="/api/lead", tags=["客户线索"])
app.include_router(application.router, prefix="/api/application", tags=["申请进度"])
app.include_router(notification.router, prefix="/api/notification", tags=["通知中心"])
app.include_router(recommend.router, prefix="/api/recommend", tags=["画像推荐"])
app.include_router(lifecycle_api.router, prefix="/api/lifecycle", tags=["学生生命周期"])
app.include_router(actions.router, prefix="/api/actions", tags=["通用业务动作"])


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"code": 422, "msg": "请求参数校验失败",
                 "data": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    print("未处理异常:", repr(exc))
    traceback.print_exc()
    return JSONResponse(
        status_code=500,
        content={"code": 500, "msg": "系统处理失败，请稍后重试", "data": None},
    )


# ==========================================
# 新增接口：系统健康检查 (对应需求 FR-030)
# ==========================================
@app.get("/health", summary="系统健康检查")
def health_check():
    # 不能只看 Web 进程是否响应，还要确认 SQLite 能执行最小查询。
    conn = get_db()
    try:
        conn.execute("SELECT 1").fetchone()
        return {"status": "ok", "database": "ok"}
    finally:
        conn.close()


@app.get("/api/health", summary="系统健康检查（需求规格兼容路径）")
def api_health_check():
    """需求说明书第 15.2 节明确要求 /api/health，保留 /health 兼容现有探针。"""
    return health_check()


# 前端也由 FastAPI 同源托管。浏览器密码管理器需要稳定的 http/https 域名，
# 直接双击 file:///.../admin.html 通常无法可靠保存密码。
# 该挂载必须放在所有 API 路由之后，避免静态路由抢占 /api 路径。
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

# 启动公司
if __name__ == '__main__':
    # host=0.0.0.0 监听所有网卡，Dify 容器才能通过 host.docker.internal 访问到本服务。
    # 只写 127.0.0.1 时容器无法连接，工具节点会一直卡住无返回。
    # reload=True 表示你改了代码，服务器会自动重启，不用每次都手动重启
    uvicorn.run(
        "main:app",
        host=os.getenv("YUEJIAO_HOST", "0.0.0.0"),
        port=int(os.getenv("YUEJIAO_PORT", "8000")),
        reload=os.getenv("YUEJIAO_RELOAD", "1") == "1",
    )
