"""确定性的画像归一化、项目匹配与可解释评分。

即使 LLM 或 Dify 不可用，也可直接调用该模块完成 FR-001~FR-005 的核心计算。
"""
import re

from service import knowledge_logic

EDUCATIONS = [
    ("博士", ("博士", "phd", "doctor")),
    ("硕士", ("硕士", "研究生", "研究生在读", "研究生毕业")),
    ("本科", ("本科", "学士", "大学本科")),
    ("专科", ("专科", "大专", "高职")),
    ("中职技校", ("中职", "中专", "职高", "技校", "技工")),
    ("高中", ("高中", "普高")),
    ("初中", ("初中", "初级中学")),
]

# “当前学历”通常带有毕业、在读、刚毕业等语境。必须先识别这些短语，
# 否则“高中毕业想读本科”会把目标本科误当成当前学历。
CURRENT_EDUCATION_PATTERNS = [
    ("博士", r"(博士|phd|doctor)(刚毕业|毕业|在读|读完|学位)"),
    ("硕士", r"(硕士|研究生)(刚毕业|毕业|在读|读完)"),
    ("本科", r"(本科|大学本科|学士)(刚毕业|毕业|在读|读完)"),
    ("专科", r"(专科|大专|高职)(刚毕业|毕业|在读|读完)"),
    ("中职技校", r"(中职|中专|职高|技校|技工)(刚毕业|毕业|在读|读完)"),
    ("高中", r"(高中|普高)(刚毕业|毕业|在读|读完)"),
    ("初中", r"(初中|初级中学)(刚毕业|毕业|在读|读完)"),
]

TARGET_EDUCATION_PATTERNS = [
    ("硕士", r"(想|要|目标|申请|升|读|拿).{0,6}(硕士|研究生)"),
    ("本科", r"(想|要|目标|申请|升|读|拿).{0,8}(本科|学士)"),
]

# 这些词出现在“本科毕业/硕士毕业”等短语前面时，说明它描述的是未来目标，
# 不是当前最高学历。例如“19岁，高中毕业，想快速本科毕业”中，当前学历
# 只能是高中，不能因为后文出现“本科毕业”就推荐本升硕。
GOAL_CONTEXT_WORDS = (
    "想", "要", "目标", "申请", "升", "读", "拿", "快速", "尽快",
    "早点", "早一点", "快一点", "争取", "希望", "考虑", "计划",
)

# 这里只保留确定性的匹配算法，不保存方案名称、描述、学制、学费等
# 知识库事实。候选方案名称与展示字段在运行时从 Dify 获取。
PLAN_RULES: dict[str, dict] = {
    "sg_2_2": {
        "tokens": ("2+2 ", "本科班"), "base": 82,
        "reason": "学历起点符合四年制本科衔接路径",
    },
    "sg_2_2_1": {
        "tokens": ("2+2+1",), "base": 80,
        "reason": "适合希望从中学阶段连续完成本科与硕士的学生",
    },
    "sg_0_5": {
        "tokens": ("0.5/1+2 ",), "base": 88,
        "reason": "预科衔接本科，适合高中或中职毕业后继续升学",
    },
    "sg_0_5_1": {
        "tokens": ("0.5/1+2+1",), "base": 86,
        "reason": "可在同一路径内完成本科和硕士衔接",
    },
    "sg_hotel": {
        "tokens": ("酒店运营",), "base": 81,
        "reason": "就业导向，符合中职技校学历与年龄条件",
    },
    "sg_aviation": {
        "tokens": ("航空运营",), "base": 80,
        "reason": "就业导向，符合中职技校学历与年龄条件",
    },
    "sg_topup": {
        "tokens": ("专升本",), "base": 86,
        "reason": "专科背景可衔接一年制本科提升路径",
    },
    "sg_master": {
        "tokens": ("本升硕",), "base": 88,
        "reason": "本科背景可衔接一年制硕士提升路径",
    },
    "de_dual": {
        "tokens": ("中德精英人才共建计划",), "base": 84,
        "reason": "年龄和学历符合德国双元制基础要求",
    },
}


def normalize_education(value: str) -> str:
    """把学历表述归一化为业务枚举。

    只有命中明确学历词时才返回标准值。以前未命中时会把整句原样返回，
    导致“新加坡留学一年多少钱”这样的普通问题也被当成“已提供学历”，
    进而误触发项目推荐并要求用户重复填写年龄、学历。
    """
    text = str(value or "").strip().lower()
    original = str(value or "").strip()
    # 先识别更具体、更长的学历词，避免“高中”被“中”误判。
    for label, words in EDUCATIONS:
        if any(word in text for word in words):
            return label
    # 需求 FR-002 明确要求：无法归一的学历原样返回。调用方如需判断
    # 是否属于标准学历桶，应使用 EDUCATION_LABELS 再校验一次。
    return original


