"""生成并可选应用 Dify 运行库更新。

用法：
    python publish_runtime.py          # 只生成 live/update_runtime.sql
    python publish_runtime.py --apply  # 写入本机 Docker Dify 并重启 API/Worker
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "live")
PROVIDER_ID = "1c2de193-75e4-4c78-8366-99b5f4e1ca00"
APPS = {
    "student": "27c83665-8887-4679-bc2c-4b59c457e7d8",
    "employee": "7a62defe-ba1a-4f0c-895d-1a22493da686",
    "customer": "bb0021cf-6b78-44c2-a06d-30959d9ef4ad",
}


def dollar_quote(text):
    tag = "$yj$"
    index = 0
    while tag in text:
        index += 1
        tag = "$yj%d$" % index
    return tag + text + tag


def build_sql():
    tools_path = os.path.join(LIVE, "tools_str_new.json")
    tools_text = open(tools_path, encoding="utf-8").read()
    tools = json.loads(tools_text)
    if not tools:
        raise SystemExit("Dify 工具集为空")
    operation_ids = [tool.get("operation_id") for tool in tools]
    duplicates = sorted({
        operation_id for operation_id in operation_ids
        if operation_id and operation_ids.count(operation_id) > 1
    })
    if duplicates:
        raise SystemExit("Dify 工具 operation_id 重复：" + ", ".join(duplicates))

    lines = [
        "-- 自动生成：更新 yuejiao_api 工具集、三份工作流草稿与已发布版本",
        "BEGIN;",
        "UPDATE tool_api_providers SET tools_str = %s, updated_at = now() "
        "WHERE id = '%s';" % (dollar_quote(tools_text), PROVIDER_ID),
    ]
    for name, app_id in APPS.items():
        graph_path = os.path.join(LIVE, name + "_new.json")
        graph_text = open(graph_path, encoding="utf-8").read()
        graph = json.loads(graph_text)
        if not graph.get("nodes") or not graph.get("edges"):
            raise SystemExit(name + " 工作流为空")
        node_ids = [node.get("id") for node in graph["nodes"]]
        edge_ids = [edge.get("id") for edge in graph["edges"]]
        if len(node_ids) != len(set(node_ids)):
            raise SystemExit(name + " 工作流存在重复节点 ID")
        if len(edge_ids) != len(set(edge_ids)):
            raise SystemExit(name + " 工作流存在重复边 ID")
        unknown_ends = [
            edge.get("id") for edge in graph["edges"]
            if edge.get("source") not in set(node_ids)
            or edge.get("target") not in set(node_ids)
        ]
        if unknown_ends:
            raise SystemExit(name + " 工作流存在悬空边：" + ", ".join(unknown_ends))
        quoted = dollar_quote(graph_text)
        lines.append(
            "UPDATE workflows SET graph = %s::jsonb, updated_at = now() "
            "WHERE app_id = '%s' AND version = 'draft';" % (quoted, app_id)
        )
        lines.append(
            "UPDATE workflows w SET graph = %s::jsonb, updated_at = now() "
            "FROM apps a WHERE w.id = a.workflow_id AND a.id = '%s';"
            % (quoted, app_id)
        )
        lines.append(
            "UPDATE apps SET updated_at = now(), status = 'normal' WHERE id = '%s';"
            % app_id
        )
    lines.append("COMMIT;")
    return "\n".join(lines) + "\n"


def main():
    sql = build_sql()
    path = os.path.join(LIVE, "update_runtime.sql")
    with open(path, "w", encoding="utf-8") as f:
        f.write(sql)
    print("已生成", path, "(%.1f KB)" % (len(sql.encode("utf-8")) / 1024))
    if "--apply" not in sys.argv:
        return
    result = subprocess.run(
        ["docker", "exec", "-i", "docker-db-1", "psql", "-v", "ON_ERROR_STOP=1",
         "-U", "postgres", "-d", "dify"],
        input=sql.encode("utf-8"),
        check=False,
    )
    if result.returncode:
        raise SystemExit("Dify SQL 更新失败")
    subprocess.run(
        ["docker", "restart", "docker-api-1", "docker-worker-1"],
        check=True,
    )
    print("Dify 运行库已更新，API/Worker 已重启")


if __name__ == "__main__":
    main()
