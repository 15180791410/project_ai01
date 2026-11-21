"""
情绪检测与心理预警引擎
======================
统一给「智能助手聊天」和「心情记录」两个入口调用。
只要识别出高危/关注情绪，就写入 student_psych_alert，
这样老师/员工后台的心理预警看板会立刻看到。
"""
import json
import re
from datetime import datetime, timedelta

from db import get_db

# 极高风险：直接触发「高危」预警
CRITICAL_WORDS = [
    "自杀", "想死", "活不下去", "不想活", "结束生命", "轻生", "寻死",
    "割腕", "跳楼", "上吊", "安眠药", "伤害自己", "死了算了", "不想活了",
    "没有活下去的意义", "人生没有意义", "想消失", "烧炭", "卧轨", "跳河",
    "跳桥", "喝农药", "吞药", "割脉",
    "杀光", "血洗", "同归于尽", "灭了他", "灭了她",
    "伤害同学", "报复同学", "砍死", "捅死", "枪杀",
]

# 单靠完整词表容易漏掉“结束自己的生命”“准备跳下去”“写了遗书”等
# 常见变体。这里补充组合模式，只有命中明确的自伤/轻生语义时才提高分值，
# 避免把普通学业压力直接判成高危。
CRITICAL_PATTERNS = [
    r"(?:结束|了结).{0,4}(?:自己|我的)?.{0,2}生命",
    r"(?:准备|打算|想|要).{0,8}(?:跳下去|跳楼|服药|吞药)",
    r"(?:写|留).{0,2}遗书",
    r"(?:自残|划伤自己|割伤自己)",
    r"不想再?醒来",
    r"看不到.{0,4}希望",
    r"活着.{0,4}没(?:有)?意思",
    # 煤气、燃气、密闭空间等组合通常不会出现“自杀”两个字，但危险性极高。
    r"(?:打开|开).{0,8}(?:煤气|燃气|天然气).{0,16}"
    r"(?:关|封|睡|不再醒|了结|结束)",
    r"(?:关|封).{0,8}(?:门窗|房间).{0,16}"
    r"(?:煤气|燃气|天然气|烧炭)",
    r"(?:烧炭|密闭).{0,10}(?:结束|了结|睡|不再醒|不想醒)",
]
# 中风险：触发「关注」预警
WARNING_WORDS = [
    "焦虑", "抑郁", "压力大", "崩溃", "绝望", "无助", "失眠", "想哭",
    "撑不住", "熬不下去", "好累", "很累", "孤独", "被孤立", "没有朋友",
    "迷茫", "荒废", "失恋", "被欺负", "霸凌", "不想上学", "厌学",
    "情绪低落", "心情很差", "很难过", "痛苦", "窒息", "活着没意思",
]

POSITIVE_WORDS = [
    "开心", "高兴", "很好", "不错", "顺利", "轻松", "安心", "期待",
    "有希望", "谢谢", "喜欢", "满意", "进步", "恢复", "放松",
]

# 这些词本身不是高危结论，只表示“规则拿不准，需要大模型结合完整语义复核”。
# 例如“打开煤气罐、关窗睡觉”过去没有任何规则命中，实际却属于严重自伤场景。
MODEL_REVIEW_CUES = (
    "煤气", "燃气", "天然气", "烧炭", "药", "刀", "绳", "桥", "河", "楼",
    "窗", "睡", "醒", "消失", "结束", "解脱", "离开", "没意义", "活着",
    "伤害", "危险", "血", "跳", "撞", "开车", "高空",
)

# 只用于识别 Dify 已经给出紧急安全干预、但原后端规则未命中的语义变体。
# 这是“模型兜底”的证据，最终是否真正写预警仍由 record_emotion 决定。
MODEL_CRISIS_MARKERS = (
    "24小时", "危机干预", "生命热线", "不能看着你出事", "立即报警",
    "马上来陪你", "通知了你的班主任", "立刻联系老师", "紧急支持人员",
    "先保证自己的安全", "离开危险环境",
)
MODEL_DANGER_MARKERS = (
    "煤气", "燃气", "伤害", "自杀", "轻生", "跳楼", "跳桥", "服药",
    "吞药", "割伤", "自残", "遗书", "结束生命",
)

