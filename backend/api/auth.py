"""
ai - 页面登录 + 学生注册 + Dify 智能体代理
=========================================
本文件为新增/改造文件，不触碰项目原有的 student/employee/customer/admin 业务逻辑。

职责：
1. /api/auth/register      学生注册（真实密码，PBKDF2 哈希存储）
2. /api/auth/login         页面登录（账号来自 sys_user 表，校验真实密码）
3. /api/auth/me            校验登录状态
4. /api/auth/verify-admin  后台权限校验（仅 EMPLOYEE 可进）
5. /api/auth/dify-chat     代理调用 Dify（学生/员工，携带登录用户ID）
6. /api/auth/public-chat   客服与活动公开聊天（免登录，走系统访客账号）
7. /api/auth/setup-status  是否需要首次初始化（库里还没有员工账号）
8. /api/auth/setup         首次初始化：创建第一个管理员账号（仅在无员工时可用）
9. /api/auth/staff         员工账号管理：列表 / 新建 / 停用

运行模式：**无演示账号**。系统启动后库内没有任何可登录账号，必须先通过
`/setup`（后台登录页的“首次初始化”向导）创建管理员，学生则自行注册。

配置（环境变量，可选）：
  YUEJIAO_TOKEN_SECRET   登录令牌签名密钥
  DIFY_API_BASE          Dify Service API 地址，默认 https://api.dify.ai/v1
  DIFY_KEY_STUDENT / DIFY_KEY_EMPLOYEE / DIFY_KEY_CUSTOMER  三个应用的 API Key
"""

import os
import json
import re
import time
import hmac
import hashlib
import base64
import secrets
import sqlite3
import urllib.request
import urllib.error
from datetime import date, datetime

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel

from db import get_db
from utils import format_data
from utils.rate_limit import rate_limit
from service import emotion_logic
from service import business_action_logic
from service import employee_chat_logic
from service import event_chat_logic
from service import feedback_chat_logic
from service import lifecycle
from service import leave_chat_logic
from service import lead_chat_logic
from service import rbac
from service import rule_logic
from service import score_chat_logic
from service.student_logic import log_conversation

router = APIRouter()


def _is_recommendation_query(query: str) -> bool:
    """判断是否在询问留学项目推荐。

    活动、员工福利和知识问答不能被误判为项目推荐。只有出现推荐表达，
    或同时包含画像线索与升学/就业/移民目标时才启用确定性直答兜底。
    """
    text = (query or "").strip()
    if not text or any(word in text for word in (
        "活动", "讲座", "宣讲会", "展会", "研学", "员工福利", "团建",
    )):
        return False
    strong = any(word in text for word in (
        "推荐", "适合我", "适合什么", "有什么方案", "哪个方案",
        "有什么项目", "有哪些项目", "项目推荐",
        "怎么选", "读什么", "学什么", "留学方案推荐",
        "拿本科", "读本科", "升本科", "申请本科",
        "读硕士", "升硕士", "申请硕士",
    ))
    profile = rule_logic.extract_profile(text)
    intent_signal = any(word in text for word in (
        "就业", "工作", "移民", "永居", "升学", "学历", "留学", "本科", "硕士",
    ))
    # 单独的“本科/专科”可能只是费用、课程等主题词，不能当作学历画像。
    # 只有在出现年龄，或明确写了“本科在读/本科毕业”等学籍语境时，
    # 才把学历字段用于推荐兜底。这样“本科学费多少”仍会走知识库。
    education_context = any(word in text for word in (
        "在读", "毕业", "读完", "学历是",
    ))
    profile_signal = bool(
        profile.get("age")
        or (profile.get("current_education") and education_context)
    )
    return strong or (profile_signal and intent_signal)


def _is_profile_only_query(query: str) -> bool:
    """判断用户是否只报年龄和学历，却还没有提出具体办理诉求。"""
    text = str(query or "").strip()
    profile = rule_logic.extract_profile(text)
    if not profile.get("age") or not profile.get("education"):
        return False
    explicit_topics = (
        "政策", "签证", "机会卡", "蓝卡", "永居", "移民", "费用", "学费",
        "流程", "材料", "活动", "成绩", "请假", "项目", "方案", "推荐",
        "适合", "专业", "学校", "院校", "国家", "就业", "工作", "升学",
        "本科", "硕士", "博士",
    )
    # “博士”在画像本身中出现，不算新的业务主题；其余主题词出现时仍交给
    # Dify/知识库回答，不能因为句子里有年龄就全部拦截。
    topic_text = text.replace("博士", "").replace("硕士", "").replace("本科", "")
    return not any(word in topic_text for word in explicit_topics if word not in (
        "博士", "硕士", "本科",
    ))


def _profile_only_answer(query: str) -> str:
    """只报画像时先确认目标，不擅自判断签证资格或推荐项目。"""
    profile = rule_logic.extract_profile(query)
    age = profile.get("age")
    education = profile.get("education") or ""
    if education == "博士":
        return (
            f"收到，你现在是 {age} 岁、博士学历。\n\n"
            "我先不替你猜要申请什么。公司目前有德国工作和居留政策资料，"
            "但目前没有面向博士或博士后的现成升学项目。\n\n"
            "你更想了解职业转型、海外工作发展、补充某个专业能力，"
            "还是只要查德国相关政策？告诉我具体目标后，我再结合公司现有政策"
            "和项目帮你梳理。"
        )
    return (
        f"收到，你现在是 {age} 岁、{education}学历。\n\n"
        "你更想解决哪件事：继续升学、就业、海外发展，还是查询某个国家或项目的"
        "具体信息？告诉我目标后，我再结合公司现有政策、项目和业务资料给你"
        "具体答案，不会先替你下结论。"
    )


def _direct_recommendation_answer(query: str, audience: str) -> str:
    """用确定性规则生成不会模棱两可的推荐直答。"""
    # 延迟导入，避免 auth 与 recommend 路由在模块加载时互相依赖。
    from api.recommend import RecommendData, recommend as recommend_api

    payload = recommend_api(RecommendData(raw_text=query))
    result = payload.get("data") or {}
    best = result.get("best_match") or {}
    if result.get("status") == "match" and best:
        lines = [
            "推荐【" + best.get("project", "") + "】，匹配度 "
            + str(best.get("score", "")) + " 分。"
        ]
        reason_parts = [
            part for part in str(best.get("reason", "")).split("；")
            if part and not part.startswith("条件推荐")
        ]
        for index, reason in enumerate(reason_parts[:2], 1):
            lines.append(str(index) + ". " + reason)
        caveats = best.get("caveats") or []
        if caveats:
            lines.append("前置条件：" + "；".join(caveats) + "。")
        target = str(best.get("audience") or "").strip()
        if target:
            lines.append("适合人群：" + target[:120] + "。")
        description = re.sub(r"\s+", " ", str(best.get("description") or ""))
        clauses = [
            clause.strip(" 。；|")
            for clause in re.split(r"[。；|]", description)
            if clause.strip(" 。；|")
        ]
        highlights = [
            clause[:100] for clause in clauses
            if any(word in clause for word in ("学制", "年", "学费", "费用", "招生", "录取"))
        ][:2]
        if highlights:
            lines.append("方案要点：" + "；".join(highlights) + "。")
        if audience == "student":
            lines.append("你可以到左侧「留学方案」板块查看全部具体方向，并一键带入就读申请。")
        else:
            lines.append("你可以到官网「留学方案」板块查看具体方向，也可以留下联系方式让顾问确认条件。")
        return "\n".join(lines)
    profile = result.get("profile") or {}
    if result.get("status") in ("no_match", "match") and not best:
        education = str(profile.get("education") or "")
        age = profile.get("age")
        if education == "博士":
            return (
                "以你的背景，我不会硬推“专升本”或“本升硕”这类项目，"
                "那对你的学历提升没有实际意义。\n\n"
                "公司目前提供的升学项目主要面向初中、高中/中职、专科和本科起点，"
                "暂时没有博士或博士后方向。" + str(age) + " 岁博士来咨询，目标通常更像是"
                "职业转型、海外工作发展或补充某个专业能力。\n\n"
                "你更看重哪一条？比如转行就业、去海外长期发展，还是补充某一领域的"
                "学习经历。告诉我目标后，我再结合公司现有项目帮你筛选；如果确实没有合适"
                "项目，我会直接说明，也不会为了凑数推荐不匹配的项目。"
            )
        detail = (
            "目前更像是个别条件没有命中现有项目，而不是你没有选择。"
            if age else
            "目前更像是个别条件没有命中现有项目。"
        )
        return (
            "按你现在提供的信息，我没有找到能够负责任推荐的具体项目。"
            + detail + "\n\n"
            "你可以再告诉我三件事：当前最高学历、最看重就业/移民/继续升学中的哪一项，"
            "以及是否接受德语学习。我再帮你细筛；如果仍然没有，就直接建议顾问人工评估，"
            "不会硬套不合适的方案。"
        )
    if result.get("status") == "insufficient":
        missing = []
        if not profile.get("age"):
            missing.append("年龄")
        if not profile.get("education"):
            missing.append("当前学历")
        if not missing:
            # 画像字段已经齐全但方案目录仍未返回可匹配结果时，不应继续追问
            # 用户重复填写同一信息。明确说明未命中并给出下一步，避免死循环。
            return "已收到你的年龄和学历，但公司现有项目暂时没有完全匹配的方向，"
            "请联系顾问人工评估；也可以到" + (
                "左侧「留学方案」" if audience == "student" else "官网「留学方案」板块"
            ) + "查看其他具体项目。"
        return "请补充" + "和".join(missing) + "，我会直接给出最匹配的具体项目；也可以先到" + (
            "左侧「留学方案」" if audience == "student" else "官网「留学方案」板块"
        ) + "查看全部方案。"
    return "公司现有项目暂时没有完全匹配的方案，请联系顾问人工评估；也可以到" + (
        "左侧「留学方案」" if audience == "student" else "官网「留学方案」板块"
    ) + "查看其他具体项目。"


