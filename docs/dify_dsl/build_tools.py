# -*- coding: utf-8 -*-
"""
从后端 FastAPI 的 OpenAPI 文档生成：
  1) yuejiao_api_tools.yaml   —— 供 Dify「从 OpenAPI 导入自定义工具」使用
  2) live/tools_str_new.json  —— Dify tool_api_providers.tools_str 的等价结构（用于直接同步到数据库）

用法（在 docs/dify_dsl 目录下）：
    python ../../backend/_dump_openapi.py     # 或先执行 dump 部分
    python build_tools.py

设计要点：
- operationId 与已有工具保持一致，新增工具使用新名字，不改旧名，保证旧工作流节点不失效。
- 只导出业务接口，不含 /health、/docs。
"""
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OPENAPI_JSON = os.path.join(HERE, "_openapi_source.json")
SERVER_URL = "http://host.docker.internal:8000"

# ---------------------------------------------------------------------------
# 路径 + 方法 -> 工具名（operationId）
# 顺序即生成顺序，分组仅影响可读性
# ---------------------------------------------------------------------------
OPERATIONS = [
    # ---------------- 学生 ----------------
    ("post", "/api/actions/prepare", "prepare_business_action"),
    ("post", "/api/actions/execute", "execute_business_action"),
    ("post", "/api/student/leave/prepare", "prepare_leave"),
    ("post", "/api/student/leave/confirm", "confirm_leave"),
    ("post", "/api/student/leave", "leave"),
    ("post", "/api/student/emotion", "emotion"),
    ("post", "/api/student/feedback", "feedback"),
    ("get", "/api/student/my_scores", "my_scores"),
    ("get", "/api/student/my_leaves", "my_leaves"),
    ("get", "/api/student/my_feedbacks", "my_feedbacks"),
    ("get", "/api/student/psych-profile", "my_psych_profile"),
    # 学籍阶段：学生问"我能请假吗/什么时候能请假"时查自己的阶段与已解锁服务
    ("get", "/api/student/stage", "my_stage"),
    # ---------------- 员工：客户 / 日报 / 成绩 ----------------
    ("get", "/api/employee/search", "search_customer"),
    ("post", "/api/employee/add_lead", "add_lead"),
    ("post", "/api/employee/report", "submit_report"),
    ("post", "/api/employee/add_score", "add_score"),
    ("put", "/api/employee/update_lead_status", "update_lead_status"),
    ("get", "/api/employee/search_advanced", "search_advanced"),
    ("put", "/api/employee/resolve_feedback", "resolve_feedback"),
    ("get", "/api/employee/reports", "get_reports"),
    ("get", "/api/employee/search_scores", "search_scores"),
    ("post", "/api/employee/nl2sql", "nl2sql"),
    # ---------------- 客服 ----------------
    ("get", "/api/customer/events", "list_events"),
    ("get", "/api/customer/courses", "list_courses"),
    ("get", "/api/customer/registrations", "get_registrations"),
    # ---------------- 登录 / 用户中心 ----------------
    ("get", "/api/auth/profile", "profile"),
    ("post", "/api/auth/change-password", "change_password"),
    ("get", "/api/auth/users", "list_users"),
    # ---------------- 请假审批（员工） ----------------
    ("get", "/api/teacher/pending-leaves", "pending_leaves"),
    ("get", "/api/teacher/all-leaves", "all_leaves"),
    ("post", "/api/teacher/approve", "approve_leave_with_comment"),
    # ---------------- 内容管理：活动 ----------------
    ("get", "/api/content/events", "content_events"),
    ("post", "/api/content/events/save", "save_event"),
    ("post", "/api/content/events/delete", "delete_event"),
    ("post", "/api/content/events/register", "register_event"),
    ("get", "/api/content/events/registrations", "event_registrations"),
    ("get", "/api/content/my-registrations", "my_registrations"),
    # ---------------- 内容管理：留学方案 ----------------
    ("get", "/api/content/programs", "list_programs"),
    # ---------------- 内容管理：学生分布 / 预警 ----------------
    ("get", "/api/content/students-by-country", "students_by_country"),
    ("post", "/api/content/student/country", "set_student_country"),
    ("post", "/api/content/alert/handle", "handle_alert"),
    # ---------------- 客户线索（官网留资 / 员工跟进） ----------------
    ("post", "/api/lead/submit", "submit_lead"),
    ("get", "/api/lead/list", "list_leads"),
    ("post", "/api/lead/follow-up", "follow_up_lead"),
    # ---------------- 申请进度 ----------------
    ("get", "/api/application/my", "my_applications"),
    ("get", "/api/application/all", "all_applications"),
    ("post", "/api/application/create", "create_application"),
    ("post", "/api/application/advance", "advance_application"),
    # ---------------- 通知中心 ----------------
    ("get", "/api/notification/list", "list_notifications"),
    ("post", "/api/notification/read", "mark_notification_read"),
    ("get", "/api/notification/unread", "unread_notifications"),
    # ---------------- 管理后台 ----------------
    ("get", "/api/admin/alerts", "list_alerts"),
    ("put", "/api/admin/handle_alert", "handle_alert_admin"),
    # ---------------- 共享画像推荐 ----------------
    ("post", "/api/recommend/recommend", "recommend_project"),
]