VIOLENCE_VERBS = (
    "杀死", "杀害", "杀掉", "杀人", "干死", "干掉", "弄死", "打死", "揍死",
    "砍死", "砍人", "捅死", "捅人", "枪杀", "开枪", "射杀", "炸死", "毒死",
    "掐死", "勒死", "伤害", "袭击", "攻击", "绑架", "报复", "杀光", "血洗",
)
HUMAN_TARGETS = (
    "一个人", "所有人", "别人", "他人", "同学", "学生", "老师", "班主任",
    "舍友", "室友", "家长", "全班", "全校", "他", "她", "人",
)
BENIGN_CONTEXTS = (
    "游戏", "电影", "电视剧", "小说", "剧本", "任务", "项目", "病毒", "作业",
)


def _violence_risk(text: str):
    """识别“暴力动作 + 人类目标”的组合，而不是死记完整句子。"""
    # 影视、游戏语境不能一律屏蔽；只有没有出现现实人物/校园目标时才跳过。
    real_targets = ("同学", "学生", "老师", "班主任", "舍友", "室友",
                    "家长", "全班", "全校", "现实", "真人")
    if any(word in text for word in BENIGN_CONTEXTS) and not any(
        word in text for word in real_targets
    ):
        return []
    if any(word in text for word in (
        "不想杀", "不想伤害", "不会伤害", "没有想伤害", "没想杀", "不会去杀",
    )):
        return []
    verbs = [word for word in VIOLENCE_VERBS if word in text]
    targets = [word for word in HUMAN_TARGETS if word in text]
    if not verbs or not targets:
        return []
    intent_words = ("我", "想", "要", "准备", "计划", "打算", "随机", "随便", "马上", "现在")
    if any(word in text for word in intent_words):
        return ["对他人暴力风险：" + "/".join(verbs + targets[:2])]
    return []


def analyze(text):
    """根据文本计算情绪分、标签与风险等级。"""
    import re
    text = (text or "").strip()
    hit_critical = [w for w in CRITICAL_WORDS if w in text]
    hit_critical += [
        match.group(0) for pattern in CRITICAL_PATTERNS
        for match in [re.search(pattern, text, re.I)] if match
    ]
    hit_warning = [w for w in WARNING_WORDS if w in text]
    # 对他人实施严重暴力的意图必须与自伤同等对待。
    violence_hits = _violence_risk(text)
    weapon_context = any(
        word in text for word in ("带刀", "带枪", "拿刀", "拿枪", "持刀", "持枪")
    ) and any(word in text for word in ("学校", "教室", "宿舍", "同学", "老师"))
    if weapon_context:
        violence_hits.append("携带武器接近校园或人群")
    if hit_critical or violence_hits:
        return {
            "score": 100 if violence_hits else 90,
            "tag": "高危",
            "risk_level": "高",
            "need_alert": True,
            "hits": hit_critical + violence_hits if violence_hits else hit_critical,
        }
    if hit_warning:
        return {
            "score": 55,
            "tag": "关注",
            "risk_level": "中",
            "need_alert": False,
            "hits": hit_warning,
        }
    positive_hits = [word for word in POSITIVE_WORDS if word in text]
    if positive_hits:
        return {
            "score": 15,
            "tag": "正常",
            "risk_level": "低",
            "need_alert": False,
            "hits": positive_hits,
        }
    return {
        "score": 20,
        "tag": "正常",
        "risk_level": "低",
        "need_alert": False,
        "hits": [],
    }


def needs_model_review(text, rule_result=None):
    """判断是否需要把语义判断交给 Dify 大模型兜底。

    明确高危已经由关键词和组合模式命中，无需重复调用模型。普通业务问题
    也不进入模型，避免每次查成绩、请假都额外消耗一次模型调用。
    """
    result = rule_result or analyze(text)
    if result.get("risk_level") == "高":
        return False
    lowered = str(text or "").lower()
    return any(cue in lowered for cue in MODEL_REVIEW_CUES)


def _score_to_label(score):
    """由后端统一把 0-100 分映射为标签，模型不能自行决定最终等级。"""
    if score >= 90:
        return "高危", "高"
    if score >= 70:
        return "高危", "高"
    if score >= 40:
        return "关注", "中"
    return "正常", "低"


def merge_model_score(rule_result, model_score, model_reason=""):
    """合并规则分与模型分，永远取较高值，防止低分模型覆盖高危规则。"""
    if model_score is None:
        return rule_result
    try:
        score = max(0, min(100, int(model_score)))
    except (TypeError, ValueError):
        return rule_result
    effective = max(int(rule_result.get("score") or 0), score)
    tag, risk_level = _score_to_label(effective)
    hits = list(rule_result.get("hits") or [])
    if model_reason:
        hits.append("大模型兜底：" + str(model_reason))
    return {
        **rule_result,
        "score": effective,
        "tag": tag,
        "risk_level": risk_level,
        "need_alert": effective >= 70,
        "model_score": score,
        "hits": hits,
    }