def _is_scheduled_event_query(query: str) -> bool:
    """判断是否是“近期可报名活动”查询。

    公司内部员工活动、团建、研学和知识库里的通用文化交流介绍不属于
    event_lecture 运营活动，必须排除，继续走知识库。
    """
    text = (query or "").strip()
    excluded = ("员工活动", "团建", "研学", "文化交流", "内部活动")
    if not text or any(word in text for word in excluded):
        return False
    event_words = ("活动", "讲座", "宣讲", "展会", "见面会", "联展", "分享会")
    if any(word in text for word in event_words):
        return True
    # “我要报名”单独出现时通常是在问可报名活动，先给出当前活动列表，
    # 不进入知识库检索公司内部员工活动。
    return "报名" in text and any(
        word in text
        for word in ("留学", "课程", "项目")
    )


def _direct_event_answer(query: str = "") -> str:
    """直接读取运营活动表，保证后台新增后下一次提问立即出现。"""
    conn = get_db()
    try:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM event_lecture ORDER BY start_time ASC, id ASC"
        ).fetchall()]
    finally:
        conn.close()

    today = date.today().isoformat()
    upcoming = [
        row for row in rows
        if not row.get("start_time") or str(row["start_time"])[:10] >= today
    ]
    if not upcoming:
        return "当前没有已发布的可报名活动。后续活动发布后，可以直接问我最新安排。"

    registration_intent = "报名" in (query or "")
    lines = [
        "当前可报名的活动如下："
        if registration_intent else "近期已发布活动如下："
    ]
    for index, row in enumerate(upcoming[:5], 1):
        maximum = int(row.get("max_participants") or 0)
        current = int(row.get("current_participants") or 0)
        seats = "不限名额" if maximum <= 0 else (
            "名额已满" if current >= maximum else "剩余 " + str(maximum - current) + " 个名额"
        )
        details = [
            str(row.get("start_time") or "时间待定"),
            str(row.get("location") or "地点待定"),
            seats,
        ]
        lines.append(
            str(index) + ". 「" + str(row.get("event_name") or "活动") + "」"
            + "，".join(details)
        )
    if registration_intent:
        lines.append(
            "请选择要报名的场次，并点击对应活动卡片的「立即报名」填写姓名和联系方式；"
            "提交后会立即生成报名记录。"
        )
    else:
        lines.append("你可以在官网「最新活动」或对应活动报名页面直接报名。")
    return "\n".join(lines)


def _clean_answer(text: str) -> str:
    """清洗大模型回复：去掉思考过程、工具调用痕迹和多余空行，只留可以直接读的答案。

    这一层是最后一道闸门。模型偶尔仍会自己加「思考过程」「正在调用工具」
    或残留 <think> 标签，前端按富文本渲染时会把这些暴露给用户，看起来很不专业。
    """
    if not text:
        return ""

    # 1) 成对的思考标签（不同厂商写法不一：think / thinking / reasoning / analysis）
    text = re.sub(
        r"<(think|thinking|reasoning|analysis)>.*?</\1>",
        "", text, flags=re.S | re.I,
    )
    # 2) 未闭合的思考标签：说明回复被截断，标签后的内容全是过程，直接丢弃
    text = re.sub(r"<(think|thinking|reasoning|analysis)>.*$", "", text, flags=re.S | re.I)
    # 3) 模型自己写的小标题式思考段，如「### 思考过程」「**推理：**」
    text = re.sub(
        r"(?im)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*{0,2})(?:思考过程|推理过程|分析过程|思考|推理|分析|"
        r"工具调用|调用工具|执行步骤|内部思考)(?:\*{0,2})[ \t]*[:：]?[ \t]*$",
        "", text,
    )
    # 4) 工具调用 / 数据来源的过程性句子
    text = re.sub(
        r"(?im)^[ \t>]*(?:[-*·]\s*)?(?:正在|马上|先)?(?:调用|使用|执行)(?:一下)?(?:工具|接口|函数)[^\n]*$",
        "", text,
    )
    text = re.sub(
        r"(?im)^[ \t>]*(?:[-*·]\s*)?(?:工具返回|工具结果|接口返回|查询结果如下|数据库返回)[^\n]*$",
        "", text,
    )
    # 5) 「作为 AI」「我是一个大语言模型」这类自我暴露
    text = re.sub(r"(?im)^[^\n]*(?:作为(?:一个)?\s*(?:AI|人工智能|大模型|语言模型)|我是一个?(?:AI|人工智能|大语言模型))[^\n]*$[ \t]*\n?", "", text)
    # 6) 折叠 3 个以上连续空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ---------------- 配置 ----------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_token_secret() -> str:
    """令牌密钥：优先环境变量，其次 backend/.token_secret（首次自动生成并落盘）。

    不再使用写死的默认密钥 —— 那等于把所有人的登录令牌都能伪造出来。
    """
    env = os.getenv("YUEJIAO_TOKEN_SECRET")
    if env:
        return env
    path = os.path.join(BASE_DIR, ".token_secret")
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved
        generated = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(generated)
        return generated
    except Exception:
        # 目录不可写时退化为进程内随机密钥（重启后需重新登录，但不影响安全）
        return secrets.token_hex(32)


TOKEN_SECRET = _load_token_secret()
TOKEN_TTL = 60 * 60 * 12

# 与业务相关的两个特殊账号名
GUEST_USERNAME = "_guest"      # 系统访客，仅供客服对话记录落库，不设密码、无法登录

# Dify 配置：优先环境变量，其次同目录的 dify_config.json，最后云端默认值。
def _load_dify_config():
    cfg = {"api_base": "", "keys": {}}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 本地真实密钥放在忽略文件里；可提交配置只保留空占位。
    for name in ("dify_config.local.json", "dify_config.json"):
        path = os.path.join(root, name)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if data.get("api_base") or data.get("keys"):
                cfg["api_base"] = data.get("api_base", "")
                cfg["keys"] = data.get("keys", {})
                break
        except Exception:
            continue
    return cfg


_DIFY_CFG = _load_dify_config()
DIFY_API_BASE = os.getenv("DIFY_API_BASE", _DIFY_CFG["api_base"] or "https://api.dify.ai/v1").rstrip("/")
DIFY_KEYS = {
    "STUDENT": os.getenv("DIFY_KEY_STUDENT", _DIFY_CFG["keys"].get("STUDENT", "")),
    "EMPLOYEE": os.getenv("DIFY_KEY_EMPLOYEE", _DIFY_CFG["keys"].get("EMPLOYEE", "")),
    "CUSTOMER": os.getenv("DIFY_KEY_CUSTOMER", _DIFY_CFG["keys"].get("CUSTOMER", "")),
}
ROLE_LABEL = {
    "STUDENT": "学生",
    "TEACHER": "老师",
    "EMPLOYEE": "员工",
    "CUSTOMER": "客户",
    "ADMIN": "管理员",
    "MAINTAINER": "维护人员",
}

