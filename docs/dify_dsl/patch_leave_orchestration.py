"""把学生 Dify 工作流升级为完整的请假编排链路。

链路：
意图分类 → LLM 字段提取 → prepare_leave（草稿/日期归一化）
→ 条件分支 → 追问或复述确认 → confirm_leave → 自然语言回复

脚本只修改 live/student_new.json；执行 publish_runtime.py --apply 后写入 Dify。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml


HERE = Path(__file__).resolve().parent
GRAPH_PATH = HERE / "live" / "student_new.json"
DSL_PATH = HERE / "student_workflow.yml"


OUTPUT_RULES = """
【输出规范】
1. 直接给结论，不输出思考过程、工具调用说明。
2. 使用简体中文自然分段。多条请假记录、成绩或其他重复字段记录使用 Markdown 表格；
   单条申请确认、追问和错误说明使用自然语言。
3. 不重复已经正确提供的字段，不把“请假天数”作为额外必填项。
4. 日期范围完整时应自动说明天数，不再要求用户另报天数。
""".strip()


def node_base(node_id, node_type, title, x, y, width=242, height=110):
    return {
        "id": node_id,
        "data": {
            "type": node_type,
            "title": title,
            "desc": "",
            "selected": False,
        },
        "type": "custom",
        "width": width,
        "height": height,
        "position": {"x": x, "y": y},
        "selected": False,
        "sourcePosition": "right",
        "targetPosition": "left",
        "positionAbsolute": {"x": x, "y": y},
    }


def llm_node(node_id, title, system_prompt, x, y, memory=True):
    node = node_base(node_id, "llm", title, x, y, height=88)
    node["data"].update({
        "model": {
            "mode": "chat",
            "name": "qwen3.8-max",
            "provider": "langgenius/tongyi/tongyi",
            "completion_params": {
                "temperature": 0.2,
                "enable_thinking": False,
            },
        },
        "vision": {"enabled": False},
        "context": {"enabled": False, "variable_selector": []},
        "prompt_template": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": "{{#sys.query#}}"},
        ],
        "memory": {
            "window": {"size": 10, "enabled": memory},
            "role_prefix": {"user": "", "assistant": ""},
            "query_prompt_template": "{{#sys.query#}}",
        },
    })
    return node


def answer_node(node_id, title, answer, x, y):
    node = node_base(node_id, "answer", title, x, y, height=103)
    node["data"].update({"answer": answer, "variables": []})
    return node


def code_node(node_id, title, desc, code, variables, outputs, x, y):
    node = node_base(node_id, "code", title, x, y, height=120)
    node["data"].update({
        "desc": desc,
        "code_language": "python3",
        "code": code,
        "variables": variables,
        "outputs": {
            key: {"type": "string", "children": None}
            for key in outputs
        },
        "dependencies": [],
    })
    return node


def tool_node(node_id, title, tool_name, tool_label, params, x, y):
    node = node_base(node_id, "tool", title, x, y, height=96)
    node["data"].update({
        "desc": "工具节点：调用 " + tool_name + "（yuejiao_api）",
        "provider_id": "1c2de193-75e4-4c78-8366-99b5f4e1ca00",
        "provider_name": "yuejiao_api",
        "provider_type": "api",
        "tool_name": tool_name,
        "tool_label": tool_label,
        "tool_configurations": {},
        "tool_parameters": {
            name: {"type": "mixed", "value": value}
            for name, value in params.items()
        },
        "tool_node_version": "2",
        "error_strategy": "default-value",
        "default_value": [{
            "key": "text",
            "type": "string",
            "value": '{"code":500,"msg":"请假服务暂不可用","data":{"status_token":"STATE_ERROR","message":"请假服务暂不可用，请稍后重试"}}',
        }],
    })
    return node


def edge(edge_id, source, target, source_handle="source"):
    return {
        "id": edge_id,
        "source": source,
        "sourceHandle": source_handle,
        "target": target,
        "targetHandle": "target",
    }


def main():
    graph = json.loads(GRAPH_PATH.read_text(encoding="utf-8"))
    nodes = {node["id"]: node for node in graph["nodes"]}
    orchestration_node_ids = {
        "code_leave_context", "llm_leave_extract", "code_leave_extract",
        "tool_leave_prepare", "code_leave_prepare", "if_leave_state",
        "llm_leave_ask", "llm_leave_confirm", "tool_leave_confirm",
        "llm_leave_result", "llm_leave_terminal", "answer_leave_ask",
        "answer_leave_confirm", "answer_leave_result", "answer_leave_terminal",
    }
    edges = [
        item for item in graph["edges"]
        if item["id"] not in {
            "e_start_if",
            "e_if_node_leave_apply",
            "e_tool_leave_llm_leave",
            "e_llm_leave_answer_leave",
        }
        and item.get("source") not in orchestration_node_ids
        and item.get("target") not in orchestration_node_ids
    ]
    for obsolete in tuple(orchestration_node_ids) + (
        "tool_leave", "llm_leave", "answer_leave",
    ):
        nodes.pop(obsolete, None)

    start = nodes["start_node"]
    variables = start["data"].setdefault("variables", [])
    if not any(item.get("variable") == "leave_pending" for item in variables):
        variables.append({
            "type": "text-input",
            "label": "是否存在请假草稿（1/0）",
            "default": "0",
            "options": [],
            "required": False,
            "variable": "leave_pending",
        })

    context_code = """def main(query: str, leave_pending: str) -> dict:
    text = str(query or '')
    pending = str(leave_pending or '0') == '1'
    fresh = any(word in text for word in ('我要请假', '我想请假', '帮我请假', '我需要请假'))
    if fresh:
        prefix = '用户本轮明确重新发起请假，旧草稿字段不得沿用。'
    elif pending:
        prefix = '当前存在待处理的请假草稿。'
    else:
        prefix = '当前没有请假草稿。'
    return {'context_query': prefix + '\\n用户本轮输入：' + text}
