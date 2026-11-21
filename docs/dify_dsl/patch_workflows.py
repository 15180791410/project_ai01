# -*- coding: utf-8 -*-
"""
把「新增接口」接进三个 Dify 工作流。

做法：
- 读取 live/<app>_draft.json（当前 Dify 里的真实图）
- 重建意图分类（if-else）的 case 与分支链：每个分支 = 工具节点 -> LLM -> 直接回复
- 学生/员工开始节点增加 auth_token 变量（登录令牌，由后端 dify-chat 传入）
- 输出 live/<app>_new.json（用于写回数据库）与 <app>_workflow.yml（可导入的 DSL）

用法：python patch_workflows.py
"""
import copy
import json
import os
import uuid

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "live")
PROVIDER_ID = "1c2de193-75e4-4c78-8366-99b5f4e1ca00"
PROVIDER_NAME = "yuejiao_api"
X_TOOL, X_LLM, X_ANS = 650, 950, 1250
Y0, DY = 40, 210

# 每个 LLM 节点都要带上的输出规范。
# 目的：回复直接给答案、不带思考过程和工具痕迹。普通说明保持自然语言，
# 多条结构化业务数据统一使用 Markdown 表格；前端已有安全的表格渲染器。
STYLE_TEXT = (
    "\n\n【输出规范（必须遵守）】\n"
    "1. 直接回答问题，只给结论和要点，不要输出思考过程、推理步骤、计划、"
    "工具调用说明，也不要出现“正在查询”“根据工具返回”“数据库显示”“作为 AI”这类话。\n"
    "2. 用简体中文，像顾问面对面说话。普通说明用自然段落，不要堆砌标题和粗体。\n"
    "3. 当返回多条项目、活动、学生、成绩、请假、审批、报名、线索、工单或统计记录时，"
    "必须使用 Markdown 表格，先给表头再逐行给数据，不得把多条记录挤成一段。"
    "表格列只使用工具实际返回的字段。\n"
    "4. 单条确认、追问、错误说明和普通建议使用自然语言，不必强行做表格。\n"
    "5. 回答尽量简洁；表格单元格中的文字要短，长说明放在表格后的自然段。\n"
    "6. 不要重复用户的问题，不要加“当然可以”“希望以上回答对你有帮助”这类客套开头结尾。"
)


def load(app):
    with open(os.path.join(LIVE, "%s_draft.json" % app), encoding="utf-8") as f:
        return json.load(f)


def cond(cid, value):
    return {
        "id": cid,
        "comparison_operator": "contains",
        "value": value,
        "varType": "string",
        "variable_selector": ["sys", "query"],
    }


def make_case(case_id, values):
    return [{
        "case_id": case_id,
        "id": case_id,
        "logical_operator": "or",
        "conditions": [cond("%s_c%d" % (case_id, i), v) for i, v in enumerate(values)],
    }]


def place(node, x, y):
    node = copy.deepcopy(node)
    node["position"] = {"x": x, "y": y}
    node["positionAbsolute"] = {"x": x, "y": y}
    node["selected"] = False
    return node


def build_tool(tpl, nid, title, tool_name, tool_label, params, desc=""):
    node = copy.deepcopy(tpl)
    node["id"] = nid
    node["data"] = {
        "type": "tool",
        "title": title,
        "desc": desc,
        "selected": False,
        "provider_id": PROVIDER_ID,
        "provider_name": PROVIDER_NAME,
        "provider_type": "api",
        "tool_name": tool_name,
        "tool_label": tool_label,
        "tool_configurations": {},
        "tool_parameters": {
            k: {"type": "mixed", "value": v} for k, v in params.items()
        },
        "tool_node_version": "2",
    }
    node["width"] = 242
    node["height"] = 96
    return node


def build_llm(tpl, nid, title, system_text):
    node = copy.deepcopy(tpl)
    node["id"] = nid
    d = copy.deepcopy(tpl["data"])
    d["title"] = title
    d["desc"] = ""
    # 关闭深度思考：qwen3 系列默认会先推理再回答，既慢（xhigh 档常要十几秒）
    # 又会把思考内容随 answer 一起吐出。这里统一钉死为关闭。
    cp = d.setdefault("model", {}).setdefault("completion_params", {})
    cp["enable_thinking"] = False
    cp.pop("reasoning_effort", None)
    d["prompt_template"] = [
        {"role": "system", "text": system_text.rstrip() + STYLE_TEXT, "id": str(uuid.uuid4())},
        {"role": "user", "text": "{{#sys.query#}}", "id": str(uuid.uuid4())},
    ]
    d["context"] = {"enabled": False, "variable_selector": []}
    node["data"] = d
    node["width"] = 242
    node["height"] = 88
    return node