TYPE_MAP = {"integer": "number", "number": "number", "boolean": "boolean", "string": "string"}

# 参数语义说明（LLM 靠这段理解该填什么，缺省时会退化成字段名，务必补充）
PARAM_DESC = {
    "token": "登录令牌。由「开始」节点的 auth_token 变量传入，形如 {{#start_node.auth_token#}}",
    "authorization": "登录令牌头。填 Bearer {{#start_node.auth_token#}}",
    "student_id": "学生ID（整数）",
    "employee_id": "员工ID（整数）",
    "customer_id": "客户ID（整数）",
    "leave_id": "请假单ID（整数）。可先调用 pending_leaves 查看待审批列表再取ID",
    "application_id": "申请ID（整数）。可先调用 my_applications / all_applications 取ID",
    "event_id": "活动ID（整数）。可先调用 content_events 或 list_events 查询活动再取ID",
    "lead_id": "客户线索ID（整数）",
    "alert_id": "心理预警ID（整数）",
    "notify_id": "通知ID（整数），传 0 表示把全部通知标记为已读",
    "approved": "是否通过：true 表示同意，false 表示驳回",
    "comment": "审批意见。同意时是给学生的留言（选填）；驳回时是驳回理由（必填）",
    "reason": "请假原因原文，例如「明天要去医院复查，请假两天」",
    "query": "用户本轮原话。用于后端日期解析辅助，不能由模型改写",
    "action": "用户本轮动作：collect 提供信息 / confirm 确认提交 / modify 修改 / cancel 取消",
    "leave_type": "Dify LLM 提取的请假类型，例如 病假 / 事假；未提供时为空字符串",
    "start_date": "Dify LLM 提取或识别的开始日期；不明确时原样放入 date_text",
    "end_date": "Dify LLM 提取或识别的结束日期；不明确时由后端日期解析辅助归一化",
    "date_text": "用户原文中的日期表达，例如「下周一到下周三」，交给后端确定性解析",
    "confirmation_token": "prepare_leave 返回的一次性确认令牌；只能传回 confirm_leave",
    "action_type": "业务动作类型：event_register / score_query / score_upsert / score_delete / feedback_create / feedback_cancel / lead_create",
    "user_action": "用户本轮动作：collect / confirm / modify / cancel",
    "history": "最近几轮用户消息，仅用于补全已经出现过的学生或名称，不得覆盖本轮明确信息",
    "slots_json": "Dify LLM 提取的业务字段 JSON 字符串，不得补写用户没有提供的信息",
    "visitor_key": "公开访客标识；登录用户可留空",
    "text": "学生的情绪倾诉原文",
    "content": "反馈或投诉的正文内容",
    "role": "要查询的角色：STUDENT（学生）或 EMPLOYEE（员工）",
    "country": "国家名称，只能填写知识库已确认的项目国家，例如 德国 / 新加坡",
    "target_country": "意向国家，例如 德国 / 新加坡；不得自行扩展知识库未上线国家",
    "customer_name": "客户姓名",
    "contact_info": "联系方式（手机号或邮箱）",
    "background_info": "背景简述，例如「本科在读，想去G5，均分85」",
    "university": "目标院校名称",
    "program": "申请专业方向",
    "stage": "要推进到的阶段；留空表示自动进入下一阶段",
    "note": "本次操作备注，会记入历史时间轴",
    "status": "线索阶段：新增意向 / 跟进中 / 已签约 / 已流失",
    "new_status": "新的客户状态：新增意向 / 跟进中 / 已签约 / 已流失",
    "username": "登录账号，例如 student / employee",
    "password": "登录密码，不得记录到聊天或日志",
    "old_password": "当前密码",
    "new_password": "新密码，至少 6 位",
    "name": "学生姓名或客户姓名关键词",
    "command": "自然语言统计指令，例如「统计一下各国家的学生人数」",
    "ticket_id": "售后工单ID（整数）",
    "solution": "处理方案说明",
    "score": "分数（数字）",
    "course_name": "课程名称",
    "semester": "学期，例如 2026春季",
    "event_name": "活动名称",
    "start_time": "活动开始时间，例如 2026-12-20",
    "location": "活动地点",
    "max_participants": "最多参与人数（整数）",
    "project_name": "留学项目名称",
    "category": "项目分类，例如 本科 / 硕士 / 预科",
    "description": "详细描述文本",
    "target_audience": "适合人群",
    "keyword": "搜索关键词，例如 新加坡",
    "owner_id": "归属员工ID（整数）",
    "owner_employee_id": "归属员工ID（整数）",
    "is_approved": "是否通过：true 通过 / false 驳回",
    "limit": "最多返回条数（整数），默认 20",
}