"""
    add_nodes = [
        code_node(
            "code_leave_context", "补全请假上下文",
            "把草稿状态与用户本轮输入一起交给意图分类",
            context_code,
            [
                {"variable": "query", "value_selector": ["sys", "query"]},
                {"variable": "leave_pending", "value_selector": ["start_node", "leave_pending"]},
            ],
            ["context_query"], 330, 320,
        ),
        llm_node(
            "llm_leave_extract", "LLM 提取请假字段",
            """你是请假字段抽取器。只根据用户本轮原话抽取，不得猜测。
输出严格 JSON，不要 Markdown，不要解释。固定字段：
{
  "action": "collect|confirm|modify|cancel",
  "leave_type": "病假|事假|年假|婚假|丧假|调休|其他或空字符串",
  "start_date": "仅当用户明确给出 ISO 日期时填写，否则空字符串",
  "end_date": "仅当用户明确给出 ISO 日期时填写，否则空字符串",
  "date_text": "用户原话中的日期表达，例如 下周一到下周三；没有则为空",
  "reason": "用户明确说明的原因；没有则为空"
}
动作规则：明确说确认/提交/可以 -> confirm；明确取消/不请了 -> cancel；明确修改 -> modify；其他 -> collect。
“过几天”“下周”但不能确定具体日期时，不得补成日期，写入 date_text。
已经提供的字段不能丢；不确定的字段保持空字符串。""",
            650, 320, memory=False,
        ),
        code_node(
            "code_leave_extract", "解析请假字段",
            "解析 LLM JSON，禁止在解析层猜测缺失字段",
            """def main(llm_json: str) -> dict:
    import json
    import re
    text = (llm_json or '').strip()
    text = re.sub(r'^```(?:json)?\\s*', '', text, flags=re.I)
    text = re.sub(r'\\s*```$', '', text)
    try:
        data = json.loads(text)
    except Exception:
        data = {}
    def value(key):
        item = data.get(key)
        return '' if item is None else str(item)
    action = value('action').lower()
    if action not in ('collect', 'confirm', 'modify', 'cancel'):
        action = 'collect'
    return {
        'action': action,
        'leave_type': value('leave_type'),
        'start_date': value('start_date'),
        'end_date': value('end_date'),
        'date_text': value('date_text'),
        'reason': value('reason'),
    }
