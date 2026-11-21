"""统一 API 返回结构。

业务失败使用真实 HTTP 状态码，响应体仍保留 code/msg/data，方便前端和 Dify
同时根据 HTTP 状态与业务码判断结果。
"""
from fastapi.responses import JSONResponse


def ok(data=None, msg="操作成功"):
    return {
        "code": 200,
        "msg": msg,
        "data": data,
    }


def _error(status_code: int, msg: str, data=None):
    return JSONResponse(
        status_code=status_code,
        content={"code": status_code, "msg": msg, "data": data},
    )


def fail(msg="操作失败"):
    return _error(400, msg)


def not_found(msg="数据不存在"):
    return _error(404, msg)


def forbidden(msg="当前状态不可使用该功能", data=None):
    return _error(403, msg, data)


def unauthorized(msg="登录已过期，请重新登录"):
    return _error(401, msg)


def conflict(msg="数据冲突"):
    return _error(409, msg)


def server_error(msg="系统处理失败，请稍后重试"):
    return _error(500, msg)


def unavailable(msg="公司业务资料服务暂时不可用，请稍后重试或联系顾问"):
    """外部业务资料/模型暂不可用，但后端业务本身仍在运行。"""
    return _error(503, msg)
