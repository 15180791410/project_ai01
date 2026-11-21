"""共享项目推荐接口，供官网客服和内部员工使用。"""
from fastapi import APIRouter
from pydantic import BaseModel

from service import knowledge_logic, rule_logic
from utils import format_data

router = APIRouter()


class RecommendData(BaseModel):
    raw_text: str = ""
    # Dify 工具参数以字符串传递；允许字符串后再清洗，避免空值触发 422。
    age: int | str | None = None
    education: str = ""
    intent: str = ""
    language: str = ""
    hands_on: bool | str | None = None
    economic_condition: str = ""


@router.post("/recommend", summary="画像项目推荐（共享确定性工具）")
def recommend(data: RecommendData):
    try:
        age_value = data.age
        if isinstance(age_value, str) and not age_value.strip():
            age_value = None
        hands_value = data.hands_on
        if isinstance(hands_value, str):
            normalized = hands_value.strip().lower()
            hands_value = (
                None if not normalized
                else True if normalized in ("true", "1", "yes", "是")
                else False if normalized in ("false", "0", "no", "否")
                else hands_value
            )
        result = rule_logic.recommend_profile(
            raw_text=data.raw_text,
            age=age_value,
            education=data.education,
            intent=data.intent,
            language=data.language,
            hands_on=hands_value,
            economic_condition=data.economic_condition,
        )
    except Exception as exc:
        print("Dify 方案目录读取失败:", repr(exc))
        return format_data.unavailable("公司业务资料暂时不可用，暂时无法完成画像匹配，请稍后重试或联系顾问")
    matches = [
        item for item in result.get("matches", [])
        if item.get("knowledge_plan")
    ]
    for item in matches:
        plan = item.pop("knowledge_plan")
        item["source_document"] = plan["source_document"]
        item["description"] = plan.get("description", "")
        item["audience"] = plan.get("target_audience", item.get("audience", ""))
    result["matches"] = matches
    result["best_match"] = matches[0] if matches else None
    if matches:
        best = matches[0]
        profile = result.get("profile", {})
        result["summary"] = (
            f"{profile.get('education', '')}学历、年龄 {profile.get('age', '')}，"
            f"推荐【{best['project']}】，匹配度 {best['score']} 分。"
        )
    elif result.get("status") == "match":
        result["status"] = "no_match"
        result["msg"] = "公司现有项目暂时没有与该画像完全匹配的方向"
        result["summary"] = "建议补充真实目标或由顾问人工评估，不会硬套不合适的项目。"
    elif result.get("status") == "no_match":
        result["msg"] = "公司现有项目暂时没有与该画像完全匹配的方向"
        result["summary"] = "建议补充真实目标或由顾问人工评估，不会硬套不合适的项目。"

    # 推荐项目由确定性规则计算，项目解释必须绑定知识库原文。
    # 这里把 Dify 实时检索片段一并返回，Dify 的回复节点只能依据这些
    # 片段补充学制、费用、适合人群等公司事实，避免模型自行添加国家或项目。
    best = result.get("best_match") or {}
    if best:
        try:
            live = knowledge_logic.retrieve(
                best.get("project", "") + " 学制 适合人群 费用 招生要求",
                top_k=4,
            )
            result["knowledge"] = {
                "source_mode": live.get("source_mode", "DIFY_LIVE"),
                "retrieved_at": live.get("retrieved_at", ""),
                "snippets": [
                    {
                        "document_name": item.get("document_name", ""),
                        "content": str(item.get("content", ""))[:1200],
                        "score": item.get("score"),
                    }
                    for item in live.get("snippets", [])
                ],
            }
        except Exception as exc:
            # Dify 暂时不可用时，仍返回确定性规则匹配结果和已登记来源文档。
            # 不伪造学费或政策，明确提示需要顾问确认。
            print("推荐公司资料检索失败:", repr(exc))
            result["knowledge"] = {
                "source_mode": "UNAVAILABLE",
                "source_document": best.get("source_document", ""),
                "snippets": [],
                "warning": "公司业务资料暂时不可用，学费和政策须由顾问确认",
            }
    return format_data.ok(
        result,
        result.get("msg") or "匹配完成",
    )