# 个别工具的整体描述补充（帮助 LLM 判断何时调用）
TOOL_DESC = {
    "prepare_business_action": "通用业务准备工具：补字段、列候选、消歧、生成确认预览，不写正式业务表。",
    "execute_business_action": "通用业务执行工具：只接受 prepare_business_action 返回的一次性确认令牌，执行前再次校验权限和数据范围。",
    "prepare_leave": "请假准备工具。接收 Dify 已抽取字段，归一化日期、保存草稿、检查缺失和重叠；绝不创建正式请假单。",
    "confirm_leave": "请假最终提交工具。只接受 prepare_leave 返回的 confirmation_token；再次执行 Pydantic、权限、日期重叠和事务写入校验。",
    "register_event": "学生或访客报名活动。已登录学生只需 event_id + token；访客需填 customer_name 与 contact_info。名额满或重复报名会返回失败。",
    "my_registrations": "查询当前登录学生已报名的活动列表，需要 token。",
    "my_applications": "查询当前登录学生的留学申请进度与时间轴，需要 token。回答「我的申请到哪一步了」这类问题时调用。",
    "pending_leaves": "查询待审批的学生请假列表，需要员工 token。",
    "approve_leave_with_comment": "审批学生请假。approved=true 时可用 comment 给学生留言；approved=false 时 comment 必填作为驳回理由。",
    "students_by_country": "按意向国家分组统计学生人数，需要员工 token。",
    "event_registrations": "查询某场活动的报名名单，需要员工 token；event_id 可选，不传则返回全部。",
    "submit_lead": "官网咨询留资：写入客户线索并自动分配给员工，免登录。",
    "follow_up_lead": "员工跟进线索：更新阶段并追加跟进记录，需要员工 token。",
    "profile": "查询当前登录用户的个人资料与数据概览（待审批数、学生数等），需要 token。",
    "list_notifications": "查询当前登录用户的站内通知列表与未读数，需要 token。",
    "list_alerts": "查询心理预警列表（含学生姓名、触发原因、风险等级、处理状态）。",
    "handle_alert": "把一个心理预警标记为已解决并填写处理备注，需要员工 token。",
    "recommend_project": "根据年龄、学历、意向、语言和动手能力进行确定性项目推荐，返回逐项目匹配分与理由。",
}


def resolve_ref(openapi, schema):
    """把 $ref 展开成真实 schema（只处理 components/schemas）。"""
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if ref and ref.startswith("#/components/schemas/"):
        name = ref.split("/")[-1]
        return openapi.get("components", {}).get("schemas", {}).get(name, {})
    return schema


def collect_params(openapi, op):
    """返回 [(name, type, required, description)]，body 属性与 query 参数统一处理。"""
    out = []
    body = op.get("requestBody")
    if body:
        schema = body.get("content", {}).get("application/json", {}).get("schema", {})
        schema = resolve_ref(openapi, schema)
        required = set(schema.get("required") or [])
        for name, prop in (schema.get("properties") or {}).items():
            prop = resolve_ref(openapi, prop)
            out.append((
                name,
                TYPE_MAP.get(prop.get("type"), "string"),
                name in required,
                PARAM_DESC.get(name) or (prop.get("description") or prop.get("title") or name).strip(),
            ))
    for prm in op.get("parameters") or []:
        sch = resolve_ref(openapi, prm.get("schema") or {})
        out.append((
            prm.get("name"),
            TYPE_MAP.get(sch.get("type"), "string"),
            bool(prm.get("required")),
            PARAM_DESC.get(prm.get("name")) or (prm.get("description") or prm.get("name")).strip(),
        ))
    return out