# 可登录后台的全部角色；MAINTAINER 只能看运行状态。
STAFF_ROLES = ("EMPLOYEE", "TEACHER", "ADMIN", "MAINTAINER")
# 可执行业务操作的角色，维护人员被排除。
BUSINESS_ROLES = ("EMPLOYEE", "TEACHER", "ADMIN")
EMPLOYEE_SUBROLES = ("ADVISOR", "HOMEROOM", "LEADER")
STAFF_SUBROLE_LABEL = {
    "ADVISOR": "顾问",
    "HOMEROOM": "班主任",
    "LEADER": "领导",
}
ROLE_PREVIEWS = ("ADMIN", "ADVISOR", "HOMEROOM", "LEADER", "MAINTAINER")
ROLE_PREVIEW_LABEL = {
    "ADMIN": "管理员",
    "LEADER": "领导",
    "ADVISOR": "顾问",
    "HOMEROOM": "班主任",
    "MAINTAINER": "维护人员",
}
ROLE_GROUP = {
    "ADMIN": ("ADMIN", "管理角色"),
    "ADVISOR": ("EMPLOYEE", "员工"),
    "HOMEROOM": ("EMPLOYEE", "员工"),
    "LEADER": ("EMPLOYEE", "员工"),
    "MAINTAINER": ("MAINTAINER", "维护角色"),
}


# ---------------- 密码哈希（标准库实现，无第三方依赖） ----------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return "pbkdf2_sha256$120000$" + salt.hex() + "$" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        alg, iters, salt_hex, hash_hex = stored.split("$")
        if alg != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def _ensure_schema():
    """兼容旧库：sys_user 没有 password_hash 时自动加列。"""
    conn = get_db()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sys_user)").fetchall()]
        if "password_hash" not in cols:
            conn.execute("ALTER TABLE sys_user ADD COLUMN password_hash TEXT")
            conn.commit()
    finally:
        conn.close()


def _staff_count(conn) -> int:
    """当前可用（未停用）的员工账号数量，用于判断是否需要首次初始化。"""
    marks = ",".join("?" * len(STAFF_ROLES))
    return conn.execute(
        "SELECT COUNT(*) FROM sys_user WHERE user_type IN (%s) AND status='正常'" % marks,
        STAFF_ROLES,
    ).fetchone()[0]


def _admin_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sys_user WHERE user_type='ADMIN' AND status='正常'"
    ).fetchone()[0]


