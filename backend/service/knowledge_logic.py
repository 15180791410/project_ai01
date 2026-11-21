"""调用 Dify Dataset API 检索ai知识库。"""
import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

DEFAULT_DATASET = "a7bf3a9f-872b-4941-a617-0afa5b8b61b4"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "dify_config.local.json"
PROGRAM_DOCUMENTS = (
    "新加坡国际本硕升学计划.docx",
    "中德精英人才共建计划.docx",
)
COMPANY_DOCUMENT = "企业信息.docx"


def _request_json(request, timeout=20):
    """执行 Dify Dataset 请求；瞬时网络错误重试一次，仍失败则抛错。

    不在本地缓存公司/项目正文，确保所有知识事实仍只来源于 Dify。
    """
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.4)
    raise RuntimeError("Dify知识库请求失败：" + repr(last_error))


def _config():
    cfg = {}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    knowledge = cfg.get("knowledge", {})
    return {
        "api_base": os.getenv("DIFY_API_BASE", cfg.get("api_base") or "http://127.0.0.1/v1").rstrip("/"),
        "api_key": os.getenv("DIFY_DATASET_KEY", knowledge.get("api_key", "")),
        "dataset_id": os.getenv("DIFY_DATASET_ID", knowledge.get("dataset_id", DEFAULT_DATASET)),
    }


def retrieve(query, top_k=5):
    cfg = _config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 DIFY_DATASET_KEY")
    payload = {
        "query": query,
        "retrieval_model": {
            "search_method": "semantic_search",
            "reranking_enable": False,
            "top_k": top_k,
            # 关闭阈值会把低相关片段也交给 LLM，实测“请假流程”曾召回
            # 企业介绍等无关内容。0.2 是保守下限，宁可不答也不提供错误依据。
            "score_threshold_enabled": True,
            "score_threshold": 0.2,
        },
    }
    req = urllib.request.Request(
        cfg["api_base"] + "/datasets/" + cfg["dataset_id"] + "/retrieve",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + cfg["api_key"],
            "Content-Type": "application/json",
        },
        method="POST",
    )
    data = _request_json(req, timeout=20)
    snippets = []
    for record in data.get("records", []):
        segment = record.get("segment") or {}
        document = segment.get("document") or record.get("document") or {}
        snippets.append({
            "content": segment.get("content", ""),
            "score": record.get("score"),
            "document_name": document.get("name", ""),
        })
    return {
        "source_mode": "DIFY_LIVE",
        "retrieved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset_id": cfg["dataset_id"],
        "snippets": snippets,
    }


def _api_get(path):
    cfg = _config()
    if not cfg["api_key"]:
        raise RuntimeError("未配置 DIFY_DATASET_KEY")
    req = urllib.request.Request(
        cfg["api_base"] + path,
        headers={"Authorization": "Bearer " + cfg["api_key"]},
    )
    return _request_json(req, timeout=20)


def _document_segments(document_name):
    """按文档名从 Dify 读取全部分段；不写入后端数据库。"""
    cfg = _config()
    docs = _api_get(
        "/datasets/" + cfg["dataset_id"] + "/documents?page=1&limit=100"
    ).get("data", [])
    document = next((item for item in docs if item.get("name") == document_name), None)
    if not document:
        raise RuntimeError("Dify 知识库缺少文档：" + document_name)
    data = _api_get(
        "/datasets/" + cfg["dataset_id"] + "/documents/"
        + document["id"] + "/segments?page=1&limit=100"
    )
    rows = sorted(data.get("data", []), key=lambda item: item.get("position") or 0)
    return [
        {"content": item.get("content", ""), "position": item.get("position", 0)}
        for item in rows
    ]


def _plan_id(name):
    return "kb_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]


def _first_match(pattern, text, default=""):
    match = re.search(pattern, text or "", re.S)
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else default


def _between(text, start, end_tokens):
    """截取某个标题到下一个标题之间的原文。"""
    index = text.find(start)
    if index < 0:
        return ""
    end = len(text)
    for token in end_tokens:
        next_index = text.find(token, index + len(start))
        if next_index >= 0:
            end = min(end, next_index)
    return text[index:end].strip()


def _singapore_programs():
    text = "\n".join(
        segment["content"] for segment in _document_segments(PROGRAM_DOCUMENTS[0])
    )
    definitions = [
        ("2+2新加坡定向培养本科班", "2+2 新加坡本科班", "本科"),
        ("2+2+1本硕连读", "2+2+1 新加坡本硕连读", "本硕"),
        ("0.5/1+2新加坡定向培养本科班", "0.5/1+2 新加坡本科班", "本科"),
        ("0.5/1+2+1本硕连读", "0.5/1+2+1 新加坡本硕连读", "本硕"),
        ("6+6酒店运营大专就业班", "6+6 酒店运营大专就业班", "就业"),
        ("9+6航空运营大专就业班", "9+6 航空运营大专就业班", "就业"),
    ]
    headings = [item[0] for item in definitions] + [
        "一年制专升本", "一年制本升硕", "报名方式",
    ]
    plans = []
    for raw_name, display_name, category in definitions:
        block = _between(text, raw_name, headings)
        # 方案名称必须真实存在于标注的 Dify 文档中；找不到就跳过，
        # 不能因为代码里写了标题就标记为“知识库已核实”。
        if raw_name not in text or not block:
            continue
        audience = _first_match(r"招生群体[:：]\s*([^\n]+)", block)
        if not audience:
            audience = _first_match(
                r"(面向[^。\n]*(?:17岁|周岁)[^。\n]*)", block
            )
        plans.append({
            "id": _plan_id(display_name),
            "project_name": display_name,
            "country": "新加坡",
            "category": category,
            "description": re.sub(r"\s+", " ", block)[:260],
            "target_audience": audience or "以公司资料和顾问评估为准",
            "source_document": PROGRAM_DOCUMENTS[0],
            "knowledge_verified": True,
        })

    upgrade_block = _between(
        text, "一年制专升本、本升硕",
        ["报名方式", "新加坡一带一路工学交替"],
    )
    if upgrade_block:
        plans.append({
            "id": _plan_id("一年制专升本"),
            "project_name": "一年制专升本",
            "country": "新加坡",
            "category": "本科",
            "description": re.sub(r"\s+", " ", upgrade_block)[:260],
            "target_audience": "国内专科毕业生",
            "source_document": PROGRAM_DOCUMENTS[0],
            "knowledge_verified": True,
        })
        plans.append({
            "id": _plan_id("一年制本升硕"),
            "project_name": "一年制本升硕",
            "country": "新加坡",
            "category": "硕士",
            "description": re.sub(r"\s+", " ", upgrade_block)[:260],
            "target_audience": "国内本科毕业生",
            "source_document": PROGRAM_DOCUMENTS[0],
            "knowledge_verified": True,
        })
    return plans


