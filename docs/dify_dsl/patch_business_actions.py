"""为学生和员工 Dify 工作流增加通用业务动作编排节点。"""
from __future__ import annotations

import json
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
LIVE = HERE / "live"

OUTPUT_RULES = """
【输出规范】
1. 直接给结论，不输出思考过程。
2. 使用简体中文自然分段。多条候选、项目、活动、成绩、学生、请假、线索或工单
   使用 Markdown 表格；单条确认、追问和错误说明使用自然语言。
3. 不重复追问已经明确的字段。
""".strip()

TABLE_MARKER = "【结构化数据表格规范】"
TABLE_TEXT = (
    "\n\n【结构化数据表格规范】\n"
    "当回答中包含两条及以上项目、活动、学生、成绩、请假、审批、报名、"
    "线索、工单或统计记录时，必须用 Markdown 表格输出。"
    "表头必须来自数据实际字段；一行动作一条记录；不得把多条记录写成连续长段落。"
    "普通说明、单条确认、追问和错误提示继续使用自然语言。"
)

GROUNDING_MARKER = "【事实边界】"
GROUNDING_TEXT = (
    "\n\n【事实边界】\n"
    "涉及公司、项目、费用、政策、签证、活动、学籍和业务数据时，"
    "只能依据工具返回或公司已提供的政策、项目与业务资料回答。没有依据时"
    "直接说“公司目前暂未提供这项信息，建议由顾问进一步确认”，"
    "不得使用模型常识补全，不得推断客户符合某项资格，"
    "也不得承诺审批或办理结果。"
)


def base(node_id, node_type, title, x, y, height=110):
    return {
        "id": node_id,
        "data": {"type": node_type, "title": title, "desc": "", "selected": False},
        "type": "custom",
        "width": 242,
        "height": height,
        "position": {"x": x, "y": y},
        "selected": False,
        "sourcePosition": "right",
        "targetPosition": "left",
        "positionAbsolute": {"x": x, "y": y},
    }


def llm(node_id, title, prompt, x, y):
    node = base(node_id, "llm", title, x, y, 88)
    node["data"].update({
        "model": {
            "mode": "chat",
            "name": "qwen3.8-max",
            "provider": "langgenius/tongyi/tongyi",
            "completion_params": {"temperature": 0.2, "enable_thinking": False},
        },
        "vision": {"enabled": False},
        "context": {"enabled": False, "variable_selector": []},
        "prompt_template": [
            {"role": "system", "text": prompt},
            {"role": "user", "text": "{{#sys.query#}}"},
        ],
        "memory": {
            "window": {"size": 10, "enabled": True},
            "role_prefix": {"user": "", "assistant": ""},
            "query_prompt_template": "{{#sys.query#}}",
        },
    })
    return node


def answer(node_id, title, value, x, y):
    node = base(node_id, "answer", title, x, y, 103)
    node["data"].update({"answer": value, "variables": []})
    return node


def code(node_id, title, source, variables, outputs, x, y):
    node = base(node_id, "code", title, x, y, 120)
    node["data"].update({
        "code_language": "python3",
        "code": source,
        "variables": variables,
        "outputs": {key: {"type": "string", "children": None} for key in outputs},
        "dependencies": [],
    })
    return node


def tool(node_id, title, tool_name, label, params, x, y):
    node = base(node_id, "tool", title, x, y, 96)
    node["data"].update({
        "provider_id": "1c2de193-75e4-4c78-8366-99b5f4e1ca00",
        "provider_name": "yuejiao_api",
        "provider_type": "api",
        "tool_name": tool_name,
        "tool_label": label,
        "tool_configurations": {},
        "tool_parameters": {
            name: {"type": "mixed", "value": value}
            for name, value in params.items()
        },
        "tool_node_version": "2",
        "error_strategy": "default-value",
        "default_value": [{
            "key": "text", "type": "string",
            "value": '{"code":500,"msg":"业务服务暂不可用","data":{"status_token":"STATE_ERROR","message":"业务服务暂不可用"}}',
        }],
    })
    return node


def edge(edge_id, source, target, source_handle="source"):
    return {
        "id": edge_id, "source": source, "sourceHandle": source_handle,
        "target": target, "targetHandle": "target",
    }