def build_answer(tpl, nid, title, text):
    node = copy.deepcopy(tpl)
    node["id"] = nid
    d = copy.deepcopy(tpl["data"])
    d["title"] = title
    d["desc"] = ""
    d["answer"] = text
    d["variables"] = []
    node["data"] = d
    node["width"] = 242
    node["height"] = 103
    return node


def build_profile_code(tpl, nid, title, llm_id):
    """构建 LLM JSON 解析节点，把自由文本画像变成结构化工具参数。"""
    code = """def main(profile_json: str) -> dict:
    import json
    import re
    text = (profile_json or '').strip()
    text = re.sub(r'^```(?:json)?\\s*', '', text, flags=re.I)
    text = re.sub(r'\\s*```$', '', text)
    try:
        data = json.loads(text)
    except Exception:
        data = {}
    def value(key):
        item = data.get(key)
        if item is None:
            return ''
        if isinstance(item, bool):
            return 'true' if item else 'false'
        return str(item)
    return {
        'age': value('age'),
        'education': value('education'),
        'intent': value('intent'),
        'language': value('language'),
        'hands_on': value('hands_on'),
        'economic_condition': value('economic_condition'),
    }
"""
    node = {
        "id": nid,
        "data": {
            "type": "code",
            "title": title,
            "desc": "解析 LLM 输出的结构化画像，禁止猜测缺失字段",
            "selected": False,
            "code_language": "python3",
            "code": code,
            "variables": [
                {"variable": "profile_json", "value_selector": [llm_id, "text"]}
            ],
            "outputs": {
                name: {"type": "string", "children": None}
                for name in (
                    "age", "education", "intent", "language",
                    "hands_on", "economic_condition",
                )
            },
            "dependencies": [],
        },
        "type": "custom",
        "width": 242,
        "height": 110,
    }
    return place(node, 0, 0)


def add_auth_token(start_node):
    vs = start_node["data"].setdefault("variables", [])
    if not any(v.get("variable") == "auth_token" for v in vs):
        vs.append({
            "variable": "auth_token",
            "label": "登录令牌（由后端自动传入）",
            "type": "text-input",
            "required": False,
            "default": "",
            "options": [],
        })
    start_node["data"]["desc"] = (
        "student_id/employee_id 为整数，由 Service API 的 inputs 传入；auth_token 为登录令牌，"
        "新接口（申请进度/报名/通知/审批/线索等）用它鉴权。"
    )


HEADER_AUTH_TOOLS = {
    "my_stage", "my_leaves", "my_scores", "my_feedbacks", "my_psych_profile",
    "search_customer", "get_reports", "search_advanced", "search_scores",
    "pending_leaves", "all_leaves", "event_registrations", "my_registrations",
    "students_by_country", "list_leads", "all_applications",
    "list_notifications", "list_alerts", "profile", "list_users", "list_staff",
}

BODY_AUTH_TOOLS = {
    "leave", "emotion", "feedback", "add_lead", "submit_report",
    "approve_leave", "add_score", "update_lead_status", "resolve_feedback",
    "nl2sql", "approve_leave_with_comment", "handle_alert",
    "handle_alert_admin", "follow_up_lead", "create_application",
    "advance_application", "mark_notification_read", "change_password",
    "save_event", "delete_event",
    "set_student_country",
}


def configure_node(node):
    """为旧模板节点补上新的鉴权参数，并开启有限对话记忆。"""
    data = node.get("data") or {}
    if data.get("type") == "tool":
        name = data.get("tool_name")
        params = data.setdefault("tool_parameters", {})
        if name in HEADER_AUTH_TOOLS:
            params.pop("token", None)
            params.pop("student_id", None)
            params.pop("employee_id", None)
            params["authorization"] = {
                "type": "mixed",
                "value": "Bearer {{#start_node.auth_token#}}",
            }
        elif name in BODY_AUTH_TOOLS:
            params.pop("student_id", None)
            params.pop("employee_id", None)
            params["token"] = {
                "type": "mixed",
                "value": "{{#start_node.auth_token#}}",
            }
        # Dify 工具节点默认遇到 HTTP 4xx/5xx 会中止整张工作流，用户只能看到
        # “服务暂不可用”。统一改为默认值策略：权限拒绝或暂时故障时把一段
        # 安全提示作为 tool.text 交给后续 LLM，让用户收到明确且不含隐私的说明。
        data["error_strategy"] = "default-value"
        data["default_value"] = [{
            "key": "text",
            "type": "string",
            "value": (
                "当前请求未取得业务数据：可能是当前角色无权访问，或服务暂时不可用。"
                "请直接告知用户“当前角色无权执行此操作或服务暂不可用”，"
                "不得猜测、补全或输出任何未返回的数据。"
            ),
        }]
    elif data.get("type") == "llm":
        data["memory"] = {
            "window": {"size": 10, "enabled": True},
            "role_prefix": {"user": "", "assistant": ""},
            "query_prompt_template": "{{#sys.query#}}",
        }
    return node