def assess_model_response(query, model_answer):
    """从 Dify 的紧急安全回复中提取模型兜底风险证据。

    这一步不搜索知识库，也不让模型直接执行通知。它只读取模型是否已经把
    用户的话识别为现实伤害风险；最终分值仍由 merge_model_score 合并，
    是否写预警与通知仍由 record_emotion 执行。
    """
    answer = str(model_answer or "")
    original = str(query or "")
    crisis_hits = [word for word in MODEL_CRISIS_MARKERS if word in answer]
    danger_hits = [
        word for word in MODEL_DANGER_MARKERS
        if word in answer or word in original
    ]
    if crisis_hits and danger_hits:
        return {
            "score": 90,
            "reason": "紧急安全干预 + 现实危险线索（" +
                      "/".join(crisis_hits[:2] + danger_hits[:2]) + "）"
        }
    if len(crisis_hits) >= 3:
        return {
            "score": 75,
            "reason": "模型执行了紧急安全干预（" + "/".join(crisis_hits[:3]) + "）"
        }
    return None


def enforce_safety_status(answer, result):
    """用数据库真实结果替换模型自行生成的“已通知”承诺。

    模型擅长安慰和建议，但它无法知道数据库事务是否提交、通知是否写入成功。
    因此先删除模型生成的“已经通知班主任/老师”话术，再只根据后端结果追加
    权威状态，避免出现学生看到“已通知”、后台却没有预警的严重误导。
    """
    text = str(answer or "")
    claim_patterns = (
        r"我(?:已经|已)?(?:帮你)?(?:通知|联系)[^。！？\n]{0,36}"
        r"(?:班主任|老师)[^。！？\n]*[。！？]?",
        r"(?:已经|已)(?:通知|告知)[^。！？\n]{0,36}"
        r"(?:班主任|老师)[^。！？\n]*[。！？]?",
    )
    for pattern in claim_patterns:
        text = re.sub(pattern, "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if result.get("risk_level") != "高":
        return text
    if result.get("alerted") and int(result.get("notified_count") or 0) > 0:
        status = (
            "系统状态：本次高危预警已写入，并已向 "
            + str(result.get("notified_count")) + " 位相关老师发送站内通知。"
        )
    elif result.get("alerted"):
        status = (
            "系统状态：本次高危预警已写入，但站内通知未送达。"
            "如你正处于危险中，请立即拨打紧急电话或联系身边可信任的人。"
        )
    else:
        status = (
            "系统状态：本次风险信号已记录，但未生成新的高危预警。"
            "如你正处于危险中，请立即联系老师、家人或紧急服务。"
        )
    return text + ("\n\n" if text else "") + status


def _already_alerted(conn, student_id, reason_text, minutes=10):
    """去重：10 分钟内同一学生、相同原因不重复预警。"""
    since = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT id FROM student_psych_alert "
        "WHERE student_id=? AND trigger_reason=? AND create_time>=? LIMIT 1",
        (student_id, reason_text, since),
    ).fetchone()
    return row is not None


def _append_history(conn, student_id, entry, tag, score):
    """把本次情绪写入画像表，保留最近 50 条。"""
    row = conn.execute(
        "SELECT emotion_history FROM student_psych_profile WHERE student_id=?",
        (student_id,),
    ).fetchone()
    history = []
    if row and row["emotion_history"]:
        try:
            history = json.loads(row["emotion_history"])
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    if history:
        last = history[-1]
        try:
            last_time = datetime.strptime(last.get("time", ""), "%Y-%m-%d %H:%M:%S")
            same_text = (last.get("text") or "") == (entry.get("text") or "")
            if same_text and abs((datetime.now() - last_time).total_seconds()) <= 300:
                history[-1] = {**last, "tag": tag, "score": score}
                entry = history[-1]
            else:
                history.append(entry)
        except Exception:
            history.append(entry)
    else:
        history.append(entry)
    history = history[-50:]
    last_time = entry.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO student_psych_profile "
        "(student_id, latest_emotion_tag, emotion_score, emotion_history, last_interaction_time) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(student_id) DO UPDATE SET "
        "latest_emotion_tag=excluded.latest_emotion_tag, "
        "emotion_score=excluded.emotion_score, "
        "emotion_history=excluded.emotion_history, "
        "last_interaction_time=excluded.last_interaction_time",
        (student_id, tag, score, json.dumps(history, ensure_ascii=False), last_time),
    )