def ensure_variable(start, name, label, default="0"):
    variables = start["data"].setdefault("variables", [])
    if not any(item.get("variable") == name for item in variables):
        variables.append({
            "type": "text-input", "label": label, "default": default,
            "options": [], "required": False, "variable": name,
        })


def patch_graph(name):
    path = LIVE / (name + "_new.json")
    graph = json.loads(path.read_text(encoding="utf-8"))
    nodes = {item["id"]: item for item in graph["nodes"]}
    classifier = nodes["if_node"]
    start = nodes["start_node"]
    ensure_variable(start, "auth_token", "auth token", "")
    if name == "customer":
        ensure_variable(start, "visitor_key", "stable public visitor key", "")
    ensure_variable(start, "action_pending", "是否存在业务动作草稿（1/0）")
    ensure_variable(start, "pending_action_type", "待处理业务动作类型")
    ensure_variable(start, "recent_history", "最近几轮用户消息（仅用于补全上下文）", "")

    custom_ids = {
        "code_action_context", "llm_action_extract", "code_action_extract",
        "tool_action_prepare", "code_action_prepare", "if_action_state",
        "llm_action_ask", "llm_action_confirm", "tool_action_execute",
        "llm_action_result", "llm_action_terminal", "answer_action_ask",
        "answer_action_confirm", "answer_action_result", "answer_action_terminal",
    }
    old_edges = [
        item for item in graph["edges"]
        if item.get("source") not in custom_ids and item.get("target") not in custom_ids
        and item["id"] not in (
            "e_start_if", "e_start_action_context", "e_action_context_if",
            "e_if_node_business_action", "e_if_node_business_continue",
            "e_if_node_feedback_create",
        )
    ]
    for node_id in custom_ids:
        nodes.pop(node_id, None)

    # 三个 Agent 的全部 LLM 节点统一增加事实边界，防止知识库无答案时
    # 模型自行扩展签证、费用、学校或项目政策。
    for node in nodes.values():
        data = node.get("data") or {}
        if data.get("type") != "llm":
            continue
        for prompt in data.get("prompt_template") or []:
            if prompt.get("role") == "system" \
                    and GROUNDING_MARKER not in str(prompt.get("text") or ""):
                prompt["text"] = str(prompt.get("text") or "").rstrip() + GROUNDING_TEXT

    if name == "student" and "code_leave_context" in nodes:
        context_id = "code_leave_context"
        nodes[context_id]["data"]["code"] = """def main(query: str, leave_pending: str, action_pending: str, pending_action_type: str) -> dict:
    leave_state = '当前存在请假草稿。' if str(leave_pending or '0') == '1' else '当前没有请假草稿。'
    action_state = ('当前存在待处理业务动作：' + str(pending_action_type or '')) if str(action_pending or '0') == '1' else '当前没有业务动作草稿。'
    return {'context_query': leave_state + '\\n' + action_state + '\\n用户本轮输入：' + str(query or '')}
"""
        nodes[context_id]["data"]["variables"] = [
            {"variable": "query", "value_selector": ["sys", "query"]},
            {"variable": "leave_pending", "value_selector": ["start_node", "leave_pending"]},
            {"variable": "action_pending", "value_selector": ["start_node", "action_pending"]},
            {"variable": "pending_action_type", "value_selector": ["start_node", "pending_action_type"]},
        ]
        context_out = ["code_leave_context", "context_query"]
    else:
        context_id = "code_action_context"
        context_node = code(
            context_id, "补全业务动作上下文",
            """def main(query: str, action_pending: str, pending_action_type: str) -> dict:
    state = ('当前存在待处理业务动作：' + str(pending_action_type or '')) if str(action_pending or '0') == '1' else '当前没有业务动作草稿。'
    return {'context_query': state + '\\n用户本轮输入：' + str(query or '')}
""",
            [
                {"variable": "query", "value_selector": ["sys", "query"]},
                {"variable": "action_pending", "value_selector": ["start_node", "action_pending"]},
                {"variable": "pending_action_type", "value_selector": ["start_node", "pending_action_type"]},
            ],
            ["context_query"], 350, 320,
        )
        nodes[context_id] = context_node
        context_out = ["code_action_context", "context_query"]
        old_edges.append(edge("e_start_action_context", "start_node", "code_action_context"))
        old_edges.append(edge("e_action_context_if", "code_action_context", "if_node"))

    if name == "student":
        allowed = (
            "event_register、feedback_create、feedback_cancel、"
            "application_create、application_update、application_cancel"
        )
        extra_rule = (
            "查询成绩仍使用原成绩工具；活动报名、售后提交/撤回、"
            "就读申请新增/修改/撤销进入通用动作。"
        )
    elif name == "employee":
        allowed = (
            "event_register、score_query、score_upsert、score_delete、"
            "lead_create、lead_follow_up、report_create、feedback_resolve、"
            "application_advance"
        )
        extra_rule = (
            "员工录入客户线索、跟进客户、提交日报、解决售后、"
            "推进申请阶段、查询/新增/修改/删除成绩、报名内部活动进入通用动作。"
        )
    if name == "customer":
        allowed = "event_register"
        extra_rule = (
            "Public visitors may only query or register for public events. "
            "Company, policy, course and fee questions must use the knowledge base."
        )
        kb_llm = nodes.get("llm_kb")
        if kb_llm:
            for prompt in kb_llm.get("data", {}).get("prompt_template", []):
                if prompt.get("role") == "system":
                    prompt["text"] = (
                        "你是公司留学顾问。本次从公司业务资料中检索到的内容如下：\n"
                        "{{#kb_node.result#}}\n\n"
                        "请严格根据上述检索内容回答客户。"
                        "资料中有明确答案时，用自然、亲切的中文直接回答，不要提资料来源。"
                        "资料为空、没有相关内容，或只能靠常识猜测时，不要补全，"
                        "应礼貌说明“公司目前暂未提供这项信息，建议由顾问进一步确认”，"
                        "并追问客户的具体目标。不得推断签证资格、录取结果、费用或政策承诺。"
                    )

    nodes["llm_action_extract"] = llm(
        "llm_action_extract", "LLM 提取业务动作",
        """你是业务动作字段抽取器。当前待处理动作类型：{{#start_node.pending_action_type#}}。
最近用户消息（仅补全上下文，不得覆盖本轮明确表达）：
{{#start_node.recent_history#}}
只输出严格 JSON，不输出解释：
{"action_type":"", "user_action":"collect|confirm|modify|cancel", "slots":{...}}
允许的 action_type：""" + allowed + """。
slots 可用字段：event_id,event_name,selection,participant_name,contact_info,
student_id,student_name,course_name,score_id,score_value,
feedback_content,feedback_detail,ticket_id,ticket_content,customer_name,
target_country,background_info,application_id,country,application_mode,
program,university,remark,application_stage,note,report_content,report_date,
lead_id,lead_status,follow_up_note,solution。
申请动作的国家字段必须使用 country；target_country 只用于客户线索。
规则：明确确认/提交 -> confirm；明确取消 -> cancel；明确修改 -> modify；其他 collect。
不得补写用户没有提供的字段；无法确定的字段保持空字符串。""" + extra_rule,
        650, 320,
    )
    nodes["code_action_extract"] = code(
        "code_action_extract", "解析业务动作字段",
        """def main(raw: str) -> dict:
    import json, re
    text = (raw or '').strip()
    text = re.sub(r'^```(?:json)?\\s*', '', text, flags=re.I)
    text = re.sub(r'\\s*```$', '', text)
    try: data = json.loads(text)
    except Exception: data = {}
    slots = data.get('slots') if isinstance(data.get('slots'), dict) else {}
    clean = {str(k): v for k, v in slots.items() if v not in (None, '', [], {})}
    action = str(data.get('user_action') or 'collect').lower()
    if action not in ('collect','confirm','modify','cancel'): action = 'collect'
    return {'action_type': str(data.get('action_type') or ''), 'user_action': action,
            'slots_json': json.dumps(clean, ensure_ascii=False)}
""",
        [{"variable": "raw", "value_selector": ["llm_action_extract", "text"]}],
        ["action_type", "user_action", "slots_json"], 950, 320,
    )
    nodes["tool_action_prepare"] = tool(
        "tool_action_prepare", "准备业务动作",
        "prepare_business_action", "补字段、消歧和确认",
        {
            "token": "{{#start_node.auth_token#}}",
            "query": "{{#sys.query#}}",
            "history": "{{#start_node.recent_history#}}",
            "action_type": "{{#code_action_extract.action_type#}}",
            "user_action": "{{#code_action_extract.user_action#}}",
            "slots_json": "{{#code_action_extract.slots_json#}}",
            "visitor_key": "{{#start_node.visitor_key#}}",
        },
        1250, 320,
    )
    nodes["code_action_prepare"] = code(
        "code_action_prepare", "解析业务准备结果",
        """def main(raw: str) -> dict:
    import json
    try:
        payload = json.loads((raw or '').strip())
        if isinstance(payload, str): payload = json.loads(payload)
        if isinstance(payload, dict) and isinstance(payload.get('data'), dict): payload = payload['data']
    except Exception: payload = {}
    missing = payload.get('missing_fields') or []
    candidates = payload.get('candidates') or []
    lines = []
    for item in candidates:
        if isinstance(item, dict): lines.append(str(item.get('label') or item))
    return {
        'status_token': str(payload.get('status_token') or 'STATE_ERROR'),
        'message': str(payload.get('message') or ''),
        'preview': str(payload.get('preview') or ''),
        'missing_text': '、'.join(str(v) for v in missing) or '无',
        'candidates_text': '\\n'.join(lines) or '无',
        'confirmation_token': str(payload.get('confirmation_token') or ''),
    }
""",
        [{"variable": "raw", "value_selector": ["tool_action_prepare", "text"]}],
        ["status_token", "message", "preview", "missing_text", "candidates_text", "confirmation_token"],
        1550, 320,
    )
    state_if = base("if_action_state", "if-else", "业务动作状态分支", 1850, 320, 260)
    state_if["data"].update({
        "logical_operator": "or",
        "cases": [
            {
                "case_id": "case_action_missing", "id": "case_action_missing",
                "logical_operator": "or",
                "conditions": [
                    {"id": "c_action_missing", "comparison_operator": "contains", "value": "STATE_MISSING",
                     "varType": "string", "variable_selector": ["code_action_prepare", "status_token"]},
                    {"id": "c_action_ambiguous", "comparison_operator": "contains", "value": "STATE_AMBIGUOUS",
                     "varType": "string", "variable_selector": ["code_action_prepare", "status_token"]},
                ],
            },
            {
                "case_id": "case_action_confirm", "id": "case_action_confirm",
                "logical_operator": "and",
                "conditions": [{"id": "c_action_confirm", "comparison_operator": "contains",
                                "value": "STATE_CONFIRM", "varType": "string",
                                "variable_selector": ["code_action_prepare", "status_token"]}],
            },
            {
                "case_id": "case_action_ready", "id": "case_action_ready",
                "logical_operator": "and",
                "conditions": [{"id": "c_action_ready", "comparison_operator": "contains",
                                "value": "STATE_READY", "varType": "string",
                                "variable_selector": ["code_action_prepare", "status_token"]}],
            },
        ],
    })
    nodes["if_action_state"] = state_if
    nodes["llm_action_ask"] = llm(
        "llm_action_ask", "业务字段追问",
        "业务信息不完整或有多个候选。\n后端提示：{{#code_action_prepare.message#}}\n"
        "缺失字段：{{#code_action_prepare.missing_text#}}\n"
        "候选列表：\n{{#code_action_prepare.candidates_text#}}\n"
        "只追问缺失项；有候选时列出候选让用户选择；已明确字段不要重复追问。"
        + OUTPUT_RULES,
        2150, 80,
    )
    nodes["llm_action_confirm"] = llm(
        "llm_action_confirm", "业务动作复述确认",
        "请复述以下操作并等待用户确认，绝不能声称已经执行：\n"
        "{{#code_action_prepare.preview#}}\n最后提示回复“确认”执行，回复“修改”重新填写。"
        + OUTPUT_RULES,
        2150, 270,
    )
    nodes["tool_action_execute"] = tool(
        "tool_action_execute", "执行业务动作",
        "execute_business_action", "确认令牌校验后执行",
        {
            "token": "{{#start_node.auth_token#}}",
            "confirmation_token": "{{#code_action_prepare.confirmation_token#}}",
            "visitor_key": "{{#start_node.visitor_key#}}",
        },
        2150, 460,
    )
    nodes["llm_action_result"] = llm(
        "llm_action_result", "生成业务动作结果",
        "业务执行接口返回：{{#tool_action_execute.text#}}。请准确说明执行结果；"
        "失败时说明失败原因，不得声称成功。" + OUTPUT_RULES,
        2450, 460,
    )
    nodes["llm_action_terminal"] = llm(
        "llm_action_terminal", "业务动作错误说明",
        "业务准备状态：{{#code_action_prepare.status_token#}}\n"
        "后端提示：{{#code_action_prepare.message#}}\n"
        "请如实说明取消、重复、权限或错误原因。" + OUTPUT_RULES,
        2150, 650,
    )
    nodes["answer_action_ask"] = answer("answer_action_ask", "回复业务追问", "{{#llm_action_ask.text#}}", 2450, 80)
    nodes["answer_action_confirm"] = answer("answer_action_confirm", "回复业务确认", "{{#llm_action_confirm.text#}}", 2450, 270)
    nodes["answer_action_result"] = answer("answer_action_result", "回复业务结果", "{{#llm_action_result.text#}}", 2750, 460)
    nodes["answer_action_terminal"] = answer("answer_action_terminal", "回复业务错误", "{{#llm_action_terminal.text#}}", 2450, 650)

    classes = classifier["data"]["classes"]
    ids = {item["id"] for item in classes}
    for item in classes:
        if item.get("id") == "case_report":
            item["name"] = "查询日报记录；查看历史日报"
        elif item.get("id") == "case_kb":
            item["name"] = "审批；新人；入职；公司；企业"
        elif name == "student" and item.get("id") == "application_progress":
            item["name"] = "查询申请进度；申请到哪一步；当前申请状态；offer状态"
        elif name == "student" and item.get("id") == "recommend":
            item["name"] = "推荐方案；适合我；升学路径；就业方向；移民规划"
        elif name == "customer" and item.get("id") == "case_program":
            item["name"] = "查询全部留学方案目录；按国家或专业了解项目介绍"
        elif name == "customer" and item.get("id") == "case_recommend":
            item["name"] = (
                "根据年龄、学历和诉求推荐具体项目；个人情况咨询；"
                "有什么项目适合我"
            )
    fallback_index = next((i for i, item in enumerate(classes) if item["id"] == "fallback_general"), len(classes))
    if "business_continue" not in ids:
        classes.insert(fallback_index, {
            "id": "business_continue",
            "name": "业务动作续办：补充字段、从候选选择、确认、修改或取消（仅当前存在业务动作草稿时）",
        })
        fallback_index += 1
    if "business_action" not in ids:
        classes.insert(fallback_index, {
            "id": "business_action",
            "name": (
                "活动报名、售后提交/解决/撤回、成绩新增修改删除、"
                "客户线索录入/跟进、员工日报、就读申请新增修改撤销/推进等"
                "需要执行或消歧的业务动作"
            ),
        })
    for item in classes:
        if item.get("id") == "business_action":
            item["name"] = (
                "活动报名、售后提交/解决/撤回、成绩新增修改删除、"
                "客户线索录入/跟进、员工日报、就读申请新增修改撤销/推进等"
                "需要执行或消歧的业务动作"
            )
    classifier["data"]["query_variable_selector"] = context_out
    _instruction = classifier["data"].get("instruction", "")
    _marker = "当前上下文明确存在业务动作草稿时"
    if _marker in _instruction:
        _instruction = _instruction.split(_marker, 1)[0].rstrip()
    classifier["data"]["instruction"] = _instruction + (
        " 当前上下文明确存在业务动作草稿时，补充、选择、确认、修改或取消优先进入“业务动作续办”。"
        " 明确要求报名、申请就读、修改或撤销申请、提交售后/日报、"
        "录入或跟进客户、新增修改删除成绩时，进入“业务动作”。"
    )
    if name == "customer":
        customer_rule = (
            " 用户同时提供年龄、学历或当前背景并询问“有什么项目/方案适合我”时，"
            "必须进入项目推荐，不得进入方案目录列表；仅询问全部方案目录、"
            "按国家或专业了解项目时才进入方案目录。"
        )
        if "必须进入项目推荐" not in classifier["data"]["instruction"]:
            classifier["data"]["instruction"] += customer_rule
    classifier["height"] = 140 + 42 * len(classes)

    edges = old_edges
    edges.extend([
        edge("e_if_node_business_action", "if_node", "llm_action_extract", "business_action"),
        edge("e_if_node_business_continue", "if_node", "llm_action_extract", "business_continue"),
        edge("e_action_extract_code", "llm_action_extract", "code_action_extract"),
        edge("e_action_code_prepare", "code_action_extract", "tool_action_prepare"),
        edge("e_action_prepare_code", "tool_action_prepare", "code_action_prepare"),
        edge("e_action_prepare_if", "code_action_prepare", "if_action_state"),
        edge("e_action_missing_ask", "if_action_state", "llm_action_ask", "case_action_missing"),
        edge("e_action_confirm", "if_action_state", "llm_action_confirm", "case_action_confirm"),
        edge("e_action_ready", "if_action_state", "tool_action_execute", "case_action_ready"),
        edge("e_action_false_terminal", "if_action_state", "llm_action_terminal", "false"),
        edge("e_action_execute_result", "tool_action_execute", "llm_action_result"),
        edge("e_action_ask_answer", "llm_action_ask", "answer_action_ask"),
        edge("e_action_confirm_answer", "llm_action_confirm", "answer_action_confirm"),
        edge("e_action_result_answer", "llm_action_result", "answer_action_result"),
        edge("e_action_terminal_answer", "llm_action_terminal", "answer_action_terminal"),
    ])
    if name == "student":
        edges = [
            item for item in edges
            if not (item.get("source") == "if_node" and item.get("sourceHandle") == "feedback_create")
        ]
        edges.append(edge("e_if_node_feedback_create", "if_node", "llm_action_extract", "feedback_create"))
    # 自定义动作节点是在函数中后创建的，因此必须在最终写盘前再统一追加
    # 一次事实边界，确保没有新增的 LLM 节点漏掉约束。同时清理最终话术里
    # 的内部架构词，客户看到的应是公司政策、项目和业务资料。
    for node in nodes.values():
        data = node.get("data") or {}
        title = str(data.get("title") or "")
        if title:
            data["title"] = (
                title.replace("检索知识库", "核对公司资料")
                .replace("回复知识库", "回复公司资料")
            )
        desc = str(data.get("desc") or "")
        if "知识库" in desc:
            data["desc"] = (
                desc.replace("检索知识库", "核对公司资料")
                .replace("回复知识库", "回复公司资料")
                .replace("你的知识库", "公司业务资料")
                .replace("知识库", "公司业务资料")
            )
        if data.get("type") != "llm":
            continue
        for prompt in data.get("prompt_template") or []:
            if prompt.get("role") != "system":
                continue
            text = str(prompt.get("text") or "")
            text = (
                text.replace("当前知识库中没有相关信息", "公司目前暂未提供相关信息")
                .replace("若知识库中没有相关信息", "若公司业务资料中没有相关信息")
                .replace("检索到的知识库上下文", "检索到的公司业务资料")
                .replace("知识库上下文", "公司业务资料")
                .replace("知识库中", "公司业务资料中")
                .replace("知识库", "公司业务资料")
            )
            if node.get("id") not in {
                "llm_profile_extract", "llm_action_extract", "llm_leave_extract",
            } and TABLE_MARKER not in text:
                text = text.rstrip() + TABLE_TEXT
            if GROUNDING_MARKER not in text:
                text = text.rstrip() + GROUNDING_TEXT
            prompt["text"] = text
    graph["nodes"] = list(nodes.values())
    graph["edges"] = edges
    path.write_text(json.dumps(graph, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return graph


def update_yaml(name, graph):
    path = HERE / (name + "_workflow.yml")
    if not path.exists():
        return
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(doc, dict) and isinstance(doc.get("workflow"), dict):
        doc["workflow"]["graph"] = graph
        path.write_text(
            "# 自动生成：patch_workflows.py + patch_leave_orchestration.py + patch_business_actions.py\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=120),
            encoding="utf-8",
        )


def main():
    for name in ("student", "employee", "customer"):
        update_yaml(name, patch_graph(name))
        print(name + " 通用业务动作编排已生成")


if __name__ == "__main__":
    main()