def build_graph(app, spec):
    """spec 描述所有分支。返回新的 graph dict。"""
    g = load(app)
    by_id = {n["id"]: n for n in g["nodes"]}
    start = by_id["start_node"]
    if_node = by_id["if_node"]
    model_tpl = by_id[spec["llm_template"]]
    tool_tpl = by_id[spec["tool_template"]]
    ans_tpl = by_id[spec["answer_template"]]

    if spec.get("auth_token"):
        add_auth_token(start)

    nodes = []
    edges = []
    seen_nodes = set()
    seen_edges = set()

    def add_node(node):
        """同一节点只写入一次；多个分支可以共享知识库链。"""
        if node["id"] not in seen_nodes:
            nodes.append(node)
            seen_nodes.add(node["id"])

    def add_edge(edge):
        """共享链的边也去重，避免 Dify 草稿自动去重后与发布图不一致。"""
        if edge["id"] not in seen_edges:
            edges.append(edge)
            seen_edges.add(edge["id"])

    add_node(start)
    add_node(if_node)
    add_edge({
        "id": "e_start_if", "source": "start_node",
        "sourceHandle": "source", "target": if_node["id"],
    })
    classes = []
    fallback_class = "fallback_general"

    for idx, br in enumerate(spec["branches"]):
        y = Y0 + idx * DY
        if "new" in br:
            n = br["new"]
            case_id = br["case_id"]
            class_values = n["conds"]
            tool = build_tool(tool_tpl, n["tool_id"], n["tool_title"], n["tool_name"],
                              n["tool_label"], n["params"], "工具节点：调用 %s（yuejiao_api）" % n["tool_name"])
            llm = build_llm(model_tpl, n["llm_id"], n["llm_title"], n["prompt"])
            ans = build_answer(ans_tpl, n["ans_id"], n["ans_title"], "{{#%s.text#}}" % n["llm_id"])
            if n.get("profile_extract"):
                profile_llm = build_llm(
                    model_tpl,
                    "llm_profile_extract",
                    "LLM 抽取用户画像",
                    "只抽取用户已经明确提供的画像信息，输出严格 JSON，不要解释。"
                    "字段固定为 age、education、intent、language、hands_on、"
                    "economic_condition。education 是当前最高学历，不是目标学历；"
                    "“想拿本科/想本科毕业”属于目标，不得当成当前本科。"
                    "缺失字段使用空字符串；hands_on 使用 true/false。"
                    "不要补充用户没有说过的信息。",
                )
                profile_code = build_profile_code(
                    tool_tpl, "code_profile_extract", "解析结构化画像",
                    "llm_profile_extract",
                )
                chain = [profile_llm, profile_code, tool, llm, ans]
            else:
                chain = [tool, llm, ans]
        else:
            case_id = br["case_id"]
            class_values = br["conds"]
            chain = [copy.deepcopy(by_id[i]) for i in br["chain"]]

        classes.append({
            "id": case_id,
            # Question Classifier 的类别名会直接进入提示词，用业务词组帮助
            # 模型理解语义，而不是只显示内部 case_id。
            "name": "；".join(str(value) for value in class_values[:6]),
        })
        for j, node in enumerate(chain):
            configure_node(node)
            x = X_TOOL + j * 300
            add_node(place(node, x, y))
        add_edge({"id": "e_%s_%s" % (if_node["id"], case_id), "source": if_node["id"],
                  "sourceHandle": case_id, "target": chain[0]["id"]})
        for a, b in zip(chain, chain[1:]):
            add_edge({"id": "e_%s_%s" % (a["id"], b["id"]), "source": a["id"],
                      "sourceHandle": "source", "target": b["id"]})

    # false 分支
    chat_chain = [copy.deepcopy(by_id[i]) for i in spec["false_chain"]]
    for j, node in enumerate(chat_chain):
        configure_node(node)
        add_node(place(node, X_TOOL + j * 300, Y0 + len(spec["branches"]) * DY))
    add_edge({"id": "e_%s_false" % if_node["id"], "source": if_node["id"],
              "sourceHandle": fallback_class, "target": chat_chain[0]["id"]})
    for a, b in zip(chat_chain, chat_chain[1:]):
        add_edge({"id": "e_%s_%s" % (a["id"], b["id"]), "source": a["id"],
                  "sourceHandle": "source", "target": b["id"]})

    classes.append({
        "id": fallback_class,
        "name": "普通闲聊、无法归类的其他问题或一般知识问答",
    })
    if_node_data = copy.deepcopy(if_node["data"])
    if_node_data.pop("cases", None)
    if_node_data.pop("logical_operator", None)
    if_node_data.update({
        "type": "question-classifier",
        "title": "意图分类",
        "desc": "使用 LLM 语义判断用户意图，再进入对应业务链路",
        "query_variable_selector": ["sys", "query"],
        "model": copy.deepcopy(model_tpl["data"]["model"]),
        "classes": classes,
        "instruction": (
            "请根据用户问题选择最匹配的业务类别。必须优先判断情绪倾诉、"
            "自伤或轻生风险；查询记录必须优先于新增申请；知识、政策、流程、"
            "费用咨询必须优先于办理动作。只有完全无法归入其他类别时才选择"
            "普通闲聊/其他问题。不得根据关键词机械匹配，应结合完整语义。"
        ),
        "vision": {"enabled": False},
    })
    if_node["data"] = if_node_data
    if_node["height"] = 140 + 42 * len(classes)

    g["nodes"] = nodes
    g["edges"] = edges
    return g