def ensure_guest():
    """取系统访客账号 id；不存在则创建（无密码，不可登录）。

    公开客服的对话需要落库到某个 CUSTOMER 用户，但线上不允许存在“演示客户”，
    因此用一个隐藏账号 `_guest` 承接，既不影响账号体系，也不会被误当成真人。
    """
    _ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM sys_user WHERE username = ? AND user_type='CUSTOMER'",
            (GUEST_USERNAME,),
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute(
            "INSERT INTO sys_user (username, real_name, user_type, contact_info, status) "
            "VALUES (?, '访客', 'CUSTOMER', '', '正常')",
            (GUEST_USERNAME,),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ---------------- 令牌（无第三方依赖，HMAC 签名） ----------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(user: dict, view_role: str = "", view_uid: int = 0) -> str:
    """签发登录令牌。

    普通登录只记录真实账号。管理员进入“演示角色视图”时，额外记录
    view_role 与 view_uid，但令牌主体仍是真实管理员，因此切换角色不需要
    重新登录，也不会把管理员密码或目标员工密码暴露给前端。
    """
    payload = {
        "uid": user["id"],
        "utype": user["user_type"],
        "ver": int(user.get("token_version") or 0),
        "exp": int(time.time()) + TOKEN_TTL,
    }
    if view_role:
        payload["vr"] = str(view_role).upper()
        payload["vuid"] = int(view_uid or user["id"])
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def parse_token(token: str):
    try:
        body, sig = token.split(".", 1)
        expect = _b64e(hmac.new(TOKEN_SECRET.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        payload = json.loads(_b64d(body).decode("utf-8"))
        if payload.get("exp", 0) < int(time.time()):
            return None
        if "ver" not in payload:
            return None
        return payload
    except Exception:
        return None


def _build_view_user(actual_row, target_row, view_role: str) -> dict:
    """构造当前请求使用的演示视图身份。

    actual_* 字段始终指向真实管理员，id/user_type/staff_role 指向当前角色
    所代表的数据账号。这样切换后数据归属和 Dify 调用都按所选角色执行，
    但令牌签名主体仍是真实管理员。
    """
    actual = dict(actual_row)
    target = dict(target_row or actual_row)
    view_role = (view_role or "").upper()
    target["actual_user_id"] = actual["id"]
    target["actual_user_type"] = actual["user_type"]
    target["actual_staff_role"] = actual.get("staff_role") or ""
    target["view_role"] = view_role
    target["token_version"] = actual.get("token_version") or 0
    if view_role in ("ADVISOR", "HOMEROOM", "LEADER"):
        target["user_type"] = "EMPLOYEE"
        target["staff_role"] = view_role
    elif view_role == "MAINTAINER":
        target["user_type"] = "MAINTAINER"
        target["staff_role"] = ""
    else:
        target["user_type"] = "ADMIN"
        target["staff_role"] = "LEADER"
    return target


def _user_payload(row, view_role: str = "", view_row=None) -> dict:
    """生成登录/切换角色响应；绝不返回密码或密码哈希。"""
    actual = dict(row)
    user = _build_view_user(row, view_row, view_role) if view_role else actual
    active_role = view_role or rbac.effective_role(user)
    if view_role:
        role_label = ROLE_PREVIEW_LABEL.get(view_role, view_role)
    elif user["user_type"] == "EMPLOYEE":
        role_label = STAFF_SUBROLE_LABEL.get(user.get("staff_role") or "", "员工")
    else:
        role_label = ROLE_LABEL.get(user["user_type"], user["user_type"])
    payload = {
        "token": make_token(actual, view_role, user["id"]),
        "user_id": user["id"],
        "user_type": user["user_type"],
        "role_label": role_label,
        "real_name": user["real_name"] or user["username"],
        "username": user["username"],
        "active_role": active_role,
        "active_role_label": ROLE_PREVIEW_LABEL.get(
            active_role, ROLE_LABEL.get(active_role, active_role)
        ),
        "can_switch_role": actual["user_type"] == "ADMIN",
        "is_role_preview": bool(view_role and view_role != "ADMIN"),
        "actual_user_id": actual["id"],
        "actual_user_type": actual["user_type"],
        # 前端菜单与后端接口使用同一套权限码；后端仍逐接口校验。
        "permissions": rbac.capabilities_for(user),
    }
    role_group, role_group_label = ROLE_GROUP.get(
        active_role, (user["user_type"], ROLE_LABEL.get(user["user_type"], ""))
    )
    payload["role_group"] = role_group
    payload["role_group_label"] = role_group_label
    if user["user_type"] in STAFF_ROLES:
        staff_role = user.get("staff_role") or ""
        payload["staff_role"] = staff_role
        payload["staff_role_label"] = STAFF_SUBROLE_LABEL.get(staff_role, "员工")
        payload["department"] = user.get("department") or ""
    if user["user_type"] == "STUDENT":
        stage = "CONSULT"
        if user.get("lifecycle_stage"):
            stage = user["lifecycle_stage"].strip().upper()
        if stage not in lifecycle.STAGE_INDEX:
            stage = "CONSULT"
        payload["lifecycle_stage"] = stage
        payload["stage_label"] = lifecycle.STAGE_LABEL[stage]
        # 登录响应直接带能力矩阵，前端第一帧即可锁定未开放模块，
        # 不必等概览接口回来后才补锁定，减少短暂越权入口。
        payload["lifecycle"] = lifecycle.get_stage_info(user["id"])
    return payload


def _extract_bearer(authorization: str = "") -> str:
    value = (authorization or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def current_user(token: str):
    """校验签名后再查库，确保停用账号、改密后的旧令牌立即失效。"""
    payload = parse_token(token)
    if not payload:
        return None, format_data.unauthorized()
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM sys_user WHERE id=?", (payload["uid"],)).fetchone()
        if not row:
            return None, format_data.unauthorized("用户不存在，请重新登录")
        if row["status"] != "正常":
            return None, format_data.forbidden("账号已停用，请联系管理员")
        stored_ver = int(row["token_version"] or 0) if "token_version" in row.keys() else 0
        if stored_ver != int(payload.get("ver", 0)):
            return None, format_data.unauthorized("登录状态已失效，请重新登录")
        if row["user_type"] != payload["utype"]:
            return None, format_data.unauthorized("账号角色已变化，请重新登录")
        view_role = str(payload.get("vr") or "").upper()
        if view_role:
            if row["user_type"] != "ADMIN" or view_role not in ROLE_PREVIEWS:
                return None, format_data.unauthorized("演示角色令牌无效，请重新登录")
            target = conn.execute(
                "SELECT * FROM sys_user WHERE id=?",
                (int(payload.get("vuid") or row["id"]),),
            ).fetchone()
            if target and target["status"] != "正常":
                return None, format_data.forbidden("当前演示角色代表账号已停用")
            return _build_view_user(row, target or row, view_role), None
        return dict(row), None
    finally:
        conn.close()


def require_user(token: str, roles=None):
    user, err = current_user(token)
    if err:
        return None, err
    if roles and user["user_type"] not in tuple(roles):
        return None, format_data.forbidden("当前账号无权执行此操作")
    return user, None


def require_staff(token: str):
    return require_user(token, BUSINESS_ROLES)


def require_backoffice(token: str):
    return require_user(token, STAFF_ROLES)


def require_admin(token: str):
    return require_user(token, ("ADMIN",))


def require_actual_admin(token: str):
    """要求真实登录账号是管理员，忽略当前演示视图。"""
    user, err = current_user(token)
    if err:
        return None, err
    actual_type = user.get("actual_user_type") or user.get("user_type")
    if actual_type != "ADMIN":
        return None, format_data.forbidden("只有管理员可使用演示角色切换")
    return user, None


def _role_representative(conn, actual_row, role: str):
    """为演示角色选择一个可用代表账号。

    优先使用真实的顾问/班主任/领导账号，以展示真实的数据范围。如果环境
    刚初始化还没有对应员工，则回退到管理员账号本身，但仍以所选角色的权限
    运行；此时看不到未分配数据是正常的。
    """
    role = (role or "").upper()
    actual = dict(actual_row)
    if role == "ADMIN":
        return actual_row
    if role == "MAINTAINER":
        return conn.execute(
            "SELECT * FROM sys_user WHERE user_type='MAINTAINER' "
            "AND status='正常' ORDER BY id LIMIT 1"
        ).fetchone() or actual_row
    if role in ("ADVISOR", "HOMEROOM", "LEADER"):
        return conn.execute(
            "SELECT * FROM sys_user WHERE user_type IN ('EMPLOYEE','TEACHER') "
            "AND staff_role=? AND status='正常' ORDER BY id LIMIT 1",
            (role,),
        ).fetchone() or actual_row
    return None


def require_business_subroles(token: str, subroles, allow_admin=True, allow_leader=True):
    """按员工子角色授权；管理员/领导默认拥有全部业务权限。"""
    user, err = require_staff(token)
    if err:
        return None, err
    role = user.get("staff_role") or "ADVISOR"
    if user["user_type"] == "ADMIN" and allow_admin:
        return user, None
    if role == "LEADER" and allow_leader:
        return user, None
    if role in tuple(subroles):
        return user, None
    return None, format_data.forbidden("当前员工角色无权执行此操作")


def require_capability(token: str, permission: str):
    """按统一权限矩阵校验功能权限。

    本函数只判断“这个角色能不能做这类事”，具体某条记录是否属于该员工，
    还必须由调用方继续做 student_scope 或 owner_id 校验。
    """
    user, err = require_backoffice(token)
    if err:
        return None, err
    if not rbac.has_capability(user, permission):
        return None, format_data.forbidden("当前角色无权执行此操作")
    return user, None


def staff_role(user):
    return rbac.effective_role(user)


def student_scope(user, alias="u"):
    """返回学生数据范围的 WHERE 片段与参数。"""
    return rbac.student_scope(user, alias)


def can_access_student(conn, user, student_id: int) -> bool:
    """检查学生是否在当前员工的数据范围内。

    管理员和领导可访问全部学生；班主任仅能访问本人班级；
    顾问仅能访问本人负责的学生。所有“按 ID 操作某学生”的接口都应调用它。
    """
    scope, args = student_scope(user)
    row = conn.execute(
        "SELECT 1 FROM sys_user u WHERE u.id=? AND u.user_type='STUDENT'" + scope,
        [student_id] + list(args),
    ).fetchone()
    return bool(row)


def request_token(authorization: str = "", legacy_token: str = "") -> str:
    """只允许通过 Authorization 头传令牌。

    legacy_token 参数保留是为了兼容旧函数签名，但不再读取查询参数值，
    避免令牌进入浏览器历史、代理日志和服务器访问日志。
    """
    del legacy_token
    return _extract_bearer(authorization)


def _legacy_business_fallback(user, query, history="", visitor_key=""):
    """Dify 不可用时复用旧的确定性业务处理器。"""
    score_result = score_chat_logic.handle_score_management(user, query, history)
    if score_result is not None:
        return {
            "answer": score_result["answer"],
            "changed": bool(score_result.get("changed")),
            "kind": "score",
        }
    feedback_result = feedback_chat_logic.handle_feedback_request(user, query)
    if feedback_result is not None:
        return {
            "answer": feedback_result["answer"],
            "changed": bool(feedback_result.get("changed")),
            "kind": "feedback",
        }
    event_result = event_chat_logic.handle_event_chat(user, query, visitor_key)
    if event_result is not None:
        return {
            "answer": event_result["answer"],
            "changed": bool(event_result.get("changed")),
            "kind": "event",
            "registration_id": event_result.get("registration_id", 0),
        }
    customer_result = lead_chat_logic.handle_customer_query(user, query)
    if customer_result is not None:
        return {
            "answer": customer_result["answer"],
            "changed": False,
            "kind": "lead",
        }
    if _is_scheduled_event_query(query):
        return {
            "answer": _direct_event_answer(query),
            "changed": False,
            "kind": "event_query",
        }
    return None


def _deterministic_read_only_result(user, query, history="", visitor_key=""):
    """明确查询类请求直接读库；写操作仍进入 Dify 确认编排。"""
    text = str(query or "")
    mutation_words = ("删除", "录入", "新增", "添加", "修改", "更新", "改成", "改为", "撤回", "撤销")
    score_context = any(word.lower() in text.lower() for word in score_chat_logic.SCORE_WORDS)
    history_has_score = any(
        word.lower() in str(history or "").lower()
        for word in score_chat_logic.SCORE_WORDS
    )
    if (score_context or history_has_score) and not any(
        word in text for word in mutation_words
    ):
        result = score_chat_logic.handle_score_management(user, text, history)
        if result is not None:
            return {
                "answer": result["answer"],
                "changed": False,
                "kind": "score_query",
            }
    if not any(word in text for word in ("撤回", "撤销", "删除", "取消")):
        feedback = feedback_chat_logic.handle_feedback_query(user, text)
        if feedback is not None:
            return {
                "answer": feedback["answer"],
                "changed": False,
                "kind": "feedback_query",
            }
    customer = lead_chat_logic.handle_customer_query(user, text)
    if customer is not None:
        return {
            "answer": customer["answer"],
            "changed": False,
            "kind": "customer_query",
        }
    if _is_scheduled_event_query(text):
        return {
            "answer": _direct_event_answer(text),
            "changed": False,
            "kind": "event_query",
        }
    return None


def _call_dify(role: str, query: str, uid, conversation_id: str = "",
               extra_inputs: dict | None = None, external_user: str = ""):
    """统一封装 Dify Service API 调用。"""
    id_var = {
        "STUDENT": "student_id",
        "TEACHER": "employee_id",
        "EMPLOYEE": "employee_id",
        "ADMIN": "employee_id",
        "CUSTOMER": "customer_id",
    }.get(role, "student_id")
    # 老师复用员工助手的 Dify Key（审批场景主要用业务接口，AI 问答走员工知识库）
    dify_role = "EMPLOYEE" if role in ("TEACHER", "ADMIN") else role
    api_key = DIFY_KEYS.get(dify_role)
    if not api_key:
        return format_data.fail(
            "尚未配置 " + dify_role + " 的 Dify API Key（环境变量 DIFY_KEY_" + dify_role + "）"
        )
    inputs = {id_var: int(uid)}
    if extra_inputs:
        inputs.update(extra_inputs)

    request_body = {
        "inputs": inputs,
        "query": query,
        "response_mode": "blocking",
        "user": (external_user or str(uid))[:80],
    }
    if conversation_id:
        request_body["conversation_id"] = conversation_id

    req = urllib.request.Request(
        DIFY_API_BASE + "/chat-messages",
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return format_data.ok(
            {
                "answer": _clean_answer(result.get("answer", "")),
                "conversation_id": result.get("conversation_id", ""),
                "message_id": result.get("message_id", ""),
                "user_id": uid,
                "orchestrator": "dify",
            }
        )
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")
        print("Dify HTTP 错误:", e.code, detail[:500])
        return format_data.fail("智能助手服务暂时不可用，请稍后重试")
    except Exception as e:
        print("Dify 调用异常:", repr(e))
        return format_data.server_error("智能助手服务暂时不可用，请稍后重试")


# ---------------- 请求模型 ----------------
class LoginData(BaseModel):
    username: str
    password: str


class RegisterData(BaseModel):
    username: str
    password: str
    real_name: str = ""
    contact_info: str = ""
    security_question: str
    security_answer: str


class TokenData(BaseModel):
    token: str


class SwitchRoleData(BaseModel):
    token: str
    role: str


class ChatData(BaseModel):
    token: str
    query: str
    conversation_id: str = ""
    # 员工端发送最近几轮用户问题，供成绩修改等多轮确定性业务使用。
    history: str = ""


class PublicChatData(BaseModel):
    query: str
    conversation_id: str = ""
    visitor: str = "guest"


class ChangePasswordData(BaseModel):
    token: str
    old_password: str
    new_password: str


class SetupData(BaseModel):
    """首次初始化：创建第一个管理员（员工）账号。"""
    username: str
    password: str
    real_name: str = ""
    contact_info: str = ""
    setup_code: str = ""     # 可选：部署时设置 YUEJIAO_SETUP_CODE，防止他人抢注管理员
    security_question: str = "你的出生城市是？"
    security_answer: str


class StaffData(BaseModel):
    """员工账号管理：新建 / 停用。"""
    token: str
    action: str = "create"   # create | disable | enable | reset_password
    username: str = ""
    password: str = ""
    real_name: str = ""
    contact_info: str = ""
    user_id: int = 0
    staff_role: str = "ADVISOR"
    department: str = ""
    manager_id: int = 0
    account_role: str = "EMPLOYEE"   # EMPLOYEE / ADMIN / MAINTAINER
    security_question: str = "你的出生城市是？"
    security_answer: str = ""


class ForgotPasswordData(BaseModel):
    username: str
    security_answer: str
    new_password: str


class RecoveryQuestionData(BaseModel):
    username: str


class SecurityQuestionData(BaseModel):
    token: str
    security_question: str
    security_answer: str


# ---------------- 接口 ----------------
@router.get("/setup-status", summary="是否需要首次初始化")
def setup_status():
    """公开接口：前端登录页据此决定显示“首次初始化向导”还是普通登录表单。"""
    conn = get_db()
    try:
        count = _staff_count(conn)
        admin_count = _admin_count(conn)
    finally:
        conn.close()
    return format_data.ok({
        "needs_setup": admin_count == 0,
        "staff_count": count,
        "require_code": bool(os.getenv("YUEJIAO_SETUP_CODE", "").strip()),
    })


@router.post("/setup", summary="首次初始化：创建第一个管理员账号")
def setup(data: SetupData, request: Request):
    """只在库里没有任何可用员工账号时开放，用于部署后的第一次建号。"""
    limited = rate_limit(request, "setup", 5, 60)
    if limited:
        return limited
    try:
        _ensure_schema()
        conn = get_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if _admin_count(conn) > 0:
                return format_data.fail("系统已有管理员，请直接登录")
            need_code = os.getenv("YUEJIAO_SETUP_CODE", "").strip()
            if need_code and data.setup_code.strip() != need_code:
                return format_data.fail("初始化口令不正确")
            username = data.username.strip()
            if len(username) < 3:
                return format_data.fail("账号至少 3 个字符")
            if len(data.password) < 8:
                return format_data.fail("管理员密码至少 8 位")
            if not data.security_answer.strip():
                return format_data.fail("请填写密码找回安全问题答案")
            if conn.execute("SELECT 1 FROM sys_user WHERE username = ?", (username,)).fetchone():
                return format_data.fail("该账号已存在，请换一个")
            cur = conn.execute(
                "INSERT INTO sys_user (username, real_name, user_type, contact_info, status, "
                "password_hash, staff_role, security_question, security_answer_hash) "
                "VALUES (?, ?, 'ADMIN', ?, '正常', ?, 'LEADER', ?, ?)",
                (username, data.real_name.strip() or "管理员",
                 data.contact_info.strip(), hash_password(data.password),
                 data.security_question.strip(), hash_password(data.security_answer.strip().lower())),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM sys_user WHERE id = ?", (cur.lastrowid,)).fetchone()
        finally:
            conn.close()
        return format_data.ok(_user_payload(row), "初始化完成，已自动登录")
    except sqlite3.IntegrityError:
        return format_data.conflict("初始化冲突，请检查账号是否已被创建")
    except Exception as e:
        print("初始化失败:", repr(e))
        return format_data.server_error()


@router.post("/register", summary="学生注册")
def register(data: RegisterData, request: Request):
    limited = rate_limit(request, "register", 10, 60)
    if limited:
        return limited
    try:
        _ensure_schema()
        username = data.username.strip()
        if not username or len(username) < 3:
            return format_data.fail("账号至少 3 个字符")
        if not data.password or len(data.password) < 6:
            return format_data.fail("密码至少 6 位")
        if not data.security_question.strip() or not data.security_answer.strip():
            return format_data.fail("请设置密码找回安全问题")
        conn = get_db()
        try:
            if conn.execute(
                "SELECT 1 FROM sys_user WHERE username = ?", (username,)
            ).fetchone():
                return format_data.fail("该账号已存在，请直接登录")
            cur = conn.execute(
                "INSERT INTO sys_user (username, real_name, user_type, contact_info, status, "
                "password_hash, security_question, security_answer_hash) "
                "VALUES (?, ?, 'STUDENT', ?, '正常', ?, ?, ?)",
                (
                    username,
                    data.real_name.strip() or username,
                    data.contact_info.strip(),
                    hash_password(data.password),
                    data.security_question.strip(),
                    hash_password(data.security_answer.strip().lower()),
                ),
            )
            conn.commit()
            uid = cur.lastrowid
            row = conn.execute("SELECT * FROM sys_user WHERE id = ?", (uid,)).fetchone()
        finally:
            conn.close()
        return format_data.ok(_user_payload(row), "注册成功")
    except sqlite3.IntegrityError:
        return format_data.conflict("账号已存在，请直接登录")
    except Exception as e:
        print("注册失败:", repr(e))
        return format_data.server_error()


@router.post("/login", summary="页面登录")
def login(data: LoginData, request: Request):
    limited = rate_limit(
        request, "login:" + data.username.strip().lower(), 8, 60
    )
    if limited:
        return limited
    try:
        _ensure_schema()
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM sys_user WHERE username = ?", (data.username.strip(),)
        ).fetchone()
        conn.close()

        if not row:
            return format_data.fail("账号或密码不正确")
        if row["status"] != "正常":
            return format_data.fail("账号已被停用，请联系管理员")
        stored = row["password_hash"]
        if not stored:
            # 系统访客等无密码账号一律禁止登录
            return format_data.fail("该账号未设置密码，请联系管理员重置")
        if not verify_password(data.password, stored):
            return format_data.fail("账号或密码不正确")

        return format_data.ok(_user_payload(row), "登录成功")
    except Exception as e:
        print("登录失败:", repr(e))
        return format_data.server_error()


@router.post("/recovery-question", summary="读取账号的密码找回安全问题")
def recovery_question(data: RecoveryQuestionData, request: Request):
    limited = rate_limit(
        request, "recovery:" + data.username.strip().lower(), 8, 60
    )
    if limited:
        return limited
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT security_question FROM sys_user WHERE username=? AND status='正常'",
            (data.username.strip(),),
        ).fetchone()
        if not row or not row["security_question"]:
            return format_data.not_found("该账号未设置密码找回问题，请联系管理员")
        return format_data.ok({"security_question": row["security_question"]})
    finally:
        conn.close()


@router.post("/forgot-password", summary="通过安全问题重置密码")
def forgot_password(data: ForgotPasswordData, request: Request):
    limited = rate_limit(
        request, "forgot:" + data.username.strip().lower(), 5, 60
    )
    if limited:
        return limited
    if len(data.new_password) < 6:
        return format_data.fail("新密码至少 6 位")
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, password_hash, security_answer_hash, user_type "
            "FROM sys_user WHERE username=? AND status='正常'",
            (data.username.strip(),),
        ).fetchone()
        if not row or not row["security_answer_hash"]:
            return format_data.fail("账号或安全问题答案不正确")
        if not verify_password(data.security_answer.strip().lower(), row["security_answer_hash"]):
            return format_data.fail("账号或安全问题答案不正确")
        minimum = 8 if row["user_type"] in STAFF_ROLES else 6
        if len(data.new_password) < minimum:
            return format_data.fail(f"新密码至少 {minimum} 位")
        conn.execute(
            "UPDATE sys_user SET password_hash=?, "
            "token_version=COALESCE(token_version,0)+1 WHERE id=?",
            (hash_password(data.new_password), row["id"]),
        )
        conn.commit()
        return format_data.ok(None, "密码已重置，请使用新密码登录")
    finally:
        conn.close()


@router.post("/set-security-question", summary="登录后设置密码找回问题")
def set_security_question(data: SecurityQuestionData):
    user, err = current_user(data.token)
    if err:
        return err
    if user.get("view_role"):
        return format_data.forbidden("演示角色视图下不能修改账号安全问题，请切回管理员视图")
    if not data.security_question.strip() or not data.security_answer.strip():
        return format_data.fail("问题和答案不能为空")
    conn = get_db()
    try:
        conn.execute(
            "UPDATE sys_user SET security_question=?, security_answer_hash=? WHERE id=?",
            (data.security_question.strip(),
             hash_password(data.security_answer.strip().lower()), user["id"]),
        )
        conn.commit()
        return format_data.ok(None, "密码找回问题已保存")
    finally:
        conn.close()


@router.get("/me", summary="校验登录状态")
def me(authorization: str = Header(default=""), token: str = ""):
    user, err = current_user(request_token(authorization, token))
    if err:
        return err
    return format_data.ok(
        {
            "user_id": user["id"],
            "user_type": user["user_type"],
            "role_label": ROLE_LABEL.get(user["user_type"], user["user_type"]),
            "real_name": user["real_name"] or user["username"],
        }
    )


@router.post("/verify-admin", summary="后台权限校验（仅员工）")
def verify_admin(data: TokenData):
    user, err = require_backoffice(data.token)
    if err:
        return err
    return format_data.ok(
        {"user_id": user["id"], "user_type": user["user_type"]}, "已授权"
    )


@router.get("/role-options", summary="管理员演示角色切换选项")
def role_options(authorization: str = Header(default=""), token: str = ""):
    """列出可切换角色及其代表账号，仅真实管理员可用。"""
    current, err = require_actual_admin(request_token(authorization, token))
    if err:
        return err
    actual_id = current.get("actual_user_id") or current["id"]
    conn = get_db()
    try:
        actual = conn.execute("SELECT * FROM sys_user WHERE id=?", (actual_id,)).fetchone()
        if not actual:
            return format_data.unauthorized("管理员账号不存在")
        options = []
        for role in ROLE_PREVIEWS:
            representative = _role_representative(conn, actual, role)
            role_group, role_group_label = ROLE_GROUP[role]
            options.append({
                "role": role,
                "label": ROLE_PREVIEW_LABEL[role],
                "group": role_group,
                "group_label": role_group_label,
                "representative": (representative["real_name"] or representative["username"])
                if representative else "",
                "available": bool(representative),
            })
        return format_data.ok(options)
    finally:
        conn.close()


@router.post("/switch-role", summary="管理员无密码切换演示角色")
def switch_role(data: SwitchRoleData):
    """在不重新登录的情况下切换管理员演示视图。"""
    current, err = require_actual_admin(data.token)
    if err:
        return err
    role = (data.role or "").strip().upper()
    if role not in ROLE_PREVIEWS:
        return format_data.fail("演示角色无效")
    actual_id = current.get("actual_user_id") or current["id"]
    conn = get_db()
    try:
        actual = conn.execute("SELECT * FROM sys_user WHERE id=?", (actual_id,)).fetchone()
        if not actual or actual["user_type"] != "ADMIN":
            return format_data.forbidden("真实登录账号不是管理员")
        representative = _role_representative(conn, actual, role)
        if not representative:
            return format_data.fail("当前没有可用的角色代表账号")
        # ADMIN 返回真实管理员身份；其他角色签发带视图声明的短令牌。
        view_role = "" if role == "ADMIN" else role
        payload = _user_payload(actual, view_role, representative)
        return format_data.ok(
            payload,
            "已切换到" + ROLE_PREVIEW_LABEL[role] + "视图",
        )
    finally:
        conn.close()


@router.post("/dify-chat", summary="代理调用 Dify 智能体（学生/员工，携带登录用户ID）")
def dify_chat(data: ChatData):
    user, err = current_user(data.token)
    if err:
        return err
    payload = {"uid": user["id"], "utype": user["user_type"]}
    if (payload["utype"] == "STUDENT"
            and _is_profile_only_query(data.query)
            and not leave_chat_logic.has_active_draft(payload["uid"])
            and not business_action_logic.has_active_action(
                user, "student:" + str(payload["uid"])
            )):
        answer = _profile_only_answer(data.query)
        log_conversation(payload["uid"], "student_agent", data.query, answer)
        return format_data.ok({
            "answer": answer,
            "changed": False,
            "conversation_id": "",
            "message_id": "",
            "user_id": payload["uid"],
        })

    # 员工的组织、带班范围、权限能力、学生名单、业绩汇总和客户状态
    # 都来自当前 SQLite 业务数据，必须在进入 Dify 前确定性分流。
    employee_result = employee_chat_logic.handle_employee_chat(
        user, data.query, data.history
    )
    if employee_result is not None:
        log_conversation(
            payload["uid"], "employee_agent", data.query,
            employee_result["answer"],
        )
        return format_data.ok({
            "answer": employee_result["answer"],
            "changed": bool(employee_result.get("changed")),
            "intent": employee_result.get("intent", ""),
            "conversation_id": "",
            "reset_conversation": True,
            "message_id": "",
            "user_id": payload["uid"],
        })

    if (
        not leave_chat_logic.has_active_draft(payload["uid"])
        and not business_action_logic.has_active_action(
            user, "student:" + str(payload["uid"])
        )
    ):
        read_only = _deterministic_read_only_result(
            user, data.query, data.history,
            "student:" + str(payload["uid"]),
        )
        if read_only is not None:
            log_conversation(
                payload["uid"],
                "student_agent" if payload["utype"] == "STUDENT" else "employee_agent",
                data.query, read_only["answer"],
            )
            return format_data.ok({
                "answer": read_only["answer"],
                "changed": False,
                "conversation_id": "",
                "reset_conversation": True,
                "message_id": "",
                "user_id": payload["uid"],
                "orchestrator": "backend_read_only",
            })

    # 关键修复：学生通过智能助手倾诉时，也要做情绪检测并触发老师预警。
    # 之前只有「心情记录」页会写预警表，导致学生在聊天里表达高危情绪时，
    # 老师后台的心理预警看板永远没有数据。
    emotion_result = None
    emotion_tag = ""
    emotion_history_allowed = False
    if payload["utype"] == "STUDENT":
        emotion_history_allowed, _ = lifecycle.check(payload["uid"], "emotion")
        try:
            # 非在读账号不能向心情画像落历史，但智能助手仍保留安全检测。
            # 如果真的出现高危内容，预警与通知继续执行，只不写心情记录。
            er = emotion_logic.record_emotion(
                payload["uid"], data.query, source="智能助手",
                persist_history=emotion_history_allowed,
            )
            emotion_result = er
            emotion_tag = er["tag"]
        except Exception:
            pass  # 情绪检测失败不影响正常问答

    # 把登录令牌一并传给 Dify：新增的接口（申请进度、活动报名、通知、审批、线索等）都要 token 鉴权，
    # 工作流的工具节点用 {{#start_node.auth_token#}} 引用。
    result = _call_dify(
        payload["utype"], data.query, payload["uid"], data.conversation_id,
        extra_inputs={
            "auth_token": data.token,
            # Dify 用它判断当前是否存在请假草稿，从而把“确认/修改/补充”
            # 继续送入请假编排分支，而不是误判成普通闲聊。
            "leave_pending": "1" if (
                payload["utype"] == "STUDENT"
                and leave_chat_logic.has_active_draft(payload["uid"])
            ) else "0",
            "action_pending": "1" if business_action_logic.has_active_action(
                user, "student:" + str(payload["uid"])
            ) else "0",
            "pending_action_type": business_action_logic.active_action_type(
                user, "student:" + str(payload["uid"])
            ),
            "recent_history": str(data.history or "")[-1200:],
        },
    )
    # 主链路必须走 Dify。只有 Dify 超时、报错或返回空答案时，才允许后端
    # 确定性请假处理器降级接管，保证服务可用但不改变正常架构。
    if (payload["utype"] == "STUDENT"
            and leave_chat_logic.should_handle_as_leave(payload["uid"], data.query)
            and not (
                isinstance(result, dict)
                and result.get("code") == 200
                and str((result.get("data") or {}).get("answer") or "").strip()
            )):
        fallback = leave_chat_logic.handle_leave_request(user, data.query)
        if fallback is not None:
            log_conversation(
                payload["uid"], "student_agent", data.query,
                fallback["answer"], "",
            )
            return format_data.ok({
                "answer": fallback["answer"],
                "changed": bool(fallback.get("changed")),
                "leave_changed": bool(fallback.get("changed")),
                "leave_duplicate": bool(fallback.get("duplicate")),
                "leave_awaiting_info": bool(fallback.get("awaiting_info")),
                "leave_awaiting_confirmation": bool(
                    fallback.get("awaiting_confirmation")
                ),
                "leave_id": fallback.get("leave_id", 0),
                "conversation_id": "",
                "reset_conversation": True,
                "message_id": "",
                "user_id": payload["uid"],
                "orchestrator": "backend_fallback",
            })
    if not (
        isinstance(result, dict)
        and result.get("code") == 200
        and str((result.get("data") or {}).get("answer") or "").strip()
    ):
        # 写操作不允许在 Dify 故障时降级为旧直写处理器：否则会绕过
        # 补字段、消歧、复述确认和一次性令牌。只读请求已在进入 Dify 前
        # 完成确定性分流，这里安全地告知用户“本次未执行”。
        safe_answer = "智能助手暂时不可用，本次没有执行任何写操作，请稍后重试。"
        log_conversation(
            payload["uid"],
            "student_agent" if payload["utype"] == "STUDENT" else "employee_agent",
            data.query, safe_answer,
        )
        return format_data.ok({
            "answer": safe_answer,
            "changed": False,
            "registration_id": 0,
            "conversation_id": "",
            "reset_conversation": True,
            "message_id": "",
            "user_id": payload["uid"],
            "orchestrator": "backend_safe_failure",
        })
    # 学生推荐问题由确定性规则兜底，防止 Dify 把“本科+就业”误答成“本升硕”
    # 或输出“看情况”一类模糊话术。
    if (payload["utype"] == "STUDENT"
            and isinstance(result, dict) and result.get("code") == 200
            and isinstance(result.get("data"), dict)
            and _is_recommendation_query(data.query)):
        result["data"]["answer"] = _direct_recommendation_answer(
            data.query, audience="student"
        )
    if isinstance(result, dict) and result.get("code") == 200:
        answer = (result.get("data") or {}).get("answer", "")
        # 关键词规则没有判高危时，再读取 Dify 的紧急安全回复作为模型兜底证据。
        # 模型只提供 risk score 证据，最终是否写预警、通知多少人仍由后端执行。
        if (payload["utype"] == "STUDENT"
                and emotion_logic.needs_model_review(data.query)):
            model_assessment = emotion_logic.assess_model_response(data.query, answer)
            if model_assessment and emotion_result and not emotion_result.get("alerted"):
                try:
                    emotion_result = emotion_logic.record_emotion(
                        payload["uid"], data.query, source="智能助手-模型兜底",
                        provided_score=model_assessment["score"],
                        provided_reason=model_assessment.get("reason", ""),
                        persist_history=emotion_history_allowed,
                    )
                    emotion_tag = emotion_result["tag"]
                except Exception:
                    pass
            if emotion_result:
                answer = emotion_logic.enforce_safety_status(answer, emotion_result)
                result["data"]["answer"] = answer
        agent_type = "student_agent" if payload["utype"] == "STUDENT" else "employee_agent"
        log_conversation(payload["uid"], agent_type, data.query, answer, emotion_tag)
    return result


@router.post("/public-chat", summary="客服与活动公开聊天（免登录）")
def public_chat(data: PublicChatData, request: Request):
    """访客无需登录，使用系统访客账号（_guest）调用客服与活动智能体。"""
    limited = rate_limit(request, "public_chat", 30, 60)
    if limited:
        return limited
    guest_uid = ensure_guest()
    visitor_key = str(data.visitor or data.conversation_id or "anonymous")[:120]
    result = _call_dify(
        "CUSTOMER", data.query, guest_uid, data.conversation_id,
        extra_inputs={
            "visitor_key": visitor_key,
            "action_pending": "1" if business_action_logic.has_active_action(
                None, visitor_key
            ) else "0",
            "pending_action_type": business_action_logic.active_action_type(
                None, visitor_key
            ),
        },
        external_user=(data.visitor or "guest_user"),
    )
    last_action_result = business_action_logic.pop_last_result(None, visitor_key)
    if (isinstance(result, dict) and result.get("code") == 200
            and isinstance(result.get("data"), dict) and last_action_result):
        changed = bool(last_action_result.get("changed"))
        result["data"]["changed"] = changed
        result["data"]["event_registration_changed"] = (
            changed and last_action_result.get("registration_id") is not None
        )
        result["data"]["registration_id"] = int(
            last_action_result.get("registration_id") or 0
        )
    if isinstance(result, dict) and result.get("code") == 200:
        answer = (result.get("data") or {}).get("answer", "")
        log_conversation(guest_uid, "customer_agent", data.query, answer)
    return result


@router.get("/profile", summary="用户中心：个人资料 + 数据概览")
def profile(authorization: str = Header(default=""), token: str = ""):
    """返回当前登录用户的资料，以及按角色聚合的个人数据概览。"""
    current, err = current_user(request_token(authorization, token))
    if err:
        return err
    uid = current["id"]
    utype = current["user_type"]
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM sys_user WHERE id = ?", (uid,)).fetchone()
        if not row:
            return format_data.fail("用户不存在")
        view_role = (current.get("view_role") or "").upper()
        if view_role:
            role_label = ROLE_PREVIEW_LABEL.get(view_role, view_role)
        elif utype == "EMPLOYEE":
            role_label = STAFF_SUBROLE_LABEL.get(current.get("staff_role") or "", "员工")
        else:
            role_label = ROLE_LABEL.get(utype, utype)
        info = {
            "id": row["id"],
            "username": row["username"],
            "real_name": row["real_name"] or row["username"],
            "user_type": utype,
            "role_label": role_label,
            "contact_info": row["contact_info"] or "",
            "status": row["status"],
            "target_country": (row["target_country"] or "") if "target_country" in row.keys() else "",
            "staff_role": current.get("staff_role") or "",
            "department": row["department"] if "department" in row.keys() else "",
            "security_question": row["security_question"] if "security_question" in row.keys() else "",
            "view_role": view_role,
            "actual_user_id": current.get("actual_user_id") or current["id"],
        }
        stats = {}
        if utype == "STUDENT":
            stats["请假次数"] = conn.execute(
                "SELECT COUNT(*) FROM student_admin_service WHERE student_id=?", (uid,)).fetchone()[0]
            stats["待审批"] = conn.execute(
                "SELECT COUNT(*) FROM student_admin_service WHERE student_id=? AND status='待审批'", (uid,)).fetchone()[0]
            stats["售后工单"] = conn.execute(
                "SELECT COUNT(*) FROM student_feedback_ticket WHERE student_id=?", (uid,)).fetchone()[0]
            stats["成绩记录"] = conn.execute(
                "SELECT COUNT(*) FROM student_score WHERE student_id=?", (uid,)).fetchone()[0]
        elif utype in ("TEACHER", "EMPLOYEE", "ADMIN"):
            # 概览数字也必须遵守数据范围。之前这里给所有员工返回全公司人数，
            # 虽然页面菜单做了限制，但直接调接口仍能看到其他人的业务规模。
            scope, args = student_scope(current)
            stats["待审批请假"] = conn.execute(
                "SELECT COUNT(*) FROM student_admin_service a "
                "JOIN sys_user u ON u.id=a.student_id "
                "WHERE a.status='待审批'" + scope,
                tuple(args),
            ).fetchone()[0]
            stats["学生数"] = conn.execute(
                "SELECT COUNT(*) FROM sys_user u "
                "WHERE u.user_type='STUDENT'" + scope,
                tuple(args),
            ).fetchone()[0]
            stats["未处理预警"] = conn.execute(
                "SELECT COUNT(*) FROM student_psych_alert a "
                "JOIN sys_user u ON u.id=a.student_id "
                "WHERE a.status='未处理'" + scope,
                tuple(args),
            ).fetchone()[0]
            stats["待处理售后"] = conn.execute(
                "SELECT COUNT(*) FROM student_feedback_ticket t "
                "JOIN sys_user u ON u.id=t.student_id "
                "WHERE t.status<>'已解决'" + scope,
                tuple(args),
            ).fetchone()[0]
        info["stats"] = stats
        # 学生附带完整学籍阶段信息：前端据此渲染进度条与模块锁定态
        if utype == "STUDENT":
            info["lifecycle"] = lifecycle.get_stage_info(uid)
        return format_data.ok(info)
    finally:
        conn.close()


@router.post("/change-password", summary="用户中心：修改密码")
def change_password(data: ChangePasswordData):
    current, err = current_user(data.token)
    if err:
        return err
    if current.get("view_role"):
        return format_data.forbidden("演示角色视图下不能修改账号密码，请切回管理员视图")
    minimum = 8 if current["user_type"] in STAFF_ROLES else 6
    if len(data.new_password) < minimum:
        return format_data.fail(f"新密码至少 {minimum} 位")
    conn = get_db()
    try:
        row = conn.execute("SELECT password_hash FROM sys_user WHERE id = ?", (current["id"],)).fetchone()
        if not row:
            return format_data.fail("用户不存在")
        stored = row["password_hash"]
        if not stored:
            return format_data.fail("该账号未设置密码，请联系管理员重置")
        if not verify_password(data.old_password, stored):
            return format_data.fail("原密码不正确")
        conn.execute(
            "UPDATE sys_user SET password_hash=?, "
            "token_version=COALESCE(token_version,0)+1 WHERE id=?",
            (hash_password(data.new_password), current["id"]),
        )
        conn.commit()
        return format_data.ok(None, "密码修改成功，请重新登录")
    finally:
        conn.close()


@router.get("/users", summary="按角色查询用户（仅员工）")
def list_users(role: str | None = None, authorization: str = Header(default=""), token: str = ""):
    """按角色查询用户，并按当前员工的数据范围过滤。

    顾问和班主任选学生时只应看到自己负责的学生；管理员与领导才可查看全量。
    其他员工的账号资料不在普通业务员工的可见范围内。
    """
    current, err = require_staff(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        current_role = rbac.effective_role(current)
        if current_role in ("ADMIN", "LEADER"):
            if role:
                rows = conn.execute(
                    "SELECT id, username, real_name, user_type, contact_info "
                    "FROM sys_user WHERE user_type=? ORDER BY id",
                    (role,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, username, real_name, user_type, contact_info "
                    "FROM sys_user ORDER BY id"
                ).fetchall()
        elif str(role or "").upper() == "STUDENT":
            scope, args = student_scope(current)
            rows = conn.execute(
                "SELECT u.id, u.username, u.real_name, u.user_type, u.contact_info "
                "FROM sys_user u WHERE u.user_type='STUDENT'" + scope + " ORDER BY u.id",
                tuple(args),
            ).fetchall()
        else:
            # 普通业务员工查询非学生账号时，只能看到自己的基本信息，
            # 防止借用户选择器枚举全公司员工联系方式。
            rows = conn.execute(
                "SELECT id, username, real_name, user_type, contact_info "
                "FROM sys_user WHERE id=?",
                (current["id"],),
            ).fetchall()
        return format_data.ok([dict(r) for r in rows])
    finally:
        conn.close()


@router.get("/staff", summary="员工账号列表")
def list_staff(authorization: str = Header(default=""), token: str = ""):
    # 员工名册包含联系方式、部门、角色和账号状态，只允许管理员查看。
    # 领导可以看业务数据，但“查看全公司账号清单”属于账号管理权限。
    _, err = require_admin(request_token(authorization, token))
    if err:
        return err
    conn = get_db()
    try:
        marks = ",".join("?" * len(STAFF_ROLES))
        rows = conn.execute(
            "SELECT id, username, real_name, contact_info, user_type, staff_role, department, "
            "manager_id, status, "
            "CASE WHEN password_hash IS NULL OR password_hash='' THEN 0 ELSE 1 END AS has_pwd "
            "FROM sys_user WHERE user_type IN (%s) ORDER BY status, id" % marks,
            STAFF_ROLES,
        ).fetchall()
        return format_data.ok([dict(r) for r in rows])
    finally:
        conn.close()


@router.post("/staff", summary="员工账号管理（新建/停用/启用）")
def manage_staff(data: StaffData):
    current, err = current_user(data.token)
    if err:
        return err
    payload = {"uid": current["id"], "utype": current["user_type"]}
    if payload["utype"] != "ADMIN":
        return format_data.forbidden("仅管理员可管理员工账号")
    action = (data.action or "create").strip().lower()
    conn = get_db()
    try:
        if action == "create":
            username = data.username.strip()
            if len(username) < 3:
                return format_data.fail("账号至少 3 个字符")
            if len(data.password) < 8:
                return format_data.fail("员工密码至少 8 位")
            if conn.execute("SELECT 1 FROM sys_user WHERE username = ?", (username,)).fetchone():
                return format_data.fail("该账号已存在")
            staff_role = (data.staff_role or "ADVISOR").strip().upper()
            account_role = (data.account_role or "EMPLOYEE").strip().upper()
            if account_role not in ("EMPLOYEE", "ADMIN", "MAINTAINER"):
                return format_data.fail("账号角色无效")
            if account_role == "EMPLOYEE" and staff_role not in EMPLOYEE_SUBROLES:
                return format_data.fail("员工子角色无效")
            if account_role in ("ADMIN", "MAINTAINER"):
                staff_role = "LEADER" if account_role == "ADMIN" else ""
            if not data.security_answer.strip():
                return format_data.fail("请设置员工密码找回安全问题答案")
            conn.execute(
                "INSERT INTO sys_user (username, real_name, user_type, contact_info, status, "
                "password_hash, staff_role, department, manager_id, "
                "security_question, security_answer_hash) "
                "VALUES (?, ?, ?, ?, '正常', ?, ?, ?, ?, ?, ?)",
                (username, data.real_name.strip() or username,
                 account_role, data.contact_info.strip(), hash_password(data.password),
                 staff_role, data.department.strip(), data.manager_id or None,
                 data.security_question.strip(),
                 hash_password(data.security_answer.strip().lower())),
            )
            conn.commit()
            return format_data.ok(None, "员工账号已创建")

        if action in ("disable", "enable"):
            if not data.user_id:
                return format_data.fail("缺少 user_id")
            row = conn.execute("SELECT id, user_type, status FROM sys_user WHERE id=?",
                               (data.user_id,)).fetchone()
            if not row or row["user_type"] not in STAFF_ROLES:
                return format_data.fail("目标账号不是员工")
            if action == "disable":
                if data.user_id == payload["uid"]:
                    return format_data.fail("不能停用自己的账号")
                if _staff_count(conn) <= 1:
                    return format_data.fail("至少要保留一个可用的员工账号")
            new_status = "正常" if action == "enable" else "停用"
            conn.execute(
                "UPDATE sys_user SET status=?, token_version=COALESCE(token_version,0)+1 "
                "WHERE id=?",
                (new_status, data.user_id),
            )
            conn.commit()
            return format_data.ok(None, "已" + ("启用" if action == "enable" else "停用"))

        if action == "reset_password":
            if not data.user_id:
                return format_data.fail("缺少 user_id")
            if len(data.password) < 8:
                return format_data.fail("员工密码至少 8 位")
            row = conn.execute(
                "SELECT id, user_type FROM sys_user WHERE id=?", (data.user_id,)
            ).fetchone()
            if not row or row["user_type"] not in STAFF_ROLES:
                return format_data.fail("目标账号不是员工")
            conn.execute(
                "UPDATE sys_user SET password_hash=?, "
                "token_version=COALESCE(token_version,0)+1 WHERE id=?",
                (hash_password(data.password), data.user_id),
            )
            conn.commit()
            return format_data.ok(None, "员工密码已重置")

        if action == "update":
            if not data.user_id:
                return format_data.fail("缺少 user_id")
            row = conn.execute(
                "SELECT * FROM sys_user WHERE id=?", (data.user_id,)
            ).fetchone()
            if not row or row["user_type"] not in STAFF_ROLES:
                return format_data.fail("目标账号不是后台账号")
            account_role = (data.account_role or row["user_type"]).strip().upper()
            if account_role not in ("EMPLOYEE", "ADMIN", "MAINTAINER"):
                return format_data.fail("账号角色无效")
            if data.user_id == payload["uid"] and account_role != "ADMIN":
                return format_data.fail("不能取消自己的管理员角色")
            staff_role = (data.staff_role or "ADVISOR").strip().upper()
            if account_role == "EMPLOYEE" and staff_role not in EMPLOYEE_SUBROLES:
                return format_data.fail("员工子角色无效")
            if account_role == "ADMIN":
                staff_role = "LEADER"
            if account_role == "MAINTAINER":
                staff_role = ""
            conn.execute(
                "UPDATE sys_user SET real_name=?, contact_info=?, user_type=?, "
                "staff_role=?, department=?, manager_id=?, "
                "token_version=COALESCE(token_version,0)+1 WHERE id=?",
                (data.real_name.strip() or row["real_name"], data.contact_info.strip(),
                 account_role, staff_role, data.department.strip(),
                 data.manager_id or None, data.user_id),
            )
            conn.commit()
            return format_data.ok(None, "账号角色与资料已更新")

        return format_data.fail("不支持的操作：" + action)
    except sqlite3.IntegrityError:
        return format_data.conflict("账号已存在")
    except Exception as e:
        print("员工账号操作失败:", repr(e))
        return format_data.server_error()
    finally:
        conn.close()

