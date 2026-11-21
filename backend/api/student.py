"""学生端接口：所有数据归属都从登录 token 获取，不接受客户端冒充其他学生。"""
import json

from fastapi import APIRouter, Header
from pydantic import BaseModel
from service import student_logic
from service import lifecycle
from service import leave_chat_logic
from utils import format_data
from api.auth import require_user, request_token
from db import get_db

router = APIRouter()

class LeaveData(BaseModel):
    token: str
    reason: str
    start_time: str = ""
    end_time: str = ""


class PrepareLeaveData(BaseModel):
    token: str
    query: str = ""
    action: str = "collect"
    leave_type: str = ""
    start_date: str = ""
    end_date: str = ""
    date_text: str = ""
    reason: str = ""


class ConfirmPreparedLeaveData(BaseModel):
    token: str
    confirmation_token: str


class EmotionData(BaseModel):
    token: str
    text: str
    emotion_tag: str = ""
    emotion_score: int | None = None
    risk_level: str = ""

class FeedbackData(BaseModel):
    token: str
    content: str
    detail: str = ""


def _student(token):
    return require_user(token, ("STUDENT",))


@router.post("/leave/prepare", summary="Dify 请假编排：准备并归一化草稿")
def prepare_leave(data: PrepareLeaveData):
    """只校验和准备草稿，不创建正式请假单。"""
    user, err = _student(data.token)
    if err:
        return err
    result = leave_chat_logic.prepare_leave_from_extraction(
        user, data.model_dump()
    )
    return format_data.ok(result, result.get("message", "请假草稿已处理"))


@router.post("/leave/confirm", summary="Dify 请假编排：确认后正式提交")
def confirm_prepared_leave(data: ConfirmPreparedLeaveData):
    """只有持有效确认令牌才能调用，最终权限、日期和重复校验仍在本层执行。"""
    user, err = _student(data.token)
    if err:
        return err
    if not data.confirmation_token.strip():
        return format_data.fail("缺少请假确认令牌，请重新确认请假内容")
    result = leave_chat_logic.confirm_prepared_leave(
        user, data.confirmation_token
    )
    return format_data.ok(result, result.get("message", "请假处理完成"))


@router.post("/leave", summary="提交请假申请")
def add_leave(data: LeaveData):
    user, err = _student(data.token)
    if err:
        return err
    if not data.reason.strip():
        return format_data.fail("请假理由不能为空")
    # 门禁：只有在读/已入学学生才有请假这项权利
    allowed, msg = lifecycle.check(user["id"], "leave")
    if not allowed:
        return format_data.forbidden(msg, lifecycle.get_stage_info(user["id"]))
    try:
        result = student_logic.handle_leave(
            user["id"], data.reason, data.start_time, data.end_time
        )
    except ValueError as exc:
        return format_data.fail(str(exc))
    if result.get("duplicate"):
        return format_data.conflict(
            "相同请假申请已存在，待审批单号：" + str(result.get("id"))
        )
    return format_data.ok(result, "请假提交成功")

@router.post("/emotion", summary="提交情绪倾诉")
def add_emotion(data: EmotionData):
    user, err = _student(data.token)
    if err:
        return err
    allowed, deny_msg = lifecycle.check(user["id"], "emotion")
    if not allowed:
        return format_data.forbidden(deny_msg)
    if not data.text.strip():
        return format_data.fail("情绪内容不能为空")
    result = student_logic.handle_emotion(
        user["id"], data.text,
        emotion_score=data.emotion_score,
        emotion_tag=data.emotion_tag,
        risk_level=data.risk_level,
    )
    return format_data.ok(result)

@router.get("/my_leaves", summary="查询我的请假记录")
def my_leaves(status: str = "", authorization: str = Header(default=""), token: str = ""):
    user, err = _student(request_token(authorization, token))
    if err:
        return err
    try:
        result = student_logic.get_my_leaves(user["id"], status or None)
        return format_data.ok(result)
    except Exception as e:
        print("查询请假失败:", repr(e))
        return format_data.server_error()

@router.post("/feedback", summary="提交售后反馈")
def feedback(data: FeedbackData):
    user, err = _student(data.token)
    if err:
        return err
    if not data.content.strip():
        return format_data.fail("反馈内容不能为空")
    # 门禁：售后是已入学学生的服务
    allowed, msg = lifecycle.check(user["id"], "feedback")
    if not allowed:
        return format_data.forbidden(msg, lifecycle.get_stage_info(user["id"]))
    result = student_logic.submit_feedback(user["id"], data.content, data.detail)
    return format_data.ok(result, "反馈已提交")

# 新增：学生查看自己的学籍阶段与已解锁服务
@router.get("/stage", summary="我的学籍阶段与已解锁服务")
def my_stage(authorization: str = Header(default=""), token: str = ""):
    user, err = _student(request_token(authorization, token))
    if err:
        return err
    return format_data.ok(lifecycle.get_stage_info(user["id"]))

# === 下面两个是补充的 ===
@router.get("/my_feedbacks", summary="查询我的售后工单")
def my_feedbacks(status: str = "", authorization: str = Header(default=""), token: str = ""):
    user, err = _student(request_token(authorization, token))
    if err:
        return err
    try:
        res = student_logic.get_my_feedbacks(user["id"], status or None)
        return format_data.ok(res)
    except Exception as e:
        print("查询售后失败:", repr(e))
        return format_data.server_error()

@router.get("/my_scores", summary="查询我的成绩")
def my_scores(authorization: str = Header(default=""), token: str = ""):
    user, err = _student(request_token(authorization, token))
    if err:
        return err
    try:
        res = student_logic.get_my_scores(user["id"])
        return format_data.ok(res)
    except Exception as e:
        print("查询成绩失败:", repr(e))
        return format_data.server_error()


@router.get("/psych-profile", summary="查询我的心理画像")
def psych_profile(authorization: str = Header(default=""), token: str = ""):
    user, err = _student(request_token(authorization, token))
    if err:
        return err
    allowed, deny_msg = lifecycle.check(user["id"], "emotion")
    if not allowed:
        return format_data.forbidden(deny_msg)
    try:
        return format_data.ok(student_logic.get_emotion_records(user["id"]))
    except Exception as exc:
        print("查询心情记录失败:", repr(exc))
        return format_data.server_error()