# ---------------------------------------------------------------------------
# 学生
# ---------------------------------------------------------------------------
STUDENT = {
    "auth_token": True,
    "tool_template": "tool_leave",
    "llm_template": "llm_leave",
    "answer_template": "answer_leave",
    "false_chain": ["llm_chat", "answer_chat"],
    "branches": [
        # Dify 的 contains 条件只能写一个字符串，不能把
        # “我的学籍/学籍阶段/已解锁”塞进同一个条件里；这里通过“宽关键词 + 分支顺序”
        # 实现等价效果。顺序必须保持：先查记录/数据，再识别帮助，最后才执行新增业务。
        {
            # 高风险情绪必须先于成绩、学籍等普通数据判断。
            # 例如“成绩不好，想自杀”不能被成绩分支截走，否则会漏掉危机预警。
            "case_id": "emotion_record",
            "conds": ["压力", "难受", "焦虑", "抑郁", "崩溃", "自杀",
                      "想不开", "活不下去", "失眠", "睡不着"],
            "chain": ["tool_emotion", "llm_emotion", "answer_emotion"],
        },
        {
            "case_id": "stage_info",
            "new": {
                "conds": ["我的学籍", "学籍阶段", "我现在什么阶段", "已解锁"],
                "tool_id": "tool_my_stage", "tool_title": "查询我的学籍阶段",
                "tool_name": "my_stage", "tool_label": "我的学籍阶段",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_my_stage", "llm_title": "整理学籍阶段",
                "ans_id": "answer_my_stage", "ans_title": "回复学籍阶段",
                "prompt": "接口返回当前学籍阶段和功能解锁情况：{{#tool_my_stage.text#}}。"
                          "请说明当前阶段、可用的服务、尚不可用的服务和下一步该做什么。",
            },
        },
        {
            "case_id": "leave_records",
            "new": {
                "conds": ["请假记录", "我的请假", "请假情况", "请过假"],
                "tool_id": "tool_my_leaves", "tool_title": "查询我的请假",
                "tool_name": "my_leaves", "tool_label": "查询我的请假记录",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_my_leaves", "llm_title": "整理请假记录",
                "ans_id": "answer_my_leaves", "ans_title": "回复请假记录",
                "prompt": "你是温暖的留学生管家。接口返回了该学生的请假记录：{{#tool_my_leaves.text#}}。"
                          "请用简洁列表说明每次请假的起止时间、理由、状态（待审批/已通过/已驳回）；"
                          "若已审批且员工留了意见，请一并转达。若没有记录，就说目前没有请假记录，需要请假可以直接告诉我。",
            },
        },
        {
            "case_id": "scores",
            "conds": ["成绩", "分数", "绩点"],
            "chain": ["tool_score", "llm_score", "answer_score"],
        },
        {
            "case_id": "psych_profile",
            "new": {
                "conds": ["心理画像", "情绪历史", "情绪记录"],
                "tool_id": "tool_psych_profile", "tool_title": "查询我的心理画像",
                "tool_name": "my_psych_profile", "tool_label": "我的心理画像",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_psych_profile", "llm_title": "整理心理画像",
                "ans_id": "answer_psych_profile", "ans_title": "回复心理画像",
                "prompt": "接口返回该学生最近的情绪画像：{{#tool_psych_profile.text#}}。"
                          "请概述最近情绪、趋势和最近记录时间，语气温和，不做医学诊断；"
                          "如出现高危记录，建议联系老师或专业支持人员。",
            },
        },
        {
            "case_id": "feedback_records",
            "new": {
                "conds": ["反馈记录", "售后记录", "投诉记录"],
                "tool_id": "tool_my_feedbacks", "tool_title": "查询我的售后工单",
                "tool_name": "my_feedbacks", "tool_label": "查询我的售后记录",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_my_feedbacks", "llm_title": "整理售后记录",
                "ans_id": "answer_my_feedbacks", "ans_title": "回复售后记录",
                "prompt": "接口返回该学生的售后工单：{{#tool_my_feedbacks.text#}}。"
                          "请说明工单内容、状态、处理方案和提交时间；没有记录就明确说明。",
            },
        },
        {
            "case_id": "application_progress",
            "new": {
                "conds": ["申请进度", "申请到哪", "申请什么阶段", "申请现在什么阶段",
                          "offer", "OFFER", "录取", "签证进度", "签证状态", "申请状态"],
                "tool_id": "tool_progress", "tool_title": "查询申请进度",
                "tool_name": "my_applications", "tool_label": "学生查看自己的申请进度",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_progress", "llm_title": "整理申请进度",
                "ans_id": "answer_progress", "ans_title": "回复申请进度",
                "prompt": "你是留学申请顾问。接口返回该学生的申请进度与时间轴：{{#tool_progress.text#}}。"
                          "请说明目标国家/院校/专业、当前阶段、总体进度百分比，并按时间顺序列出已完成的节点；"
                          "若还没有申请记录，就说明暂未建档，建议先联系顾问老师建立申请档案。",
            },
        },
        {
            "case_id": "registration_records",
            "new": {
                "conds": ["我的报名", "报名记录", "报过名", "报名了"],
                "tool_id": "tool_my_regs", "tool_title": "查询我的报名",
                "tool_name": "my_registrations", "tool_label": "我报名的活动",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_my_regs", "llm_title": "整理报名记录",
                "ans_id": "answer_my_regs", "ans_title": "回复报名记录",
                "prompt": "接口返回该学生已报名的活动：{{#tool_my_regs.text#}}。请列出活动名称、时间、地点、报名时间；"
                          "没有记录就说明还没报名过活动，可以到「活动报名」页看看近期讲座。",
            },
        },
        {
            "case_id": "events",
            "new": {
                # “活动”已覆盖近期活动、活动时间、活动地点、活动报名等常见说法；
                # “讲座/宣讲会/展会”覆盖活动类型，不再逐句枚举。
                "conds": ["活动", "讲座", "宣讲", "展会", "见面会", "联展",
                          "报名", "分享会"],
                "tool_id": "tool_events", "tool_title": "查询近期活动",
                "tool_name": "content_events", "tool_label": "活动列表",
                "params": {},
                "llm_id": "llm_events", "llm_title": "整理活动信息",
                "ans_id": "answer_events", "ans_title": "回复活动",
                "prompt": "接口返回了近期活动列表：{{#tool_events.text#}}。有数据时最多挑最近的 3-5 场，"
                          "说明名称、时间、地点、面向国家，并提醒可到「活动报名」页报名；"
                          "返回空列表时明确说当前没有已发布活动，不得编造活动、时间或地点。",
            },
        },
        {
            "case_id": "notifications",
            "new": {
                "conds": ["通知", "消息", "提醒", "未读"],
                "tool_id": "tool_notify", "tool_title": "查询我的通知",
                "tool_name": "list_notifications", "tool_label": "我的通知列表",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_notify", "llm_title": "整理通知",
                "ans_id": "answer_notify", "ans_title": "回复通知",
                "prompt": "接口返回该学生的站内通知：{{#tool_notify.text#}}。请按时间倒序列出未读通知的标题与内容要点，"
                          "并提示还有多少条未读；没有通知就说明当前没有新消息。",
            },
        },
        {
            "case_id": "recommend",
            "new": {
                "profile_extract": True,
                # 推荐场景只保留明确的决策表达。不能把单独的“本科/专科/毕业”
                # 当作推荐词，否则“本科学费多少”会被误判成项目推荐。
                "conds": ["推荐", "适合我", "想读", "想升", "就业", "移民",
                          "提升学历", "学历提升", "拿本科", "读本科", "升本科",
                          "申请本科", "读硕士", "升硕士", "申请硕士",
                          "有什么方案", "哪个方案", "怎么选", "读什么", "学什么"],
                "tool_id": "tool_recommend", "tool_title": "画像匹配留学项目",
                "tool_name": "recommend_project", "tool_label": "项目推荐",
                "params": {
                    "raw_text": "{{#sys.query#}}",
                    "age": "{{#code_profile_extract.age#}}",
                    "education": "{{#code_profile_extract.education#}}",
                    "intent": "{{#code_profile_extract.intent#}}",
                    "language": "{{#code_profile_extract.language#}}",
                    "hands_on": "{{#code_profile_extract.hands_on#}}",
                    "economic_condition": "{{#code_profile_extract.economic_condition#}}",
                },
                "llm_id": "llm_recommend", "llm_title": "整理推荐结果",
                "ans_id": "answer_recommend", "ans_title": "回复项目推荐",
                "prompt": "推荐工具返回：{{#tool_recommend.text#}}。第一行必须直接写"
                          "「推荐【best_match.project】，匹配度 best_match.score 分」，不要反问偏好，不要先寒暄。"
                          "第二行开始最多列 3 条依据，总长度不超过 5 行。"
                          "只能推荐 matches 中明确出现的项目；不得增加英国、澳洲、加拿大、自考本科、"
                          "某某大学或其他工具未返回的项目。学制、学费、适合人群和政策只能采用 tool 返回的"
                          "knowledge.snippets 原文；snippets 没有的事实直接说“以公司资料和顾问确认为准”。"
                          "如果 best_match.caveats 有内容，必须直接说明这是一条“条件推荐”并列出前置条件。"
                          "回答结尾提醒学生可到左侧「留学方案」查看全部具体方向并一键带入申请。"
                          "如果 status=insufficient，只追问 profile 中缺失的 age/current_education；"
                          "已经提供的字段绝不能再次追问。两项都已提供仍无匹配时，说明公司现有项目暂未覆盖，"
                          "不得重复要求用户填写年龄或学历。其他情况直接给结论，不再追问偏好。",
            },
        },
        {
            # 知识咨询统一放在办理类分支之前：
            # “怎么请假/请假流程”走知识库；单独的“我要请假”不命中这些帮助词，
            # 会继续落到后面的 leave_apply，避免把咨询误办成请假单。
            "case_id": "knowledge_help",
            "conds": ["请假流程", "能否请假", "能请假", "可以请假",
                      "条件", "政策", "规定", "流程", "材料", "要求", "学费",
                      "院校", "专业", "签证", "怎么办", "怎么", "如何"],
            "chain": ["2858411665611", "llm_kb", "answer_kb"],
        },
        {
            "case_id": "leave_apply",
            "conds": ["请假", "请个假", "病假", "事假"],
            "chain": ["tool_leave", "llm_leave", "answer_leave"],
        },
        {
            "case_id": "feedback_create",
            "conds": ["反馈", "投诉", "售后", "报修"],
            "chain": ["tool_feedback", "llm_feedback", "answer_feedback"],
        },
    ],
}

