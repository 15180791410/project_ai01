"""创建 Dify 知识库只读 Dataset API Key 并写入本地忽略配置。"""
import json
import secrets
import subprocess
import uuid
from pathlib import Path

TENANT_ID = "00d8c633-daa6-46bf-8b94-e22161d58f42"
DATASET_ID = "a7bf3a9f-872b-4941-a617-0afa5b8b61b4"
CONFIG = Path(__file__).resolve().parent / "dify_config.local.json"


def run_sql(sql):
    subprocess.run(
        ["docker", "exec", "docker-db-1", "psql", "-U", "postgres",
         "-d", "dify", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def main():
    # 清理早期脚本失败产生的无效占位 token。
    run_sql("DELETE FROM api_tokens WHERE type='dataset' AND token='dataset-'")
    token = "dataset-" + secrets.token_urlsafe(18).replace("-", "A").replace("_", "B")[:24]
    token_id = str(uuid.uuid4())
    run_sql(
        "INSERT INTO api_tokens (id,type,token,created_at,tenant_id) "
        "VALUES ('%s','dataset','%s',now(),'%s')" %
        (token_id, token, TENANT_ID)
    )
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["knowledge"] = {"api_key": token, "dataset_id": DATASET_ID}
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Dataset API Key 已创建并保存到本地忽略配置")


if __name__ == "__main__":
    main()