def build(openapi):
    paths = {}
    tools = []
    for method, path, op_id in OPERATIONS:
        src = openapi.get("paths", {}).get(path, {}).get(method)
        if not src:
            print("  [跳过] OpenAPI 中不存在：%s %s" % (method.upper(), path))
            continue
        summary = src.get("summary") or op_id
        description = TOOL_DESC.get(op_id) or (src.get("description") or summary).strip()

        op = {"operationId": op_id, "summary": summary, "description": description}

        # 请求体
        body = src.get("requestBody")
        if body:
            schema = resolve_ref(openapi, body["content"]["application/json"]["schema"])
            props = {}
            for name, prop in (schema.get("properties") or {}).items():
                prop = resolve_ref(openapi, prop)
                item = {"type": prop.get("type", "string")}
                desc = PARAM_DESC.get(name) or prop.get("description")
                if desc:
                    item["description"] = desc
                if prop.get("default") not in (None, ""):
                    item["example"] = prop["default"]
                props[name] = item
            op["requestBody"] = {
                "required": bool(body.get("required", True)),
                "content": {"application/json": {"schema": {
                    "type": "object",
                    "required": schema.get("required") or [],
                    "properties": props,
                }}},
            }
            if not schema.get("required"):
                op["requestBody"]["content"]["application/json"]["schema"].pop("required")

        # 查询参数
        qs = []
        for prm in src.get("parameters") or []:
            sch = resolve_ref(openapi, prm.get("schema") or {})
            item = {
                "name": prm["name"],
                "in": prm.get("in", "query"),
                "required": bool(prm.get("required")),
                "description": (prm.get("description") or prm["name"]),
                "schema": {"type": sch.get("type", "string")},
            }
            if sch.get("default") not in (None, ""):
                item["schema"]["example"] = sch["default"]
            qs.append(item)
        if qs:
            op["parameters"] = qs

        op["responses"] = {"200": {"description": "处理结果"},
                           "422": {"description": "参数校验失败"}}
        paths.setdefault(path, {})[method] = op

        # ---- Dify tools_str 单条 ----
        params = []
        for name, typ, required, desc in collect_params(openapi, src):
            params.append({
                "name": name,
                "label": {k: name for k in ("en_US", "zh_Hans", "pt_BR", "ja_JP")},
                "placeholder": {k: desc for k in ("en_US", "zh_Hans", "pt_BR", "ja_JP")},
                "scope": None,
                "auto_generate": None,
                "template": None,
                "required": required,
                "default": None,
                "min": None,
                "max": None,
                "precision": None,
                "options": [],
                "type": typ,
                "human_description": {k: desc for k in ("en_US", "zh_Hans", "pt_BR", "ja_JP")},
                "form": "llm",
                "llm_description": desc,
                "input_schema": None,
            })
        tools.append({
            "server_url": SERVER_URL + path,
            "method": method,
            "summary": description,
            "operation_id": op_id,
            "parameters": params,
            "author": "",
            "icon": None,
            "openapi": op,
        })

    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "yuejiao_api",
            "version": "2.0.0",
            "description": (
                "ai智能助手后端接口集合（v2，覆盖学生/员工/客服/内容/线索/申请/通知共 %d 个工具）。"
                "导入 Dify「工具 → 自定义工具 → 从 OpenAPI/Swagger 导入」后即可在工作流工具节点中调用；"
                "创建工具时名称请填写 yuejiao_api。token 参数由工作流的 auth_token 变量传入。" % len(tools)
            ),
        },
        "servers": [{"url": SERVER_URL, "description": "本地后端（Dify 容器通过 host.docker.internal 访问）"}],
        "paths": paths,
    }
    return spec, tools


def main():
    if not os.path.exists(OPENAPI_JSON):
        print("缺少 %s，请先执行：cd backend && python -c \"import main,json;json.dump(main.app.openapi(),open('../docs/dify_dsl/_openapi_source.json','w',encoding='utf-8'),ensure_ascii=False)\"" % OPENAPI_JSON)
        sys.exit(1)
    openapi = json.load(open(OPENAPI_JSON, encoding="utf-8"))
    spec, tools = build(openapi)

    yaml_path = os.path.join(HERE, "yuejiao_api_tools.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("# 自动生成：不要手改，改 build_tools.py 后重新生成\n")
        yaml.safe_dump(spec, f, allow_unicode=True, sort_keys=False, width=120)

    os.makedirs(os.path.join(HERE, "live"), exist_ok=True)
    tools_path = os.path.join(HERE, "live", "tools_str_new.json")
    with open(tools_path, "w", encoding="utf-8") as f:
        json.dump(tools, f, ensure_ascii=False)

    print("生成完成：")
    print("  YAML :", yaml_path, "->", len(paths_count(spec)), "个路径")
    print("  工具 :", tools_path, "->", len(tools), "个工具")
    print("  工具名：", ", ".join(t["operation_id"] for t in tools))


def paths_count(spec):
    return spec["paths"]


if __name__ == "__main__":
    main()
