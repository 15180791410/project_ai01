"""为演示学生补充心情历史数据。

目标：
1. 让“心情记录”页面能展示连续趋势，而不是空白；
2. 同时覆盖积极、普通、关注三种状态；
3. 幂等执行，不重复插入，不删除其他学生或真实业务数据。

本脚本只生成运营演示数据，不写入公司介绍、费用或留学方案等知识库内容。
"""
from __future__ import annotations

import json
from datetime import datetime

from db import get_db


DEMO_ENTRIES = [
    {
        "time": "2026-11-14 09:10:00",
        "tag": "关注",
        "score": 55,
        "text": "最近有点焦虑，晚上也睡不好",
        "response": "已记录你的情绪；如需要，可以联系老师进一步沟通",
    },
    {
        "time": "2026-11-15 16:25:00",
        "tag": "正常",
        "score": 15,
        "text": "和同学一起去图书馆，感觉不错",
        "response": "已记录",
    },
    {
        "time": "2026-11-16 20:05:00",
        "tag": "正常",
        "score": 20,
        "text": "课程内容有点难，但我已经整理了问题",
        "response": "已记录",
    },
    {
        "time": "2026-11-17 21:35:00",
        "tag": "关注",
        "score": 55,
        "text": "最近压力大，担心跟不上课程",
        "response": "已记录你的情绪；如需要，可以联系老师进一步沟通",
    },
    {
        "time": "2026-11-18 15:40:00",
        "tag": "正常",
        "score": 15,
        "text": "和老师沟通后轻松很多",
        "response": "已记录",
    },
    {
        "time": "2026-11-19 18:20:00",
        "tag": "正常",
        "score": 15,
        "text": "第一次独立完成小组汇报，很有成就感",
        "response": "已记录",
    },
    {
        "time": "2026-11-20 09:20:00",
        "tag": "正常",
        "score": 15,
        "text": "今天状态稳定，准备继续完成申请材料",
        "response": "已记录",
    },
]


def seed_student(username="student01"):
    """给指定学生追加固定演示记录，按时间排序并保留最近 50 条。"""
    conn = get_db()
    try:
        student = conn.execute(
            "SELECT id, real_name, username FROM sys_user "
            "WHERE username=? AND user_type='STUDENT'",
            (username,),
        ).fetchone()
        if not student:
            raise RuntimeError("找不到演示学生：" + username)

        profile = conn.execute(
            "SELECT emotion_history FROM student_psych_profile WHERE student_id=?",
            (student["id"],),
        ).fetchone()
        try:
            history = json.loads(profile["emotion_history"] or "[]") if profile else []
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []

        existing_keys = {
            (str(item.get("time") or ""), str(item.get("text") or ""))
            for item in history
        }
        inserted = 0
        for entry in DEMO_ENTRIES:
            key = (entry["time"], entry["text"])
            if key in existing_keys:
                continue
            history.append({
                "time": entry["time"],
                "source": "心情记录",
                "tag": entry["tag"],
                "score": entry["score"],
                "text": entry["text"],
            })
            existing_keys.add(key)
            inserted += 1

        # 情绪趋势图按时间顺序绘制；只保留最近 50 条，与线上逻辑一致。
        history.sort(key=lambda item: str(item.get("time") or ""))
        history = history[-50:]
        latest = history[-1] if history else {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tag": "正常",
            "score": 20,
        }
        conn.execute(
            "INSERT INTO student_psych_profile "
            "(student_id,latest_emotion_tag,emotion_score,emotion_history,last_interaction_time) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(student_id) DO UPDATE SET "
            "latest_emotion_tag=excluded.latest_emotion_tag,"
            "emotion_score=excluded.emotion_score,"
            "emotion_history=excluded.emotion_history,"
            "last_interaction_time=excluded.last_interaction_time",
            (
                student["id"], latest.get("tag", "正常"),
                int(latest.get("score") or 20),
                json.dumps(history, ensure_ascii=False),
                latest.get("time", ""),
            ),
        )

        # 页面会用这段时间附近的同文本对话补齐“系统返回消息”。
        for entry in DEMO_ENTRIES:
            exists = conn.execute(
                "SELECT 1 FROM conversation_log "
                "WHERE user_id=? AND user_input=? AND create_time=? LIMIT 1",
                (student["id"], entry["text"], entry["time"]),
            ).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO conversation_log "
                "(user_id,agent_type,user_input,ai_output,emotion_tag,create_time) "
                "VALUES (?,?,?,?,?,?)",
                (
                    student["id"], "student_agent", entry["text"],
                    entry["response"], entry["tag"], entry["time"],
                ),
            )

        conn.commit()
        return {
            "student": student["real_name"] or student["username"],
            "inserted": inserted,
            "total_history": len(history),
            "latest_tag": latest.get("tag"),
            "latest_score": latest.get("score"),
        }
    finally:
        conn.close()


if __name__ == "__main__":
    print(seed_student())