""",
            [
                {"variable": "llm_json", "value_selector": ["llm_leave_extract", "text"]},
            ],
            ["action", "leave_type", "start_date", "end_date", "date_text", "reason"],
            950, 320,
        ),
        tool_node(
            "tool_leave_prepare", "准备请假草稿",
            "prepare_leave", "请假准备与日期归一化",
            {
                "token": "{{#start_node.auth_token#}}",
                "query": "{{#sys.query#}}",
                "action": "{{#code_leave_extract.action#}}",
                "leave_type": "{{#code_leave_extract.leave_type#}}",
                "start_date": "{{#code_leave_extract.start_date#}}",
                "end_date": "{{#code_leave_extract.end_date#}}",
                "date_text": "{{#code_leave_extract.date_text#}}",
                "reason": "{{#code_leave_extract.reason#}}",
            },
            1250, 320,
        ),
        code_node(
            "code_leave_prepare", "解析请假准备结果",
            "解析 prepare_leave 的结构化状态和确认令牌",
            """def main(prepare_text: str) -> dict:
    import json
    text = (prepare_text or '').strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict) and isinstance(payload.get('data'), dict):
            payload = payload['data']
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    fields = payload.get('fields') or {}
    if not isinstance(fields, dict):
        fields = {}
    missing = payload.get('missing_fields') or []
    if not isinstance(missing, list):
        missing = [str(missing)]
    duplicate = payload.get('duplicate') or {}
    days = fields.get('days')
    fields_text = '\\n'.join([
        '请假类型：' + str(fields.get('leave_type') or '未提供'),
        '开始日期：' + str(fields.get('start_date') or '未提供'),
        '结束日期：' + str(fields.get('end_date') or '未提供'),
        '请假天数：' + ('未计算' if days in (None, '') else str(days)),
        '请假原因：' + str(fields.get('reason') or '未提供'),
    ])
    return {
        'status_token': str(payload.get('status_token') or 'STATE_ERROR'),
        'message': str(payload.get('message') or ''),
        'missing_text': '、'.join(str(v) for v in missing) or '无',
        'fields_text': fields_text,
        'confirmation_token': str(payload.get('confirmation_token') or ''),
        'duplicate_text': json.dumps(duplicate, ensure_ascii=False) if duplicate else '',
    }
