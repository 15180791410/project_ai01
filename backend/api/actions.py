"""通用业务动作准备与执行接口。"""
from __future__ import annotations

import json

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.auth import require_user
from service import business_action_logic
from utils import format_data


router = APIRouter()


class ActionPrepareData(BaseModel):
    token: str = ""
    visitor_key: str = ""
    query: str = ""
    history: str = ""
    action_type: str = ""
    user_action: str = "collect"
    slots_json: str = "{}"


class ActionExecuteData(BaseModel):
    token: str = ""
    visitor_key: str = ""
    confirmation_token: str


def _actor(data):
    if data.token:
        user, err = require_user(data.token)
        if err:
            return None, err
        return user, None
    # 匿名访客只允许活动报名；具体白名单由业务层根据 action_type 再校验。
    return None, None


@router.post("/prepare", summary="通用业务动作准备：补字段、消歧和确认")
def prepare_action(data: ActionPrepareData):
    user, err = _actor(data)
    if err:
        return err
    payload = data.model_dump()
    try:
        slots = json.loads(data.slots_json or "{}")
        if not isinstance(slots, dict):
            raise ValueError
    except (TypeError, ValueError):
        return format_data.fail("slots_json 必须是 JSON 对象")
    payload["slots"] = slots
    result = business_action_logic.prepare_business_action(user, payload)
    return format_data.ok(result, result.get("message", "动作已准备"))


@router.post("/execute", summary="通用业务动作执行：消费确认令牌后写库")
def execute_action(data: ActionExecuteData):
    user, err = _actor(data)
    if err:
        return err
    result = business_action_logic.execute_business_action(
        user, data.confirmation_token, data.visitor_key
    )
    return format_data.ok(result, result.get("message", "动作已执行"))