# ---------------------------------------------------------------------------
# 员工
# ---------------------------------------------------------------------------
EMPLOYEE = {
    "auth_token": True,
    "tool_template": "tool_search",
    "llm_template": "llm_search",
    "answer_template": "answer_search",
    "false_chain": ["llm_chat", "answer_chat"],
    "branches": [
        {
            "case_id": "case_pending",
            "new": {
                # “请假审批”本身有歧义，不能再作为唯一命中词：
                # “待审批”表示查数据，“请假审批流程”应进入知识库。
                "conds": ["待审批", "谁请假"],
                "tool_id": "tool_pending", "tool_title": "查询待审批请假",
                "tool_name": "pending_leaves", "tool_label": "待审批请假列表",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_pending", "llm_title": "整理待审批",
                "ans_id": "answer_pending", "ans_title": "回复待审批",
                "prompt": "接口返回待审批的请假列表：{{#tool_pending.text#}}。请按提交时间列出学生姓名、请假起止、理由、提交时间，"
                          "并在结尾提示：可在「请假审批」页选择通过（可写留言）或驳回（需填理由）。",
            },
        },
        {
            "case_id": "case_regs",
            "new": {
                "conds": ["报名名单", "谁报名", "报名情况", "报名人数"],
                "tool_id": "tool_regs", "tool_title": "查询报名名单",
                "tool_name": "event_registrations", "tool_label": "活动报名名单",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_regs", "llm_title": "整理报名名单",
                "ans_id": "answer_regs", "ans_title": "回复报名名单",
                "prompt": "接口返回活动报名名单：{{#tool_regs.text#}}。请按活动分组统计报名人数，列出报名人姓名与联系方式；"
                          "如数据为空，说明目前还没有人报名。",
            },
        },
        {
            "case_id": "case_country",
            "new": {
                "conds": ["国家分布", "各国家", "哪个国家", "意向国家", "按国家"],
                "tool_id": "tool_country", "tool_title": "学生国家分布",
                "tool_name": "students_by_country", "tool_label": "按意向国家分组的学生",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_country", "llm_title": "整理国家分布",
                "ans_id": "answer_country", "ans_title": "回复国家分布",
                "prompt": "接口返回按意向国家分组的学生：{{#tool_country.text#}}。请用表格或清单形式给出每个国家的学生人数，"
                          "并指出当前最热门的方向；可提示到后台「学生分布」页查看具体名单。",
            },
        },
        {
            "case_id": "case_leads",
            "new": {
                "conds": ["线索", "留资", "意向客户", "客户跟进"],
                "tool_id": "tool_leads", "tool_title": "查询客户线索",
                "tool_name": "list_leads", "tool_label": "全部线索",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_leads", "llm_title": "整理线索",
                "ans_id": "answer_leads", "ans_title": "回复线索",
                "prompt": "接口返回全部客户线索：{{#tool_leads.text#}}。请列出客户姓名、联系方式、意向国家、当前阶段与负责员工，"
                          "并提示可在「客户线索」页更新阶段、追加跟进记录。",
            },
        },
        {
            "case_id": "case_apps",
            "new": {
                "conds": ["申请进度", "申请情况", "申请到哪", "全部申请"],
                "tool_id": "tool_apps", "tool_title": "查询全部申请",
                "tool_name": "all_applications", "tool_label": "全部申请进度",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_apps", "llm_title": "整理申请进度",
                "ans_id": "answer_apps", "ans_title": "回复申请进度",
                "prompt": "接口返回全部学生的申请进度：{{#tool_apps.text#}}。请按阶段汇总（选校/材料/递交/面试/Offer/签证/行前/入学）"
                          "各有多少人，并列出需要推进的申请；提示可在「申请管理」页推进阶段。",
            },
        },
        {
            "case_id": "case_alerts",
            "new": {
                "conds": ["预警", "心理", "高危", "情绪"],
                "tool_id": "tool_alerts", "tool_title": "查询心理预警",
                "tool_name": "list_alerts", "tool_label": "心理预警列表",
                "params": {"authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_alerts", "llm_title": "整理预警",
                "ans_id": "answer_alerts", "ans_title": "回复预警",
                "prompt": "接口返回心理预警列表：{{#tool_alerts.text#}}。请优先列出「未处理」的高风险学生（姓名、情绪分值、触发原因、时间），"
                          "提醒尽快介入，并说明可在「心理预警」页标记已处理并填写跟进备注。",
            },
        },
        {
            "case_id": "case_search",
            "new": {
                "conds": ["查客户", "查同学", "查学生", "学生资料", "学生档案",
                          "学生信息", "客户资料", "客户档案", "跟进记录",
                          "搜索客户", "查询学生"],
                "tool_id": "tool_search", "tool_title": "统一搜索学生与客户",
                "tool_name": "search_customer", "tool_label": "学生与客户检索",
                "params": {"name": "{{#sys.query#}}",
                           "authorization": "Bearer {{#start_node.auth_token#}}"},
                "llm_id": "llm_search", "llm_title": "整理学生与客户信息",
                "ans_id": "answer_search", "ans_title": "回复学生与客户检索",
                "prompt": "统一检索结果为：{{#tool_search.text#}}。"
                          "返回对象可能同时包含 students 和 leads。"
                          "如果 students 非空，先明确说明这是学生档案，列出姓名、账号、联系方式、"
                          "意向国家和学籍阶段；如果用户问的是 CRM 跟进记录但 leads 为空，"
                          "要说明该学生存在，但当前没有客户线索跟进记录，不能回答成完全没有档案。"
                          "如果 leads 非空，再单独说明客户线索状态和跟进记录。"
                          "两类都为空时，才明确说没有找到匹配的学生或客户。",
            },
        },
        {
            "case_id": "case_report",
            "conds": ["写日报", "提交日报", "汇报今天"],
            "chain": ["tool_report", "llm_report", "answer_report"],
        },
        {
            "case_id": "case_stat",
            "conds": ["统计", "有多少"],
            "chain": ["tool_stat", "llm_stat", "answer_stat"],
        },
        {
            "case_id": "case_kb",
            # 审批帮助、日报帮助和普通知识问答原本是三个同链路分支，现合并为一个。
            # 该分支必须放在待审批、写日报等数据/办理分支之后，避免吞掉实际业务操作。
            "conds": ["审批", "日报", "新人", "入职", "公司", "企业", "画像",
                      "研判", "规则", "制度", "流程", "怎么办", "如何",
                      "福利", "考勤", "员工"],
            "chain": ["kb_employee", "llm_kb", "answer_kb"],
        },
    ],
}

# ---------------------------------------------------------------------------
# 客服（免登录）
# ---------------------------------------------------------------------------
CUSTOMER = {
    "auth_token": False,
    "tool_template": "tool_events",
    "llm_template": "llm_event",
    "answer_template": "answer_event",
    "false_chain": ["kb_node", "llm_kb", "answer_kb"],
    "branches": [
        # 具体方案查询放在推荐之前，避免“留学方案”被“方案推荐”误判。
        {
            "case_id": "case_program",
            "new": {
                "conds": ["留学方案", "项目介绍", "专业方向", "哪个国家"],
                "tool_id": "tool_programs", "tool_title": "查询留学方案",
                "tool_name": "list_programs", "tool_label": "留学方案列表",
                "params": {},
                "llm_id": "llm_programs", "llm_title": "整理留学方案",
                "ans_id": "answer_programs", "ans_title": "回复留学方案",
                "prompt": "接口返回留学方案列表：{{#tool_programs.text#}}。请按国家分组介绍项目名称、类别、亮点与适合人群，"
                          "最多挑 6 个；结尾引导用户留下联系方式，由顾问一对一评估。",
            },
        },
        {
            "case_id": "case_recommend",
            "new": {
                "profile_extract": True,
                # 与后端推荐判断保持同一口径：主题词“本科”不能单独触发推荐。
                "conds": ["推荐", "适合我", "想读", "想升", "就业", "移民",
                          "提升学历", "学历提升", "拿本科", "读本科", "升本科",
                          "申请本科", "读硕士", "升硕士", "申请硕士",
                          "有什么方案", "有什么项目", "有哪些项目", "项目推荐",
                          "个人情况", "年龄和学历",
                          "哪个方案", "怎么选", "读什么", "学什么"],
                "tool_id": "tool_recommend", "tool_title": "画像匹配留学项目",
                "tool_name": "recommend_project", "tool_label": "项目推荐",
                "params": {
                    "raw_text": "{{#sys.query#}}",
                    "age": "{{#code_profile_extract.age#}}",
                    "education": "{{#code_profile_extract.education#}}",
                    "intent": "{{#code_profile_extract.intent#}}",
                    "language": "{{#code_profile_extract.language#}}",
                    "hands_on": "{{#code_profile_extract.hands_on#}}",
                    "economic_condition": "{{#code_profile_extract.economic_condition#}}",
                },
                "llm_id": "llm_recommend", "llm_title": "整理项目推荐",
                "ans_id": "answer_recommend", "ans_title": "回复项目推荐",
                "prompt": "推荐工具返回：{{#tool_recommend.text#}}。"
                          "先读取 status：只有 status=match 且 best_match.project 非空时，"
                          "第一行才写「推荐【best_match.project】，匹配度 best_match.score 分」，"
                          "之后最多列 3 条理由，结尾引导到官网「留学方案」查看具体方向。"
                          "只能推荐 matches 中明确列出的项目，不得添加任何工具未返回的项目。"
                          "学制、学费、适合人群和政策只能引用 tool 返回的 knowledge.snippets；"
                          "没有依据就写“以公司资料和顾问确认为准”。"
                          "若 status=insufficient，只追问 profile 中缺失的 age/current_education，"
                          "已经提供的字段绝不能再次追问。"
                          "若 status=no_match，禁止输出项目名、匹配度或 0 分，"
                          "应委婉说明公司现有项目暂未覆盖当前情况，请客户补充真实目标，"
                          "或建议顾问进一步确认，不能编造方案。",
            },
        },
        {
            "case_id": "case_event",
            # 使用当前统一活动接口 content_events，不再使用旧 list_events。
            # 旧工具曾在运行库返回 404，随后把工具异常误说成“角色无权访问”。
            "new": {
                "conds": ["活动", "讲座", "宣讲", "展会", "见面会", "联展",
                          "报名", "分享会"],
                "tool_id": "tool_events", "tool_title": "查询活动列表",
                "tool_name": "content_events", "tool_label": "活动列表",
                "params": {},
                "llm_id": "llm_event", "llm_title": "整理活动信息",
                "ans_id": "answer_event", "ans_title": "回复活动",
                "prompt": "接口返回已发布活动列表：{{#tool_events.text#}}。"
                          "请按时间顺序列出活动名称、时间、地点和剩余名额，最多 5 场；"
                          "没有数据时明确说当前没有已发布活动，不得编造。"
                          "结尾提醒用户可以到官网「最新活动」或对应报名页面报名。",
            },
        },
        {
            "case_id": "case_course",
            "conds": ["课程", "学费"],
            "chain": ["tool_courses", "llm_course", "answer_course"],
        },
    ],
}


def dump_dsl(app, graph, src_yml):
    """把新图写回可导入的 DSL（保留 app/features 等元信息）。"""
    with open(os.path.join(HERE, src_yml), encoding="utf-8") as f:
        dsl = yaml.safe_load(f)
    dsl["workflow"]["graph"] = graph
    with open(os.path.join(HERE, src_yml), "w", encoding="utf-8") as f:
        f.write("# 自动生成：由 patch_workflows.py 从 Dify 实际图生成\n")
        yaml.safe_dump(dsl, f, allow_unicode=True, sort_keys=False, width=200)


def main():
    for app, spec, yml in [
        ("student", STUDENT, "student_workflow.yml"),
        ("employee", EMPLOYEE, "employee_workflow.yml"),
        ("customer", CUSTOMER, "customer_workflow.yml"),
    ]:
        graph = build_graph(app, spec)
        out = os.path.join(LIVE, "%s_new.json" % app)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False)
        dump_dsl(app, graph, yml)
        print("%-9s -> %d nodes, %d edges, %d 分支" % (
            app, len(graph["nodes"]), len(graph["edges"]), len(spec["branches"])))


if __name__ == "__main__":
    main()