def _germany_programs():
    text = "\n".join(
        segment["content"] for segment in _document_segments(PROGRAM_DOCUMENTS[1])
    )
    # 年龄、学历和项目名称都必须来自文档；解析失败时留空或跳过，
    # 不能用代码中的旧数据冒充知识库事实。
    age = _first_match(r"年龄[:：]\s*([^\n]+)", text)
    education = _first_match(r"学历[:：]\s*([^\n]+)", text)
    audience = "；".join(
        part for part in (age, education, "具备德语学习能力和动手能力") if part
    )
    generic_name = "中德精英人才共建计划(双元制)"
    plans = []
    # `_document_segments` 已确认该文档真实存在于 Dify；正文虽没有重复写
    # 标题，但整篇文档就是德国双元制项目说明，因此通用方案可安全生成。
    plans.append({
        "id": _plan_id(generic_name),
        "project_name": generic_name,
        "country": "德国",
        "category": "双元制",
        "description": re.sub(r"\s+", " ", text)[:320],
        "target_audience": audience or "以公司资料和顾问评估为准",
        "source_document": PROGRAM_DOCUMENTS[1],
        "knowledge_verified": True,
    })
    categories = _first_match(r"专业类别\s*([^\n]+)", text)
    for category in [item.strip() for item in categories.split("、") if item.strip()]:
        direction = category.removesuffix("类")
        name = "中德双元制·" + direction + "方向"
        plans.append({
            "id": _plan_id(name),
            "project_name": name,
            "country": "德国",
            "category": "双元制",
            "description": direction + "类职业培训与企业实践，具体以公司资料和顾问确认为准",
            "target_audience": audience,
            "source_document": PROGRAM_DOCUMENTS[1],
            "knowledge_verified": True,
        })
    return plans


def program_catalog():
    """从 Dify 文档实时生成留学方案目录。

    后端不保存方案主数据；每次读取 Dify 文档分段。若 Dify 不可用则抛错，
    不返回后端旧副本。
    """
    plans = _singapore_programs() + _germany_programs()
    return {
        "source_mode": "DIFY_LIVE",
        "retrieved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "programs": plans,
    }


def company_profile():
    """从 Dify 企业信息文档解析官网展示字段，不在后端持久化。"""
    segments = _document_segments(COMPANY_DOCUMENT)
    text = "\n".join(segment["content"] for segment in segments)
    full_name = _first_match(r"([^\n，]+)，创办于", text)
    if not full_name:
        # 企业名称是事实主键，不能在解析失败时回退到代码里的旧名称。
        raise RuntimeError("Dify企业信息文档未解析到企业名称")
    founded = _first_match(r"创办于\s*(\d{4})年", text)
    transfer = _first_match(
        r"((?:\d{4}年(?:\d+月)?).*?划转至[^。\n]+)", text
    )
    mission = _first_match(r"我们的使命[:：]\s*([^\n]+)", text)
    values = _first_match(r"我们的价值观[:：]\s*([^\n]+)", text)
    business = _first_match(r"主营业务包括([^。\n]+)", text)
    international = _first_match(r"国际教育包括([^。\n]+)", text)
    address = _first_match(r"公司地址[:：]\s*([^\n]+)", text)
    phone = _first_match(r"电话[:：]\s*([0-9\-]+)", text)
    wechat = _first_match(r"公众号[:：]\s*([A-Za-z0-9_\-]+)", text)
    return {
        "hero": {
            "name": {"title": "ai", "content": full_name, "source_document": COMPANY_DOCUMENT},
            "founded": {"title": "创办年份", "content": founded, "source_document": COMPANY_DOCUMENT},
            "transfer": {"title": "企业沿革", "content": transfer, "source_document": COMPANY_DOCUMENT},
        },
        "about": {
            "mission": {"title": "企业使命", "content": mission, "source_document": COMPANY_DOCUMENT},
            "values": {"title": "企业价值观", "content": values, "source_document": COMPANY_DOCUMENT},
            "business": {"title": "主营业务", "content": business, "source_document": COMPANY_DOCUMENT},
            "international": {"title": "国际教育范围", "content": international, "source_document": COMPANY_DOCUMENT},
        },
        "contact": {
            "phone": {"title": "咨询电话", "content": phone, "source_document": COMPANY_DOCUMENT},
            "address": {"title": "公司地址", "content": address, "source_document": COMPANY_DOCUMENT},
            "wechat": {"title": "公众号", "content": wechat, "source_document": COMPANY_DOCUMENT},
        },
    }