def record_emotion(student_id, text, source="心情记录",
                   provided_score=None, provided_tag="", provided_risk="",
                   provided_reason="", persist_history=True):
    """
    记录一次情绪，必要时触发预警。

    返回:
      {
        "score", "tag", "risk_level", "alerted"(本次是否新写入预警),
        "hit_words", "tip"
      }
    """
    result = analyze(text)
    # FR-009 允许调用方附带 LLM 评估字段。模型分只能与规则分取较高值，
    # 不能把明确高危降级。最终标签和风险等级始终由后端阈值映射。
    if provided_score is not None:
        result = merge_model_score(
            result,
            provided_score,
            provided_reason or "外部模型风险评分",
        )
    conn = get_db()
    staff_ids = []
    student_name = ""
    try:
        alerted = False
        if result["score"] >= 70 and not _already_alerted(conn, student_id, text):
            # 显式写本地时间：SQLite 的 CURRENT_TIMESTAMP 是 UTC，与 Python 本地时间差 8 小时，
            # 会导致去重的时间比较失效。
            local_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO student_psych_alert "
                "(student_id, trigger_reason, risk_level, emotion_score, status, create_time) "
                "VALUES (?, ?, ?, ?, '未处理', ?)",
                (student_id, text[:500], result["risk_level"], result["score"], local_now),
            )
            alerted = True
            student = conn.execute(
                "SELECT real_name, username, homeroom_id FROM sys_user WHERE id=?", (student_id,)
            ).fetchone()
            if student and student["homeroom_id"]:
                homeroom = conn.execute(
                    "SELECT id FROM sys_user WHERE id=? AND user_type='EMPLOYEE' "
                    "AND staff_role='HOMEROOM' AND status='正常'",
                    (student["homeroom_id"],),
                ).fetchone()
                if homeroom:
                    staff_ids = [homeroom["id"]]
            if not staff_ids:
                staff_ids = [
                    row["id"] for row in conn.execute(
                        "SELECT id FROM sys_user WHERE user_type='EMPLOYEE' "
                        "AND status='正常' AND staff_role='HOMEROOM' ORDER BY id"
                    ).fetchall()
                ]
            # 高危预警同时升级给管理员和领导。班主任离职、停用或长时间未登录时，
            # 不能只依赖单一接收人，否则风险会停在通知中心无人处理。
            escalation_ids = [
                row["id"] for row in conn.execute(
                    "SELECT id FROM sys_user WHERE status='正常' AND ("
                    "user_type='ADMIN' OR "
                    "(user_type='EMPLOYEE' AND staff_role='LEADER')) ORDER BY id"
                ).fetchall()
            ]
            staff_ids = list(dict.fromkeys(staff_ids + escalation_ids))
            student_name = (
                (student["real_name"] or student["username"]) if student else str(student_id)
            )
        if persist_history:
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source": source,
                "tag": result["tag"],
                "score": result["score"],
                "text": text[:200],
            }
            _append_history(conn, student_id, entry, result["tag"], result["score"])
        conn.commit()
    finally:
        conn.close()

    notified_count = 0
    if alerted and staff_ids:
        from service.notify_logic import notify_many
        notified_count = notify_many(
            staff_ids,
            "预警",
            "学生高危情绪预警",
            f"{student_name} 出现高危情绪信号，请尽快查看心理预警看板并跟进。",
            "alerts",
        )

    if result["tag"] == "高危" and alerted and notified_count > 0:
        tip = "已触发高危预警并通知老师，请尽快联系可信任的老师或紧急支持人员"
    elif result["tag"] == "高危" and alerted:
        tip = "已写入高危预警，但通知未能送达老师；请立即联系学校或紧急支持人员"
    elif result["tag"] == "高危":
        tip = "这次记录已保存；如风险持续，请立即联系老师或紧急支持人员"
    elif result["tag"] == "关注":
        tip = "已记录你的情绪；如需要，可以联系老师进一步沟通"
    else:
        tip = "已记录"
    return {
        "score": result["score"],
        "tag": result["tag"],
        "risk_level": result["risk_level"],
        "alerted": alerted,
        "notified_count": notified_count,
        "model_score": result.get("model_score"),
        "history_saved": bool(persist_history),
        "hit_words": result["hits"],
        "tip": tip,
    }