def normalize_intent(value: str) -> str:
    text = str(value or "")
    if any(word in text for word in ("就业", "工作", "求职")):
        return "就业"
    if any(word in text for word in ("移民", "永居", "绿卡")):
        return "移民"
    if any(word in text for word in ("升学", "学历", "本科", "硕士", "留学", "深造", "读书")):
        return "升学"
    return "升学"


def _has_german(value) -> bool:
    text = str(value or "").lower()
    if any(word in text for word in (
        "没有德语", "无德语", "不会德语", "不懂德语", "德语零基础",
        "无德福", "没有德福",
    )):
        return False
    return any(word in text for word in ("德语", "german", "b1", "b2", "德福"))


def _has_hands_on(value) -> bool:
    text = str(value or "").lower()
    if isinstance(value, bool):
        return value
    if any(word in text for word in ("没有动手", "无动手", "动手能力弱", "不会实操")):
        return False
    return any(word in text for word in ("强", "擅长", "实操", "动手", "维修", "机械", "电工"))


def _extract_current_education(text):
    """提取当前最高学历，并排除目标语境里的“本科毕业/硕士毕业”。"""
    candidates = []
    for label, pattern in CURRENT_EDUCATION_PATTERNS:
        for match in re.finditer(pattern, text):
            # “本科/硕士/专科”更容易被用户写进目标句；中职、高中、初中
            # 通常是当前起点，但也统一检查前缀，保持规则一致。
            prefix = text[max(0, match.start() - 10):match.start()]
            if any(word in prefix for word in GOAL_CONTEXT_WORDS):
                continue
            candidates.append((match.start(), label))
    if not candidates:
        return ""
    # 同一句话出现多个当前学历候选时，采用最靠前的一个。这样
    # “高中毕业，准备本科毕业”仍会稳定识别为高中。
    return min(candidates, key=lambda item: item[0])[1]


def extract_profile(raw_text: str) -> dict:
    """从自然语言抽取需求中列出的基础字段。"""
    text = str(raw_text or "")
    age_match = re.search(r"(\d{1,2})\s*岁", text)
    intent = ""
    for word in ("就业", "工作", "求职", "移民", "永居", "升学", "学历", "本科", "硕士", "留学"):
        if word in text:
            intent = word
            break
    education = _extract_current_education(text)
    target_education = ""
    for label, pattern in TARGET_EDUCATION_PATTERNS:
        if re.search(pattern, text):
            target_education = label
            break
    if not education:
        # 没有当前学历语境时才使用普通关键词。
        candidate = normalize_education(text)
        education = candidate if candidate in {label for label, _ in EDUCATIONS} else ""
    # 多轮追问时用户常说“19，本科”而不是“19岁”。只有在同一句话里
    # 已经识别出学历，才把 1-2 位短数字解释为年龄，避免把年份、课程年数
    # 或人数误当成年龄。
    if not age_match and education and len(text) <= 30:
        short_age = re.search(r"(?<!\d)(\d{1,2})(?!\d)", text)
        if short_age and 14 <= int(short_age.group(1)) <= 60:
            age_match = short_age
    economic = ""
    budget_match = re.search(r"(?:预算|费用|经济).{0,8}(\d+(?:\.\d+)?)\s*(?:万|千)?", text)
    if budget_match:
        economic = budget_match.group(0)
    elif any(word in text for word in ("经济困难", "预算有限", "费用有限", "工薪家庭")):
        economic = "预算有限"
    return {
        "age": int(age_match.group(1)) if age_match else None,
        "education": education,
        "current_education": education,
        "target_education": target_education,
        "intent": normalize_intent(intent or text),
        "language": "德语" if _has_german(text) else "",
        "hands_on": bool(re.search(r"动手|实操|机械|维修|电工|实训", text)),
        "economic_condition": economic,
    }