""",
            [
                {"variable": "prepare_text", "value_selector": ["tool_leave_prepare", "text"]},
            ],
            [
                "status_token", "message", "missing_text", "fields_text",
                "confirmation_token", "duplicate_text",
            ],
            1550, 320,
        ),
    ]

    state_if = node_base("if_leave_state", "if-else", "请假状态分支", 1850, 320, height=260)
    state_if["data"].update({
        "logical_operator": "or",
        "cases": [
            {
                "case_id": "case_missing", "id": "case_missing",
                "logical_operator": "and",
                "conditions": [{
                    "id": "c_missing", "comparison_operator": "contains",
                    "value": "STATE_MISSING",
                    "varType": "string",
                    "variable_selector": ["code_leave_prepare", "status_token"],
                }],
            },
            {
                "case_id": "case_confirm", "id": "case_confirm",
                "logical_operator": "and",
                "conditions": [{
                    "id": "c_confirm", "comparison_operator": "contains",
                    "value": "STATE_CONFIRM",
                    "varType": "string",
                    "variable_selector": ["code_leave_prepare", "status_token"],
                }],
            },
            {
                "case_id": "case_ready", "id": "case_ready",
                "logical_operator": "and",
                "conditions": [{
                    "id": "c_ready", "comparison_operator": "contains",
                    "value": "STATE_READY",
                    "varType": "string",
                    "variable_selector": ["code_leave_prepare", "status_token"],
                }],
            },
            {
                "case_id": "case_terminal", "id": "case_terminal",
                "logical_operator": "or",
                "conditions": [
                    {
                        "id": "c_duplicate", "comparison_operator": "contains",
                        "value": "STATE_DUPLICATE", "varType": "string",
                        "variable_selector": ["code_leave_prepare", "status_token"],
                    },
                    {
                        "id": "c_cancel", "comparison_operator": "contains",
                        "value": "STATE_CANCELLED", "varType": "string",
                        "variable_selector": ["code_leave_prepare", "status_token"],
                    },
                    {
                        "id": "c_error", "comparison_operator": "contains",
                        "value": "STATE_ERROR", "varType": "string",
                        "variable_selector": ["code_leave_prepare", "status_token"],
                    },
                ],
            },
        ],
    })
    add_nodes.append(state_if)
    add_nodes.extend([
        llm_node(
            "llm_leave_ask", "请假追问",
            "学生请假信息不完整，请根据结构化结果自然追问。\n"
            "已识别字段：{{#code_leave_prepare.fields_text#}}\n"
            "缺失字段：{{#code_leave_prepare.missing_text#}}\n"
            "后端提示：{{#code_leave_prepare.message#}}\n"
            "只追问缺失项；已经明确的字段不要重复问。开始和结束日期都存在时，"
            "不得再问请假天数。若后端指出非法日期，要明确转告并请用户重新提供。"
            + OUTPUT_RULES,
            2150, 100,
        ),
        llm_node(
            "llm_leave_confirm", "请假复述确认",
            "请根据后端归一化结果向学生复述请假信息并等待确认。\n"
            "{{#code_leave_prepare.fields_text#}}\n"
            "必须说清楚类型、开始日期、结束日期、自动计算的天数和原因，"
            "最后提示“回复确认提交，回复修改则重新填写”。不要再调用任何提交动作。"
            + OUTPUT_RULES,
            2150, 300,
        ),
        tool_node(
            "tool_leave_confirm", "确认后提交请假",
            "confirm_leave", "确认令牌校验并写入正式请假单",
            {
                "token": "{{#start_node.auth_token#}}",
                "confirmation_token": "{{#code_leave_prepare.confirmation_token#}}",
            },
            2150, 500,
        ),
        llm_node(
            "llm_leave_result", "生成请假提交结果",
            "请假最终写入接口返回：{{#tool_leave_confirm.text#}}\n"
            "若已提交，说明单号、起止日期、天数和待审批状态；"
            "若重复，必须同时说明本次申请范围和已有单号，不得说成角色无权限。"
            + OUTPUT_RULES,
            2450, 500,
        ),
        llm_node(
            "llm_leave_terminal", "请假终止或错误说明",
            "请假准备状态：{{#code_leave_prepare.status_token#}}\n"
            "后端提示：{{#code_leave_prepare.message#}}\n"
            "已识别信息：{{#code_leave_prepare.fields_text#}}\n"
            "重复信息：{{#code_leave_prepare.duplicate_text#}}\n"
            "请如实说明取消、重复或错误原因，不得声称已经提交。"
            + OUTPUT_RULES,
            2150, 700,
        ),
        answer_node("answer_leave_ask", "回复请假追问", "{{#llm_leave_ask.text#}}", 2450, 100),
        answer_node("answer_leave_confirm", "回复请假确认", "{{#llm_leave_confirm.text#}}", 2450, 300),
        answer_node("answer_leave_result", "回复请假结果", "{{#llm_leave_result.text#}}", 2750, 500),
        answer_node("answer_leave_terminal", "回复请假终止", "{{#llm_leave_terminal.text#}}", 2450, 700),
    ])
    graph["nodes"] = list(nodes.values()) + add_nodes

    classifier = nodes["if_node"]
    classifier["data"]["query_variable_selector"] = ["code_leave_context", "context_query"]
    classes = classifier["data"].get("classes", [])
    if not any(item.get("id") == "leave_continue" for item in classes):
        insert_at = next(
            (index for index, item in enumerate(classes) if item.get("id") == "leave_apply"),
            len(classes),
        )
        classes.insert(insert_at, {
            "id": "leave_continue",
            "name": "请假草稿续办：补充字段、确认、修改或取消（仅当前上下文明确存在请假草稿时）",
        })
    classifier["data"]["instruction"] = (
        "请根据上下文和用户问题选择最匹配的业务类别。上下文明确存在请假草稿时，"
        "只有用户的补充、确认、修改或取消进入“请假草稿续办”；"
        "如果用户明确重新说“我要请假/我想请假/帮我请假/我需要请假”，"
        "必须进入“请假”重新发起，旧草稿字段不得沿用；"
        "没有请假草稿时，明确要请假选择“请假”；询问流程、条件、政策选择知识咨询。"
        "查询记录优先于新增，高风险情绪永远优先。不得机械按关键词判断。"
    )
    classifier["height"] = 140 + 42 * len(classes)

    edges.extend([
        edge("e_start_leave_context", "start_node", "code_leave_context"),
        edge("e_leave_context_if", "code_leave_context", "if_node"),
        edge("e_if_leave_apply_extract", "if_node", "llm_leave_extract", "leave_apply"),
        edge("e_if_leave_continue_extract", "if_node", "llm_leave_extract", "leave_continue"),
        edge("e_extract_code", "llm_leave_extract", "code_leave_extract"),
        edge("e_code_prepare", "code_leave_extract", "tool_leave_prepare"),
        edge("e_prepare_code", "tool_leave_prepare", "code_leave_prepare"),
        edge("e_prepare_if", "code_leave_prepare", "if_leave_state"),
        edge("e_leave_missing", "if_leave_state", "llm_leave_ask", "case_missing"),
        edge("e_leave_confirm", "if_leave_state", "llm_leave_confirm", "case_confirm"),
        edge("e_leave_ready", "if_leave_state", "tool_leave_confirm", "case_ready"),
        edge("e_leave_terminal", "if_leave_state", "llm_leave_terminal", "case_terminal"),
        edge("e_leave_false_terminal", "if_leave_state", "llm_leave_terminal", "false"),
        edge("e_leave_submit_result", "tool_leave_confirm", "llm_leave_result"),
        edge("e_leave_ask_answer", "llm_leave_ask", "answer_leave_ask"),
        edge("e_leave_confirm_answer", "llm_leave_confirm", "answer_leave_confirm"),
        edge("e_leave_result_answer", "llm_leave_result", "answer_leave_result"),
        edge("e_leave_terminal_answer", "llm_leave_terminal", "answer_leave_terminal"),
    ])
    graph["edges"] = edges
    GRAPH_PATH.write_text(
        json.dumps(graph, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    if DSL_PATH.exists():
        document = yaml.safe_load(DSL_PATH.read_text(encoding="utf-8")) or {}
        if isinstance(document, dict) and isinstance(document.get("workflow"), dict):
            document["workflow"]["graph"] = graph
            DSL_PATH.write_text(
                "# 自动生成：patch_workflows.py + patch_leave_orchestration.py\n"
                + yaml.safe_dump(
                    document, allow_unicode=True, sort_keys=False, width=120
                ),
                encoding="utf-8",
            )
    print("学生请假 Dify 编排已写入", GRAPH_PATH)


if __name__ == "__main__":
    main()
