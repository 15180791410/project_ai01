"""为演示学生生成可预览的申请阶段档案和材料。

只处理 `student01` 的一份既有申请，所有文字和文件均明确标注“演示数据”，
不写入任何公司介绍、费用、项目规则等知识库内容。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from api.application import UPLOAD_ROOT, _ensure_stage_records
from db import ensure_migrations, get_db


DEMO_STAGES = {
    "选校定位": {
        "summary": "【演示数据】已确认申请国家、学习模式和具体方案，形成目标院校与备选方向。",
        "detail": {
            "decision_reason": "结合学生现有学历、意向国家和升学目标完成选校定位",
            "backup_schools": "备选方向已记录在申请档案中",
        },
    },
    "材料准备": {
        "summary": "【演示数据】已完成基础材料清单核验，学生材料状态可在附件区查看。",
        "detail": {
            "checklist_summary": "身份证明、学历材料、语言或能力材料、申请表",
            "missing_items": "无",
            "deadline": "以顾问实际通知为准",
        },
    },
    "递交申请": {
        "summary": "【演示数据】申请已向目标院校递交，保留申请编号和回执记录。",
        "detail": {
            "application_no": "DEMO-APP-0001",
            "submitted_at": "2026-11-19 19:23:21",
            "receipt_no": "DEMO-RECEIPT-0001",
            "submission_note": "演示流程记录，不代表真实院校受理结果",
        },
    },
    "面试/补充": {
        "summary": "【演示数据】已完成面试或补充材料环节，结果已记录。",
        "detail": {
            "interview_mode": "线上或线下（以实际安排为准）",
            "supplement": "演示材料已归档",
            "result": "演示流程通过",
        },
    },
    "收到Offer": {
        "summary": "【演示数据】已收到并归档录取通知，保留条件与接受期限信息。",
        "detail": {
            "offer_no": "DEMO-OFFER-0001",
            "conditions": "以正式录取通知为准",
            "accept_deadline": "演示数据，不产生真实截止日期",
            "result": "演示归档",
        },
    },
    "办理签证": {
        "summary": "【演示数据】签证材料已递交，受理编号和结果用于演示查询。",
        "detail": {
            "visa_type": "学生签证",
            "visa_no": "DEMO-VISA-0001",
            "submitted_items": "护照、录取材料、签证申请表等",
            "result": "演示流程完成",
        },
    },
    "行前准备": {
        "summary": "【演示数据】航班、住宿、保险和行前培训信息已形成演练记录。",
        "detail": {
            "flight_info": "演示航班信息，不代表真实行程",
            "accommodation": "演示住宿安排",
            "insurance": "演示保险记录",
            "orientation": "已完成行前说明",
            "emergency_contact": "仅展示字段，不填写真实敏感联系人",
        },
    },
    "已入学": {
        "summary": "【演示数据】学生已进入在读阶段，入学档案和后续服务入口已解锁。",
        "detail": {
            "student_no": "DEMO-STUDENT-0001",
            "campus": "演示校区",
            "study_major": "以入学档案中的方案为准",
            "duration": "以培养计划为准",
        },
    },
}


DEMO_MATERIALS = [
    ("材料准备", "护照信息页（演示）", "护照信息页演示材料.txt"),
    ("材料准备", "学历证明材料（演示）", "学历证明材料演示.txt"),
    ("收到Offer", "录取通知归档（演示）", "录取通知归档演示.txt"),
    ("办理签证", "签证受理回执（演示）", "签证受理回执演示.txt"),
]


DEMO_NOTICE = (
    "此文件仅用于粤教服务系统申请档案功能演示，"
    "不代表真实学生材料、录取结果、签证结果或公司承诺。\n"
)


def main() -> int:
    ensure_migrations()
    conn = get_db()
    try:
        student = conn.execute(
            "SELECT id,username,real_name FROM sys_user "
            "WHERE username='student01' AND user_type='STUDENT'"
        ).fetchone()
        if not student:
            print("未找到 student01，跳过演示档案生成")
            return 1
        application = conn.execute(
            "SELECT * FROM application_progress WHERE student_id=? "
            "ORDER BY id DESC LIMIT 1",
            (student["id"],),
        ).fetchone()
        if not application:
            print("student01 没有申请记录，跳过演示档案生成")
            return 1
        admin = conn.execute(
            "SELECT id FROM sys_user WHERE user_type='ADMIN' "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        operator_id = admin["id"] if admin else application["operator_id"]
        application_id = application["id"]
        _ensure_stage_records(conn, application_id)

        for stage, content in DEMO_STAGES.items():
            detail = dict(cast(dict[str, str], content["detail"]))
            if stage == "选校定位":
                detail.update({
                    "country": str(application["country"] or ""),
                    "application_mode": str(application["application_mode"] or ""),
                    "program": str(application["program"] or ""),
                    "university": str(application["university"] or ""),
                })
                detail = {key: value for key, value in detail.items() if value}
            conn.execute(
                "UPDATE application_stage_record SET summary=?,detail_json=?,"
                "source_type='SYSTEM',operator_id=?,update_time=datetime('now','localtime') "
                "WHERE application_id=? AND stage=?",
                (
                    content["summary"],
                    json.dumps(detail, ensure_ascii=False),
                    operator_id,
                    application_id,
                    stage,
                ),
            )
            exists = conn.execute(
                "SELECT 1 FROM application_history "
                "WHERE application_id=? AND stage=? AND action='载入演示档案' LIMIT 1",
                (application_id, stage),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO application_history "
                    "(application_id,stage,note,operator_id,action,source_type,"
                    "detail_json,create_time) VALUES (?,?,?,?,?,?,?,datetime('now','localtime'))",
                    (
                        application_id,
                        stage,
                        "【演示数据】载入阶段明细，不代表真实业务结果",
                        operator_id,
                        "载入演示档案",
                        "SYSTEM",
                        json.dumps({"demo": True}, ensure_ascii=False),
                    ),
                )

        UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
        for stage, material_name, file_name in DEMO_MATERIALS:
            storage_name = "demo_app_%d_%s.txt" % (
                application_id,
                "material_%d" % (DEMO_MATERIALS.index(
                    (stage, material_name, file_name)
                ) + 1),
            )
            file_path = UPLOAD_ROOT / storage_name
            file_path.write_text(
                DEMO_NOTICE + "\n材料演示名称：" + material_name + "\n",
                encoding="utf-8",
            )
            existing = conn.execute(
                "SELECT id FROM application_material "
                "WHERE application_id=? AND material_name=? AND is_active=1",
                (application_id, material_name),
            ).fetchone()
            if existing:
                material_id = existing["id"]
                conn.execute(
                    "UPDATE application_material SET status='已通过',file_name=?,"
                    "storage_name=?,mime_type='text/plain',file_size=?,content_note=?,"
                    "review_note='演示材料，不代表真实审核结论',uploaded_by=?,reviewed_by=?,"
                    "visible_to_student=1,update_time=datetime('now','localtime') WHERE id=?",
                    (
                        file_name,
                        storage_name,
                        file_path.stat().st_size,
                        "仅用于申请档案功能演示",
                        operator_id,
                        operator_id,
                        material_id,
                    ),
                )
            else:
                cursor = conn.execute(
                    "INSERT INTO application_material "
                    "(application_id,stage,material_name,required_flag,status,file_name,"
                    "storage_name,mime_type,file_size,content_note,review_note,uploaded_by,"
                    "reviewed_by,version,visible_to_student,is_active,create_time,update_time) "
                    "VALUES (?,?,?,0,'已通过',?,?,'text/plain',?,?,?,?,?,1,1,1,"
                    "datetime('now','localtime'),datetime('now','localtime'))",
                    (
                        application_id,
                        stage,
                        material_name,
                        file_name,
                        storage_name,
                        file_path.stat().st_size,
                        "仅用于申请档案功能演示",
                        "演示材料，不代表真实审核结论",
                        operator_id,
                        operator_id,
                    ),
                )
                material_id = cursor.lastrowid
            exists = conn.execute(
                "SELECT 1 FROM application_history "
                "WHERE application_id=? AND related_material_id=? "
                "AND action='载入演示材料' LIMIT 1",
                (application_id, material_id),
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO application_history "
                    "(application_id,stage,note,operator_id,action,source_type,"
                    "related_material_id,detail_json,create_time) "
                    "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                    (
                        application_id,
                        stage,
                        "【演示数据】载入演示附件：" + material_name,
                        operator_id,
                        "载入演示材料",
                        "SYSTEM",
                        material_id,
                        json.dumps({"demo": True, "file_name": file_name}, ensure_ascii=False),
                    ),
                )
        conn.commit()
        print("已生成 student01 申请档案演示数据：application_id=%d" % application_id)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