def recommend_profile(raw_text="", age=None, education="", intent="",
                      language="", hands_on=None, economic_condition="") -> dict:
    extracted = extract_profile(raw_text)
    try:
        age_value = int(age) if age not in (None, "") else extracted.get("age")
    except (TypeError, ValueError):
        age_value = extracted.get("age")
    edu = normalize_education(education or extracted.get("education", ""))
    want = normalize_intent(intent or extracted.get("intent", ""))
    language_value = language or extracted.get("language", "")
    hands_value = hands_on if hands_on is not None else extracted.get("hands_on", False)
    economic_value = economic_condition or extracted.get("economic_condition", "")

    if not age_value or not edu:
        missing = []
        if not age_value:
            missing.append("年龄")
        if not edu:
            missing.append("当前学历")
        missing_text = "和".join(missing)
        return {
            "status": "insufficient",
            "msg": "画像信息不足，请补充" + missing_text + "。",
            "profile": {"age": age_value, "education": edu, "intent": want},
            "matches": [],
            "best_match": None,
            "summary": "请补充" + missing_text + "后再进行项目匹配。",
        }

    # 方案名称、描述、适合人群、来源文档均从 Dify 目录读取。
    catalog = knowledge_logic.program_catalog()["programs"]

    def pick(rule_key):
        rule = PLAN_RULES[rule_key]
        for item in catalog:
            name = item.get("project_name", "")
            if all(token in name for token in rule["tokens"]):
                return item
        return None

    candidates = []
    candidate_caveats: dict[str, list[str]] = {}
    has_german = _has_german(language_value)
    has_hands_on = _has_hands_on(hands_value)
    germany_ready = has_german and has_hands_on
    if edu == "初中":
        candidates += [pick("sg_2_2"), pick("sg_2_2_1")]
    elif edu in ("高中", "中职技校"):
        candidates += [pick("sg_0_5"), pick("sg_0_5_1")]
    elif edu == "专科":
        if want == "升学":
            candidates.append(pick("sg_topup"))
        elif want in ("就业", "移民") and 18 <= age_value <= 35 and germany_ready:
            candidates.append(pick("de_dual"))
    elif edu == "本科":
        if want == "升学":
            candidates.append(pick("sg_master"))
        elif want in ("就业", "移民") and 18 <= age_value <= 35 and germany_ready:
            # 本科且目标就业/移民时，不应推荐“本升硕”。
            candidates.append(pick("de_dual"))

    if edu == "中职技校" and age_value >= 17 and want == "就业":
        candidates += [pick("sg_hotel"), pick("sg_aviation")]

    germany_item = pick("de_dual")
    germany_plan = germany_item["project_name"] if germany_item else ""
    if (18 <= age_value <= 35 and edu in ("高中", "中职技校", "专科", "本科")
            and germany_ready):
        candidates.append(germany_item)

    candidates = [item for item in candidates if item]
    seen = set()
    matches = []
    for plan in candidates:
        name = plan["project_name"]
        if name in seen:
            continue
        seen.add(name)
        rule_key = next((
            key for key, rule in PLAN_RULES.items()
            if all(token in name for token in rule["tokens"])
        ), None)
        if not rule_key:
            continue
        meta = PLAN_RULES[rule_key]
        score = int(meta["base"])
        reasons: list[str] = [str(meta["reason"])]
        if want == "就业" and any(word in name for word in ("就业", "双元制")):
            score += 5
            reasons.append("就业意向与项目方向匹配")
        if want == "升学" and any(word in name for word in ("本科", "本硕", "专升本", "本升硕")):
            score += 3
            reasons.append("升学意向与学历提升路径匹配")
        if want == "移民" and "双元制" in name:
            score += 5
            reasons.append("移民意向与双元制路径匹配")
        if want in ("就业", "移民") and not any(
            word in name for word in ("就业", "双元制")
        ):
            # 用户目标明确为就业/移民时，纯升学项目不能因为基础分高排在前面。
            score -= 20
        caveats = candidate_caveats.get(name, [])
        if caveats:
            score -= 8 * len(caveats)
            reasons.append("条件推荐：" + "；".join(caveats))
        elif name == germany_plan:
            reasons.append("德语基础和动手能力条件已满足")
        matches.append({
            "project": name,
            "score": min(score, 100),
            "reason": "；".join(reasons),
            "caveats": caveats,
            "knowledge_plan": plan,
        })

    matches.sort(key=lambda item: (-item["score"], item["project"]))
    profile = {
        "age": age_value,
        "education": edu,
        "intent": want,
        "language": str(language_value or ""),
        "hands_on": bool(hands_value),
        "economic_condition": economic_value,
    }
    if not matches:
        return {
            "status": "no_match", "msg": "暂无匹配项目，请联系老师",
            "profile": profile, "matches": [], "best_match": None,
            "summary": "当前画像没有命中确定性项目规则，建议人工评估。",
        }
    best = matches[0]
    if best.get("caveats"):
        summary = (
            f"{edu}学历、年龄 {age_value}，条件推荐【{best['project']}】，"
            f"匹配度 {best['score']} 分；" + "；".join(best["caveats"]) + "。"
        )
    else:
        summary = (
            f"{edu}学历、年龄 {age_value}，推荐【{best['project']}】，"
            f"匹配度 {best['score']} 分。"
        )
    return {
        "status": "match",
        "profile": profile,
        "matches": matches,
        "best_match": best,
        "summary": summary,
    }
