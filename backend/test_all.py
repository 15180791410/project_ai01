# -*- coding: utf-8 -*-
"""ai端到端测试。

默认只运行确定性的业务与权限测试；加 --dify 才会调用真实模型。
测试只创建带 qa_ 前缀的账号和本轮精确 ID 的数据，清理时不会删除其他用户数据。
"""
import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from api.auth import _direct_recommendation_answer
from service.emotion_logic import (
    analyze as analyze_emotion,
    assess_model_response,
    enforce_safety_status,
    merge_model_score,
    needs_model_review,
)
from service.rule_logic import normalize_education
from service.score_chat_logic import _new_score_value
from utils import time_parser

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.getenv("YUEJIAO_BASE", "http://127.0.0.2:8000").rstrip("/")
PASS = 0
FAIL = 0
FAILED = []


def request(method, path, body=None, params=None, token="", timeout=25):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            data = json.loads(raw)
        except Exception:
            data = {"raw": raw}
        return exc.code, data
    except Exception as exc:
        return 0, {"error": str(exc)}


def request_raw(path, token="", timeout=25):
    """下载附件时读取原始字节，不把文件二进制误当 JSON。"""
    url = BASE + path
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read()
    except Exception as exc:
        return 0, "", str(exc).encode("utf-8")


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("[PASS]", name, detail)
    else:
        FAIL += 1
        FAILED.append(name)
        print("[FAIL]", name, detail)


def ok(status, data):
    return status == 200 and isinstance(data, dict) and data.get("code") == 200


def section(text):
    print("\n" + "=" * 68)
    print(text)
    print("=" * 68)


def login_account(username, password):
    """登录演示或测试账号，返回登录数据中的 data 对象。"""
    status, payload = request("POST", "/api/auth/login", {
        "username": username,
        "password": password,
    })
    return payload.get("data") if ok(status, payload) else None


def main():
    global BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--dify", action="store_true", help="额外调用三条真实 Dify 会话")
    args = parser.parse_args()
    BASE = args.base.rstrip("/")

    stamp = time.strftime("%m%d%H%M%S")
    qa_student_name = "qa_student_" + stamp
    qa_admin_name = "qa_admin_" + stamp
    qa_staff_name = "qa_staff_" + stamp
    student_pwd = "Qa!student2026"
    admin_pwd = "Qa!admin2026"
    staff_pwd = "Qa!staff2026"

    qa_uids = []
    lead_ids = []
    event_ids = []
    app_ids = []
    leave_ids = []
    ticket_ids = []
    registration_contacts = []
    course_name = "QA课程_" + stamp
    qa_archive_material_id = 0
    qa_archive_file_name = "qa_archive_" + stamp + ".txt"
    qa_archive_file_bytes = ("QA 申请材料内容 " + stamp).encode("utf-8")

    section("0. 服务与初始化")
    status, data = request("GET", "/health")
    check("健康检查", status == 200 and data.get("status") == "ok", str(data))
    status, data = request("GET", "/api/health")
    check("需求规格健康检查兼容路径", status == 200 and data.get("status") == "ok", str(data))
    if status != 200:
        raise SystemExit("后端不可用：" + BASE)

    status, setup = request("GET", "/api/auth/setup-status")
    if not ok(status, setup):
        raise SystemExit("无法读取初始化状态")

    admin = None
    employee_user = os.getenv("YUEJIAO_EMP_USER", "").strip()
    employee_pass = os.getenv("YUEJIAO_EMP_PASS", "").strip()
    if employee_user:
        status, data = request("POST", "/api/auth/login", {
            "username": employee_user, "password": employee_pass,
        })
        check("环境变量员工登录", ok(status, data), str(data.get("msg")))
        if ok(status, data):
            admin = data["data"]
    elif setup.get("data", {}).get("needs_setup"):
        status, data = request("POST", "/api/auth/setup", {
            "username": qa_admin_name, "password": admin_pwd,
            "real_name": "QA 管理员", "security_question": "测试问题",
            "security_answer": "测试答案",
        })
        check("临时管理员初始化", ok(status, data), str(data.get("msg")))
        if ok(status, data):
            admin = data["data"]
            qa_uids.append(admin["user_id"])
    else:
        print("[SKIP] 系统已有员工账号；设置 YUEJIAO_EMP_USER/YUEJIAO_EMP_PASS 可执行员工用例")

    if not admin:
        cleanup(qa_uids, lead_ids, event_ids, app_ids, registration_contacts)
        return finish()
    atok = admin["token"]

    # 全角色权限验收使用项目演示账号；环境变量可覆盖，便于部署到其他环境后复用。
    homeroom = login_account(
        os.getenv("YUEJIAO_HOMEROOM_USER", "teacher01"),
        os.getenv("YUEJIAO_HOMEROOM_PASS", "Teacher@2026"),
    )
    advisor = login_account(
        os.getenv("YUEJIAO_ADVISOR_USER", "advisor01"),
        os.getenv("YUEJIAO_ADVISOR_PASS", "Advisor@2026"),
    )
    advisor2 = login_account(
        os.getenv("YUEJIAO_ADVISOR2_USER", "advisor02"),
        os.getenv("YUEJIAO_ADVISOR2_PASS", "Advisor@2026"),
    )
    leader = login_account(
        os.getenv("YUEJIAO_LEADER_USER", "leader01"),
        os.getenv("YUEJIAO_LEADER_PASS", "Leader@2026"),
    )
    check("班主任演示账号可登录", bool(homeroom), "teacher01")
    check("顾问演示账号可登录", bool(advisor), "advisor01")
    check("第二顾问演示账号可登录", bool(advisor2), "advisor02")
    check("领导演示账号可登录", bool(leader), "leader01")
    check("顾问拥有活动报名查看权限",
          bool(advisor) and "event_registration.read" in advisor.get("permissions", []),
          str(advisor.get("permissions", [])) if advisor else "未登录")

    section("1. 认证、角色与数据归属")
    status, data = request("GET", "/api/admin/alerts")
    check("未登录读后台预警被拒", status == 401, f"HTTP {status}")

    status, data = request("POST", "/api/auth/register", {
        "username": qa_student_name, "password": student_pwd,
        "real_name": "QA 留学生", "contact_info": "qa_student@example.com",
        "security_question": "测试问题", "security_answer": "测试答案",
    })
    check("学生注册", ok(status, data), str(data.get("msg")))
    if not ok(status, data):
        cleanup(qa_uids, lead_ids, event_ids, app_ids, registration_contacts)
        return finish()
    student = data["data"]
    stu_id = student["user_id"]
    stok = student["token"]
    qa_uids.append(stu_id)
    status, data = request("POST", "/api/auth/recovery-question", {"username": qa_student_name})
    check("读取密码找回问题", ok(status, data) and data.get("data", {}).get("security_question"),
          str(data.get("msg")))
    status, data = request("POST", "/api/auth/forgot-password", {
        "username": qa_student_name, "security_answer": "错误答案",
        "new_password": "Qa!recovered2026",
    })
    check("错误安全问题被拒", status == 400, f"HTTP {status}")
    status, data = request("POST", "/api/auth/forgot-password", {
        "username": qa_student_name, "security_answer": "测试答案",
        "new_password": "Qa!recovered2026",
    })
    check("通过安全问题找回密码", ok(status, data), str(data.get("msg")))
    status, data = request("POST", "/api/auth/login", {
        "username": qa_student_name, "password": "Qa!recovered2026",
    })
    check("找回后新密码可登录", ok(status, data), str(data.get("msg")))
    if ok(status, data):
        stok = data["data"]["token"]

    status, data = request("GET", "/api/teacher/pending-leaves", token=stok)
    check("学生访问员工接口被拒", status == 403, f"HTTP {status}")
    status, data = request("GET", "/api/admin/alerts", token=stok)
    check("学生访问后台接口被拒", status == 403, f"HTTP {status}")

    status, data = request("GET", "/api/auth/me", token=stok)
    check("Authorization 头登录态", ok(status, data), str(data.get("msg")))
    status, data = request("GET", "/api/student/my_leaves", params={"token": stok})
    check("禁止通过查询参数传递登录令牌", status == 401, f"HTTP {status}")
    status, data = request("GET", "/api/employee/search", token=atok, params={
        "name": qa_student_name,
    })
    check("员工统一搜索可命中学生档案",
          ok(status, data)
          and any(row.get("id") == stu_id for row in data.get("data", {}).get("students", [])),
          str(data.get("data")))
    status, data = request("POST", "/api/auth/dify-chat", {
        "token": atok,
        "query": "帮我查一下许同学的跟进记录",
    })
    customer_answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
    check("员工客户查询返回结构化学生/客户档案",
          ok(status, data)
          and "许安娜" in customer_answer
          and "联系方式" in customer_answer
          and ("跟进历史" in customer_answer or "客户线索跟进记录" in customer_answer)
          and ("状态" in customer_answer or "学籍阶段" in customer_answer),
          customer_answer or str(data.get("msg")))
    status, data = request("GET", "/api/student/stage", token=stok)
    check("咨询期学生可直接进入就读申请",
          ok(status, data)
          and data.get("data", {}).get("capabilities", {}).get("application") is True,
          str(data.get("data", {}).get("capabilities")))
    check("咨询期账号不开放心情记录能力",
          ok(status, data)
          and data.get("data", {}).get("capabilities", {}).get("emotion") is False,
          str(data.get("data", {}).get("capabilities")))
    status, _ = request("POST", "/api/student/emotion", {
        "token": stok, "text": "刚注册账号不应写入心情记录",
    })
    check("咨询期账号写心情记录接口被拒", status == 403, f"HTTP {status}")
    status, _ = request("GET", "/api/student/psych-profile", token=stok)
    check("咨询期账号读取心情画像接口被拒", status == 403, f"HTTP {status}")

    status, data = request("GET", "/api/application/options")
    option_programs = data.get("data", {}).get("programs", []) if ok(status, data) else []
    germany_programs = [
        row for row in option_programs if row.get("country") == "德国"
    ]
    check("就读申请包含德国具体细分方案",
          ok(status, data)
          and len(germany_programs) >= 6
          and any(row.get("project_name", "").startswith("中德双元制·")
                  for row in germany_programs),
          f"Germany={len(germany_programs)}")

    section("1.1 全角色功能权限")
    if advisor:
        at = advisor["token"]
        status, _ = request("GET", "/api/teacher/pending-leaves", token=at)
        check("顾问不能查看/审批请假", status == 403, f"HTTP {status}")
        status, data = request("GET", "/api/admin/alerts", token=at)
        check("顾问可只读本人学生心理预警", ok(status, data), str(data.get("msg")))
        status, _ = request("POST", "/api/content/alert/handle", {
            "token": at, "alert_id": 1, "note": "顾问不应处理预警",
        })
        check("顾问不能处理心理预警", status == 403, f"HTTP {status}")
        status, _ = request("POST", "/api/content/students/stage", {
            "token": at, "student_id": stu_id, "stage": "ENROLLED",
        })
        check("顾问不能修改学生学籍阶段", status == 403, f"HTTP {status}")
        status, data = request("GET", "/api/admin/leads", token=at)
        check("顾问可查看本人客户线索", ok(status, data), str(data.get("msg")))

    if homeroom:
        ht = homeroom["token"]
        status, _ = request("POST", "/api/content/events/save", {
            "token": ht, "event_name": "越权活动不应创建",
        })
        check("班主任不能新增活动", status == 403, f"HTTP {status}")
        status, _ = request("POST", "/api/content/programs/save", {
            "token": ht, "project_name": "越权方案不应创建",
        })
        check("留学方案禁止写入后端", status == 405, f"HTTP {status}")
        status, _ = request("GET", "/api/lead/list", token=ht)
        check("班主任不能查看客户线索", status == 403, f"HTTP {status}")
        status, _ = request("GET", "/api/auth/staff", token=ht)
        check("班主任不能查看员工账号清单", status == 403, f"HTTP {status}")
        status, _ = request("POST", "/api/application/create", {
            "token": ht, "student_id": stu_id, "country": "新加坡",
        })
        check("班主任不能替学生创建申请", status == 403, f"HTTP {status}")
        status, data = request("GET", "/api/content/students-by-country", token=ht)
        check("班主任学生名单受班级范围限制", ok(status, data), str(data.get("msg")))
        status, _ = request("POST", "/api/employee/nl2sql", {
            "token": ht, "command": "统计一下一共有多少客户",
        })
        check("班主任 NL2SQL 越权查客户被拒", status == 403, f"HTTP {status}")

    if leader:
        lt = leader["token"]
        status, _ = request("GET", "/api/auth/staff", token=lt)
        check("领导不能管理员工账号", status == 403, f"HTTP {status}")
        status, _ = request("POST", "/api/content/events/save", {
            "token": lt, "event_name": "领导越权活动不应创建",
        })
        check("领导不能修改系统内容", status == 403, f"HTTP {status}")
        status, data = request("GET", "/api/admin/leads", token=lt)
        check("领导可查看全部业务线索", ok(status, data), str(data.get("msg")))
        status, data = request("GET", "/api/admin/alerts", token=lt)
        check("领导可查看心理预警", ok(status, data), str(data.get("msg")))

    # 管理员一次登录后直接切换演示角色，不需要目标账号密码。
    status, data = request("POST", "/api/auth/switch-role", {
        "token": atok, "role": "ADVISOR",
    })
    preview_advisor = data.get("data", {}) if ok(status, data) else {}
    check("管理员无密码切换到顾问视图",
          ok(status, data)
          and preview_advisor.get("active_role") == "ADVISOR"
          and preview_advisor.get("user_type") == "EMPLOYEE"
          and preview_advisor.get("role_group") == "EMPLOYEE",
          str(data.get("msg")))
    preview_token = preview_advisor.get("token", atok)
    status, _ = request("GET", "/api/auth/staff", token=preview_token)
    check("顾问演示视图不能管理账号", status == 403, f"HTTP {status}")
    status, data = request("GET", "/api/admin/alerts", token=preview_token)
    check("顾问演示视图可读取预警", ok(status, data), str(data.get("msg")))
    status, data = request("POST", "/api/auth/switch-role", {
        "token": preview_token, "role": "HOMEROOM",
    })
    preview_homeroom = data.get("data", {}) if ok(status, data) else {}
    check("演示视图可继续切换到班主任",
          ok(status, data) and preview_homeroom.get("active_role") == "HOMEROOM",
          str(data.get("msg")))
    status, data = request("POST", "/api/auth/switch-role", {
        "token": preview_homeroom.get("token", preview_token), "role": "MAINTAINER",
    })
    preview_maintainer = data.get("data", {}) if ok(status, data) else {}
    check("演示视图可继续切换到维护人员",
          ok(status, data) and preview_maintainer.get("active_role") == "MAINTAINER",
          str(data.get("msg")))
    status, _ = request("GET", "/api/admin/alerts", token=preview_maintainer.get("token", ""))
    check("维护人员演示视图不能读取业务隐私", status == 403, f"HTTP {status}")
    status, data = request("POST", "/api/auth/switch-role", {
        "token": preview_maintainer.get("token", preview_token), "role": "ADMIN",
    })
    check("演示视图可切回管理员", ok(status, data) and data.get("data", {}).get("active_role") == "ADMIN")

    status, data = request("POST", "/api/content/programs/save", {
        "token": atok, "project_name": "2+2 加拿大本科班", "country": "加拿大",
        "category": "本科", "knowledge_verified": True,
        "source_document": "新加坡国际本硕升学计划.docx",
    })
    check("方案主数据只能通过 Dify 不得写入后端", status == 405, f"HTTP {status}")

    status, data = request("POST", "/api/content/students/stage", {
        "token": atok, "student_id": stu_id, "stage": "ENROLLED",
        "note": "QA 自动化测试临时置为在读",
    })
    check("咨询期不能无申请直接跳到在读",
          status == 400,
          str(data.get("msg")))
    status, data = request("POST", "/api/content/students/stage", {
        "token": atok, "student_id": stu_id, "stage": "ENROLLED",
        "note": "QA 自动化测试管理员纠错", "force": True,
    })
    check("管理员带原因纠错后可置为在读", ok(status, data), str(data.get("msg")))

    section("2. 请假、审批和通知")
    status, data = request("POST", "/api/student/leave", {
        "token": stok, "reason": "QA 非法日期测试",
        "start_time": "2026-12-05", "end_time": "2026-12-01",
    })
    check("结束日期早于开始日期被拒", status == 400, f"HTTP {status}")
    status, data = request("POST", "/api/student/leave", {
        "token": stok, "reason": "QA 明确日期测试",
        "start_time": "2026-12-01", "end_time": "2026-12-03",
    })
    check("明确日期请假", ok(status, data), str(data.get("msg")))
    if ok(status, data):
        leave_record = data["data"]
        check("请假日期未丢失",
              leave_record["开始"] == "2026-12-01" and leave_record["结束"] == "2026-12-03",
              str(leave_record))
        check("请假提交返回完整记录",
              isinstance(data.get("data"), dict)
              and data["data"].get("id") is not None,
              repr(leave_record))
    status, data = request("POST", "/api/student/leave", {
        "token": stok, "reason": "QA 明确日期测试",
        "start_time": "2026-12-01", "end_time": "2026-12-03",
    })
    check("相同待审批请假不会重复创建", status == 409, f"HTTP {status}")
    status, data = request("POST", "/api/student/leave", {
        "token": stok, "reason": "我25号要请假",
    })
    check("中文日期‘25号’正确解析",
          ok(status, data)
          and data.get("data", {}).get("开始", "").endswith("-25")
          and data.get("data", {}).get("开始") == data.get("data", {}).get("结束"),
          str(data.get("data")))
    weekday_start, _, _, weekday_error = time_parser.parse_leave_time("周三请假1天")
    expected_wednesday = (
        datetime.now() + timedelta(days=(2 - datetime.now().weekday()) % 7)
    ).strftime("%Y-%m-%d")
    check("周几请假解析为最近未来日期",
          not weekday_error and weekday_start == expected_wednesday,
          f"{weekday_start} expected={expected_wednesday}")
    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "我要请假", "action": "collect",
    })
    prepare_missing = data.get("data", {}) if ok(status, data) else {}
    check("请假准备工具在字段大量缺失时只返回追问所需字段",
          ok(status, data)
          and prepare_missing.get("status_token") == "STATE_MISSING"
          and "leave_type" in prepare_missing.get("missing_fields", [])
          and "start_date" in prepare_missing.get("missing_fields", [])
          and "reason" in prepare_missing.get("missing_fields", []),
          str(prepare_missing or data.get("msg")))

    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "下周四请假1天", "action": "collect",
        "date_text": "下周四请假1天",
    })
    prepare_partial = data.get("data", {}) if ok(status, data) else {}
    check("请假准备工具保留已识别日期并只追问类型和原因",
          ok(status, data)
          and prepare_partial.get("status_token") == "STATE_MISSING"
          and prepare_partial.get("fields", {}).get("start_date")
          and "leave_type" in prepare_partial.get("missing_fields", [])
          and "reason" in prepare_partial.get("missing_fields", []),
          str(prepare_partial or data.get("msg")))

    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "病假，因为感冒", "action": "collect",
        "leave_type": "病假", "reason": "感冒",
    })
    prepare_confirm = data.get("data", {}) if ok(status, data) else {}
    check("请假字段齐全后进入 Dify 复述确认阶段且不写库",
          ok(status, data)
          and prepare_confirm.get("status_token") == "STATE_CONFIRM"
          and prepare_confirm.get("fields", {}).get("days") == 1
          and not prepare_confirm.get("confirmation_token"),
          str(prepare_confirm or data.get("msg")))

    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "确认", "action": "confirm",
    })
    prepare_ready = data.get("data", {}) if ok(status, data) else {}
    confirmation_token = prepare_ready.get("confirmation_token", "")
    check("用户确认后 prepare_leave 签发一次性确认令牌",
          ok(status, data)
          and prepare_ready.get("status_token") == "STATE_READY"
          and bool(confirmation_token),
          str(prepare_ready or data.get("msg")))
    status, data = request("POST", "/api/student/leave/confirm", {
        "token": stok, "confirmation_token": confirmation_token,
    })
    confirmed_leave = data.get("data", {}) if ok(status, data) else {}
    first_chat_leave_id = confirmed_leave.get("leave", {}).get("leave_id", 0)
    check("confirm_leave 消费确认令牌后才正式写入请假单",
          ok(status, data)
          and confirmed_leave.get("status_token") == "STATE_SUBMITTED"
          and bool(first_chat_leave_id),
          str(confirmed_leave or data.get("msg")))
    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "下周四请假1天", "action": "collect",
        "date_text": "下周四请假1天", "leave_type": "病假", "reason": "感冒",
    })
    duplicate_prepare = data.get("data", {}) if ok(status, data) else {}
    check("请假准备工具在确认前识别日期重叠并返回原单号",
          ok(status, data)
          and duplicate_prepare.get("status_token") == "STATE_DUPLICATE"
          and duplicate_prepare.get("duplicate", {}).get("id") == first_chat_leave_id,
          str(duplicate_prepare or data.get("msg")))

    range_fields = time_parser.extract_leave_date_fields("下周一到下周三请事假，家里有事")
    check("日期解析支持下周一到下周三并自动计算三天",
          range_fields.get("start") == "2026-11-23"
          and range_fields.get("end") == "2026-11-25"
          and range_fields.get("days") == 3,
          str(range_fields))
    invalid_fields = time_parser.extract_leave_date_fields(
        "事假，2026-02-30 至 2026-03-02，原因家里有事"
    )
    check("非法日期返回明确错误而不是静默改到三月二号",
          "2026-02-30" in invalid_fields.get("error", "")
          and not invalid_fields.get("start"),
          str(invalid_fields))
    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "事假，2026-02-30 至 2026-03-02，原因家里有事",
        "action": "collect", "leave_type": "事假", "reason": "家里有事",
        "date_text": "2026-02-30 至 2026-03-02",
    })
    invalid_prepare = data.get("data", {}) if ok(status, data) else {}
    check("Dify 准备工具明确返回非法日期错误",
          ok(status, data)
          and invalid_prepare.get("status_token") == "STATE_ERROR"
          and "2026-02-30" in invalid_prepare.get("error", ""),
          str(invalid_prepare or data.get("msg")))
    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "事假，2026-10-07到2026-10-09，家里有事",
        "action": "collect", "leave_type": "事假", "reason": "家里有事",
        "date_text": "2026-10-07到2026-10-09",
    })
    past_prepare = data.get("data", {}) if ok(status, data) else {}
    check("请假准备工具拒绝早于今天的日期",
          ok(status, data)
          and past_prepare.get("status_token") == "STATE_ERROR"
          and "不能早于今天" in past_prepare.get("error", ""),
          str(past_prepare or data.get("msg")))
    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "我要请假", "action": "collect",
        "leave_type": "", "start_date": "", "end_date": "",
        "date_text": "", "reason": "",
    })
    fresh_leave = data.get("data", {}) if ok(status, data) else {}
    fresh_fields = fresh_leave.get("fields", {})
    check("用户重新说我要请假时清空旧草稿日期",
          ok(status, data)
          and fresh_leave.get("status_token") == "STATE_MISSING"
          and not fresh_fields.get("start_date")
          and not fresh_fields.get("end_date")
          and not fresh_fields.get("leave_type")
          and not fresh_fields.get("reason"),
          str(fresh_leave or data.get("msg")))
    status, data = request("POST", "/api/student/leave", {
        "token": stok, "reason": "QA 历史日期测试",
        "start_time": "2026-10-07", "end_time": "2026-10-09",
    })
    check("请假正式接口也拒绝历史日期",
          status == 400 and "不能早于今天" in data.get("msg", ""),
          str(data.get("msg")))

    vague_date = time_parser.extract_leave_date_fields("过几天请假")
    check("模糊日期不会被自动解释为今天",
          vague_date.get("ambiguous") is True and not vague_date.get("start"),
          str(vague_date))
    status, data = request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "过几天请假两天", "action": "collect",
        "date_text": "过几天", "leave_type": "事假", "reason": "休息",
    })
    vague_prepare = data.get("data", {}) if ok(status, data) else {}
    check("模糊相对日期在准备阶段要求补充具体日期",
          ok(status, data)
          and vague_prepare.get("status_token") == "STATE_MISSING"
          and "start_date" in vague_prepare.get("missing_fields", []),
          str(vague_prepare or data.get("msg")))
    request("POST", "/api/student/leave/prepare", {
        "token": stok, "query": "取消请假", "action": "cancel",
    })

    status, data = request("POST", "/api/actions/prepare", {
        "token": stok, "query": "帮我报名活动",
        "action_type": "event_register", "user_action": "collect",
        "slots_json": "{}",
    })
    event_prepare = data.get("data", {}) if ok(status, data) else {}
    event_candidates = event_prepare.get("candidates", [])
    check("活动报名模糊时不直接写库并返回候选活动",
          ok(status, data)
          and event_prepare.get("status_token") == "STATE_AMBIGUOUS"
          and bool(event_candidates),
          str(event_prepare or data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": stok, "query": "报名1",
        "action_type": "event_register", "user_action": "collect",
        "slots_json": json.dumps({"selection": 1}, ensure_ascii=False),
    })
    event_confirm = data.get("data", {}) if ok(status, data) else {}
    check("活动选择后先复述确认仍不写库",
          ok(status, data)
          and event_confirm.get("status_token") == "STATE_CONFIRM"
          and "报名人" in event_confirm.get("preview", ""),
          str(event_confirm or data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": stok, "query": "确认",
        "action_type": "event_register", "user_action": "confirm",
        "slots_json": "{}",
    })
    event_ready = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": stok,
        "confirmation_token": event_ready.get("confirmation_token", ""),
    })
    event_exec = data.get("data", {}) if ok(status, data) else {}
    check("活动报名确认后才写入正式报名记录",
          ok(status, data)
          and event_exec.get("status_token") == "STATE_EXECUTED"
          and event_exec.get("changed") is True,
          str(event_exec or data.get("msg")))

    status, data = request("POST", "/api/actions/prepare", {
        "token": stok, "query": "课程资料没收到，帮我提交售后",
        "action_type": "feedback_create", "user_action": "collect",
        "slots_json": json.dumps({
            "feedback_content": "QA 通用动作售后",
            "feedback_detail": "课程资料未收到",
        }, ensure_ascii=False),
    })
    feedback_confirm = data.get("data", {}) if ok(status, data) else {}
    check("售后提交先复述确认不提前建单",
          ok(status, data)
          and feedback_confirm.get("status_token") == "STATE_CONFIRM",
          str(feedback_confirm or data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": stok, "query": "确认",
        "action_type": "feedback_create", "user_action": "confirm",
        "slots_json": "{}",
    })
    feedback_ready = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": stok,
        "confirmation_token": feedback_ready.get("confirmation_token", ""),
    })
    feedback_exec = data.get("data", {}) if ok(status, data) else {}
    check("售后确认后才创建正式工单",
          ok(status, data)
          and feedback_exec.get("status_token") == "STATE_EXECUTED"
          and feedback_exec.get("changed") is True,
          str(feedback_exec or data.get("msg")))

    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "给 QA 留学生录入口语成绩 88 分",
        "action_type": "score_upsert", "user_action": "collect",
        "slots_json": json.dumps({
            "student_id": stu_id, "course_name": "QA口语", "score_value": 88,
        }, ensure_ascii=False),
    })
    score_confirm = data.get("data", {}) if ok(status, data) else {}
    check("员工成绩修改先进入确认阶段",
          ok(status, data)
          and score_confirm.get("status_token") == "STATE_CONFIRM"
          and "88" in score_confirm.get("preview", ""),
          str(score_confirm or data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "确认", "action_type": "score_upsert",
        "user_action": "confirm", "slots_json": "{}",
    })
    score_ready = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": atok,
        "confirmation_token": score_ready.get("confirmation_token", ""),
    })
    check("成绩确认后才写入数据库",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_EXECUTED"
          and data.get("data", {}).get("changed") is True,
          str(data.get("data") or data.get("msg")))

    action_contact = "qa_action_" + stamp + "@example.com"
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "录入客户 QA动作客户",
        "action_type": "lead_create", "user_action": "collect",
        "slots_json": json.dumps({
            "customer_name": "QA动作客户",
            "contact_info": action_contact,
            "target_country": "新加坡",
        }, ensure_ascii=False),
    })
    lead_confirm = data.get("data", {}) if ok(status, data) else {}
    check("客户线索录入先复述确认",
          ok(status, data)
          and lead_confirm.get("status_token") == "STATE_CONFIRM"
          and action_contact in lead_confirm.get("preview", ""),
          str(lead_confirm or data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "确认", "action_type": "lead_create",
        "user_action": "confirm", "slots_json": "{}",
    })
    lead_ready = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": atok,
        "confirmation_token": lead_ready.get("confirmation_token", ""),
    })
    lead_action_result = data.get("data", {}) if ok(status, data) else {}
    if lead_action_result.get("lead_id"):
        lead_ids.append(int(lead_action_result["lead_id"]))
    check("客户线索确认后写入 CRM",
          ok(status, data)
          and lead_action_result.get("status_token") == "STATE_EXECUTED"
          and bool(lead_action_result.get("lead_id")),
          str(lead_action_result or data.get("msg")))
    registration_contacts.append(action_contact)

    def confirmed_action(token, query, action_type, slots):
        """执行 prepare→用户确认→execute，供新增确认链路回归复用。"""
        status, payload = request("POST", "/api/actions/prepare", {
            "token": token, "query": query, "action_type": action_type,
            "user_action": "collect",
            "slots_json": json.dumps(slots, ensure_ascii=False),
        })
        prepared = payload.get("data", {}) if ok(status, payload) else {}
        if prepared.get("status_token") != "STATE_CONFIRM":
            return prepared, {}, {}
        status, payload = request("POST", "/api/actions/prepare", {
            "token": token, "query": "确认", "action_type": action_type,
            "user_action": "confirm", "slots_json": "{}",
        })
        ready = payload.get("data", {}) if ok(status, payload) else {}
        if ready.get("status_token") != "STATE_READY":
            return prepared, ready, {}
        status, payload = request("POST", "/api/actions/execute", {
            "token": token,
            "confirmation_token": ready.get("confirmation_token", ""),
        })
        executed = payload.get("data", {}) if ok(status, payload) else {}
        return prepared, ready, executed

    report_prepare, _, report_exec = confirmed_action(
        atok, "帮我写今天的日报", "report_create",
        {"report_content": "QA日报" + stamp, "report_date": "2026-11-20"},
    )
    check("员工日报先预览确认再写入",
          report_prepare.get("status_token") == "STATE_CONFIRM"
          and report_exec.get("status_token") == "STATE_EXECUTED"
          and bool(report_exec.get("report_id")),
          str(report_exec or report_prepare))

    follow_lead_id = int(lead_action_result.get("lead_id") or 0)
    follow_prepare, _, follow_exec = confirmed_action(
        atok, "跟进客户", "lead_follow_up",
        {
            "lead_id": follow_lead_id, "lead_status": "跟进中",
            "follow_up_note": "QA 已电话沟通，等待家庭确认",
        },
    )
    check("客户跟进先预览确认再更新阶段和记录",
          follow_lead_id > 0
          and follow_prepare.get("status_token") == "STATE_CONFIRM"
          and follow_exec.get("status_token") == "STATE_EXECUTED"
          and follow_exec.get("changed") is True,
          str(follow_exec or follow_prepare))

    resolve_ticket_id = int(feedback_exec.get("ticket_id") or 0)
    resolve_prepare, _, resolve_exec = confirmed_action(
        atok, "解决售后", "feedback_resolve",
        {"ticket_id": resolve_ticket_id, "solution": "QA 已联系学生并补充资料"},
    )
    check("售后解决先预览方案，确认后才完成工单",
          resolve_ticket_id > 0
          and resolve_prepare.get("status_token") == "STATE_CONFIRM"
          and resolve_exec.get("status_token") == "STATE_EXECUTED"
          and resolve_exec.get("changed") is True,
          str(resolve_exec or resolve_prepare))

    if homeroom:
        status, data = request("POST", "/api/auth/dify-chat", {
            "token": homeroom["token"], "query": "帮我审批所有学生的请假",
        })
        approval_preview = data.get("data", {}).get("answer", "") if ok(status, data) else ""
        check("班主任助手按本人班级展示待审批请假",
              ok(status, data)
              and "批量审批请求" in approval_preview
              and "权限范围：本人班级学生" in approval_preview
              and "确认通过本人名下全部待审批请假" in approval_preview
              and "本次没有直接执行" in approval_preview
              and "知识库中没有相关信息" not in approval_preview,
              approval_preview)
    if advisor:
        status, data = request("POST", "/api/auth/dify-chat", {
            "token": advisor["token"], "query": "帮我审批所有学生的请假",
        })
        check("顾问助手不能越权审批请假",
              ok(status, data)
              and "没有请假审批权限" in data.get("data", {}).get("answer", ""),
              str(data.get("data") or data.get("msg")))

    status, data = request("GET", "/api/student/my_leaves", token=stok)
    rows = data.get("data", []) if ok(status, data) else []
    if rows:
        leave_ids.append(rows[0]["id"])
    check("学生只能查本人请假", ok(status, data) and rows and rows[0]["student_id"] == stu_id,
          f"rows={len(rows)}")
    if leave_ids:
        if homeroom:
            status, data = request("POST", "/api/teacher/approve", {
                "token": homeroom["token"], "leave_id": leave_ids[0],
                "approved": True, "comment": "跨班级审批不应成功",
            })
            check("班主任不能审批其他班级学生请假", status == 403, f"HTTP {status}")
        status, data = request("POST", "/api/teacher/approve", {
            "token": atok, "leave_id": leave_ids[0], "approved": True,
            "comment": "QA 审批通过",
        })
        check("员工审批请假", ok(status, data), str(data.get("msg")))
    status, data = request("POST", "/api/teacher/approve", {
        "token": atok, "leave_id": 999999999, "approved": True,
    })
    check("不存在的请假返回 404", status == 404, f"HTTP {status}")

    section("3. 情绪、心理画像与售后")
    gas_risk = analyze_emotion("我想打开煤气罐，关上门窗，好好的睡一觉")
    check("煤气与密闭空间组合触发后端高危规则",
          gas_risk.get("risk_level") == "高" and gas_risk.get("score") >= 90,
          str(gas_risk))
    ambiguous_text = "我最近总在桥边发呆，脑子里一直想着解脱"
    model_signal = assess_model_response(
        ambiguous_text,
        "请马上离开危险环境，我已经通知了你的班主任。你可以拨打24小时生命热线。",
    )
    check("规则未命中的危险语义可由Dify安全回复兜底",
          needs_model_review(ambiguous_text)
          and bool(model_signal)
          and model_signal.get("score") >= 70,
          str(model_signal))
    merged_high = merge_model_score(analyze_emotion("我想死"), 20, "模型误判低风险")
    check("模型低分不能把后端高危规则降级",
          merged_high.get("risk_level") == "高" and merged_high.get("score") >= 90,
          str(merged_high))
    guarded_answer = enforce_safety_status(
        "我已经通知了你的班主任，老师会马上来陪你。请拨打热线。",
        {"risk_level": "高", "alerted": False, "notified_count": 0},
    )
    check("模型不能虚构已通知老师的承诺",
          "我已经通知了你的班主任" not in guarded_answer
          and "未生成新的高危预警" in guarded_answer,
          guarded_answer)
    status, data = request("POST", "/api/student/emotion", {
        "token": stok, "text": "最近有点焦虑",
    })
    check("关注情绪只记录不强制预警",
          ok(status, data)
          and data.get("data", {}).get("分数") == 55
          and not data.get("data", {}).get("已触发预警"),
          str(data.get("data")))
    status, data = request("POST", "/api/student/emotion", {
        "token": stok, "text": "最近压力很大，感觉活不下去了",
    })
    check("高危情绪触发预警", ok(status, data) and data.get("data", {}).get("已触发预警"))
    db_path = Path(os.getenv(
        "YUEJIAO_DB_FILE", str(Path(__file__).resolve().parent / "yuejiao.db")
    ))
    conn = sqlite3.connect(str(db_path))
    try:
        alert_recipients = {
            row[0] for row in conn.execute(
                "SELECT DISTINCT CASE "
                "WHEN u.user_type='ADMIN' THEN 'ADMIN' "
                "WHEN u.staff_role='LEADER' THEN 'LEADER' "
                "WHEN u.staff_role='HOMEROOM' THEN 'HOMEROOM' "
                "ELSE u.user_type END "
                "FROM notification n JOIN sys_user u ON u.id=n.user_id "
                "WHERE n.type='预警' AND n.content LIKE '%QA 留学生%'"
            ).fetchall()
        }
    finally:
        conn.close()
    check("高危预警升级通知班主任管理员和领导",
          {"HOMEROOM", "ADMIN", "LEADER"}.issubset(alert_recipients),
          str(alert_recipients))
    status, data = request("POST", "/api/student/emotion", {
        "token": stok, "text": "我现在心情不好，想把同学全干掉",
    })
    check("伤害他人的暴力意图触发最高危预警",
          ok(status, data)
          and data.get("data", {}).get("标签") == "高危"
          and data.get("data", {}).get("分数") == 100
          and data.get("data", {}).get("已触发预警"),
          str(data.get("data")))
    status, data = request("POST", "/api/student/emotion", {
        "token": stok, "text": "我想结束自己的生命，已经写了遗书",
    })
    check("轻生变体与遗书表达触发高危预警",
          ok(status, data)
          and data.get("data", {}).get("标签") == "高危"
          and data.get("data", {}).get("分数") == 90,
          str(data.get("data")))
    status, data = request("POST", "/api/student/emotion", {
        "token": stok, "text": "今天收到录取通知，很开心",
    })
    check("积极情绪按需求记为15分",
          ok(status, data) and data.get("data", {}).get("分数") == 15,
          str(data.get("data")))
    status, data = request("POST", "/api/student/emotion", {
        "token": stok,
        "text": "外部模型已评估这段文本",
        "emotion_score": 75,
        "emotion_tag": "高危",
        "risk_level": "高",
    })
    check("情绪接口支持采用外部模型分值",
          ok(status, data)
          and data.get("data", {}).get("分数") == 75
          and data.get("data", {}).get("标签") == "高危"
          and data.get("data", {}).get("已触发预警"),
          str(data.get("data")))
    check("无法归一化的学历原样返回",
          normalize_education("火星学历") == "火星学历",
          normalize_education("火星学历"))
    status, data = request("GET", "/api/student/psych-profile", token=stok)
    check("心情记录返回个人历史、预警和返回消息",
          ok(status, data)
          and "history" in data.get("data", {})
          and bool(data.get("data", {}).get("alerts"))
          and any(row.get("trigger_reason")
                  for row in data.get("data", {}).get("alerts", [])),
          str(data.get("msg")))
    check("心情记录历史包含系统返回消息",
          ok(status, data)
          and any(row.get("system_response")
                  for row in data.get("data", {}).get("history", [])),
          str((data.get("data", {}).get("history") or [])[:1]))

    status, data = request("GET", "/api/admin/alerts", token=atok)
    check("员工可查看心理预警", ok(status, data), str(data.get("msg")))
    check("心理预警列表返回情绪分值",
          ok(status, data)
          and bool(data.get("data"))
          and all(row.get("emotion_score") is not None for row in data.get("data", [])),
          str(data.get("data", [])[:1]))
    qa_alerts = [
        row for row in (data.get("data", []) if ok(status, data) else [])
        if row.get("student_id") == stu_id and row.get("status") != "已解决"
    ]
    if qa_alerts and homeroom:
        status, _ = request("POST", "/api/content/alert/handle", {
            "token": homeroom["token"], "alert_id": qa_alerts[0]["id"],
            "note": "跨班级处理不应成功",
        })
        check("班主任不能处理其他班级学生预警", status == 403, f"HTTP {status}")

    status, data = request("POST", "/api/student/feedback", {
        "token": stok, "content": "QA 售后反馈", "detail": "课程资料需要补充",
    })
    check("提交售后反馈并返回工单记录",
          ok(status, data)
          and data.get("data", {}).get("status") == "待处理"
          and bool(data.get("data", {}).get("id")),
          str(data.get("data") or data.get("msg")))
    status, data = request("GET", "/api/student/my_feedbacks", token=stok)
    if ok(status, data) and data.get("data"):
        ticket_ids.append(data["data"][0]["id"])
    feedback_count_before_query = len(data.get("data", [])) if ok(status, data) else -1
    status, data = request("POST", "/api/auth/dify-chat", {
        "token": stok,
        "query": "我的售后服务",
    })
    check("学生查询售后服务读取真实工单而不是提交反馈",
          ok(status, data)
          and "QA 售后反馈" in data.get("data", {}).get("answer", ""),
          str(data.get("data", {}).get("answer") or data.get("msg")))
    status, data = request("POST", "/api/auth/dify-chat", {
        "token": stok,
        "query": "查询我有哪些售后",
    })
    check("学生查询售后列表返回实际工单内容",
          ok(status, data)
          and "售后工单" in data.get("data", {}).get("answer", "")
          and "QA 售后反馈" in data.get("data", {}).get("answer", ""),
          str(data.get("data", {}).get("answer") or data.get("msg")))
    status, data = request("GET", "/api/student/my_feedbacks", token=stok)
    feedback_count_after_query = len(data.get("data", [])) if ok(status, data) else -2
    check("售后查询不会新增工单",
          feedback_count_before_query == feedback_count_after_query,
          f"{feedback_count_before_query} -> {feedback_count_after_query}")

    status, data = request("POST", "/api/student/feedback", {
        "token": stok, "content": "QA 待撤销售后",
    })
    check("创建可撤销售后工单", ok(status, data), str(data.get("msg")))
    status, data = request("GET", "/api/student/my_feedbacks", token=stok)
    cancel_ticket_id = 0
    if ok(status, data):
        for row in data.get("data", []):
            if row.get("content") == "QA 待撤销售后":
                cancel_ticket_id = row["id"]
    before_cancel = len(data.get("data", [])) if ok(status, data) else -1
    status, data = request("POST", "/api/auth/dify-chat", {
        "token": stok, "query": "删除售后",
    })
    check("未指定工单的删除售后不会创建新工单",
          ok(status, data)
          and ("请指定" in data.get("data", {}).get("answer", "")
               or "请选择" in data.get("data", {}).get("answer", "")),
          str(data.get("data", {}).get("answer") or data.get("msg")))
    status, data = request("GET", "/api/student/my_feedbacks", token=stok)
    after_prompt = len(data.get("data", [])) if ok(status, data) else -2
    check("删除售后提示前后工单数不变",
          before_cancel == after_prompt and cancel_ticket_id > 0,
          f"{before_cancel} -> {after_prompt}, id={cancel_ticket_id}")
    if cancel_ticket_id:
        status, data = request("POST", "/api/auth/dify-chat", {
            "token": stok, "query": "删除售后 " + str(cancel_ticket_id),
        })
        check("指定编号后先要求确认撤回售后",
              ok(status, data)
              and "确认" in data.get("data", {}).get("answer", ""),
              str(data.get("data", {}).get("answer") or data.get("msg")))
        status, data = request("POST", "/api/auth/dify-chat", {
            "token": stok, "query": "确认",
        })
        check("确认后可撤回本人待处理售后",
              ok(status, data)
              and ("撤回" in data.get("data", {}).get("answer", "")
                   or "成功" in data.get("data", {}).get("answer", "")),
              str(data.get("data", {}).get("answer") or data.get("msg")))
        status, data = request("GET", "/api/student/my_feedbacks", token=stok)
        check("已撤销售后从学生工单列表隐藏",
              ok(status, data)
              and not any(row.get("id") == cancel_ticket_id
                      for row in (data.get("data") or [])),
          str(data.get("data") or []))
    status, data = request("GET", "/api/employee/feedbacks", token=atok, params={
        "keyword": "QA 售后反馈",
    })
    check("后台售后服务列表可查询学生工单",
          ok(status, data) and any(r.get("id") in ticket_ids for r in data.get("data", [])),
          str(data.get("msg")))
    if advisor:
        status, data = request("GET", "/api/employee/feedbacks", token=advisor["token"])
        check("顾问可进入本人学生售后服务范围", ok(status, data), str(data.get("msg")))
    if ticket_ids:
        status, data = request("PUT", "/api/employee/resolve_feedback", {
            "token": atok, "ticket_id": ticket_ids[0], "solution": "QA 已补充资料",
        })
        check("解决售后并通知", ok(status, data), str(data.get("msg")))

    section("4. CRM、成绩与 NL2SQL")
    report_content = "QA日报" + stamp
    status, data = request("POST", "/api/employee/report", {
        "token": atok, "content": report_content,
    })
    check("员工提交日报并返回日报记录",
          ok(status, data)
          and data.get("data", {}).get("content") == report_content
          and bool(data.get("data", {}).get("id")),
          str(data.get("data") or data.get("msg")))
    status, data = request("GET", "/api/employee/reports", token=atok)
    check("日报列表返回员工姓名",
          ok(status, data)
          and any(row.get("content") == report_content and row.get("employee_name")
                  for row in data.get("data", [])),
          str(data.get("data", [])[:1]))

    lead_name = "QA线索" + stamp
    status, data = request("POST", "/api/employee/add_lead", {
        "token": atok, "customer_name": lead_name, "contact_info": "qa_lead@example.com",
        "background_info": "本科在读，意向德国",
    })
    check("录入客户并返回客户记录",
          ok(status, data)
          and data.get("data", {}).get("customer_name") == lead_name
          and data.get("data", {}).get("status") == "新增意向"
          and bool(data.get("data", {}).get("id")),
          str(data.get("data") or data.get("msg")))
    main_lead_id = int((data.get("data", {}) if ok(status, data) else {}).get("id") or 0)
    status, data = request("GET", "/api/lead/list", token=atok)
    if ok(status, data):
        for row in data.get("data", []):
            if row.get("customer_name") == lead_name:
                lead_ids.append(row["id"])
                main_lead_id = int(row["id"])
    check("客户线索可见", bool(lead_ids), f"ids={lead_ids}")
    if lead_ids:
        status, data = request("GET", f"/api/lead/{main_lead_id}", token=atok)
        check("客户详情接口可用",
              ok(status, data) and data.get("data", {}).get("id") == main_lead_id,
              str(data.get("msg")))

    status, data = request("POST", "/api/lead/submit", {
        "customer_name": "QA公开线索_" + stamp,
        "contact_info": "qa_public_lead@example.com",
        "background_info": "检查公开留资的负责人角色",
    })
    public_lead_id = data.get("data", {}).get("lead_id") if ok(status, data) else 0
    if public_lead_id:
        lead_ids.append(public_lead_id)
    owner_role = ""
    if public_lead_id:
        db_path = Path(os.getenv(
            "YUEJIAO_DB_FILE", str(Path(__file__).resolve().parent / "yuejiao.db")
        ))
        conn = sqlite3.connect(str(db_path))
        try:
            owner = conn.execute(
                "SELECT u.user_type,u.staff_role FROM crm_lead l "
                "JOIN sys_user u ON u.id=l.owner_employee_id WHERE l.id=?",
                (public_lead_id,),
            ).fetchone()
            owner_role = (owner[1] or owner[0]) if owner else ""
        finally:
            conn.close()
    check("公网线索只分配给有 CRM 权限的角色",
          ok(status, data) and owner_role in ("ADVISOR", "ADMIN", "LEADER"),
          f"owner_role={owner_role}")
    if lead_ids:
        status, data = request("POST", "/api/lead/follow-up", {
            "token": atok, "lead_id": main_lead_id, "status": "跟进中",
            "note": "QA 已电话联系",
        })
        check("CRM 合法状态流转", ok(status, data), str(data.get("msg")))
        status, data = request("GET", "/api/employee/search_advanced", token=atok, params={
            "keyword": lead_name, "status": "跟进中",
        })
        check("客户高级筛选支持关键词和状态组合",
              ok(status, data)
              and any(row.get("id") == main_lead_id for row in data.get("data", [])),
              str(data.get("msg")))
        status, data = request("POST", "/api/lead/follow-up", {
            "token": atok, "lead_id": main_lead_id, "status": "已联系",
            "note": "非法状态",
        })
        check("CRM 非法状态被拒", status == 400, f"HTTP {status}")

    status, data = request("POST", "/api/employee/nl2sql", {
        "token": atok, "command": "帮我查一下" + lead_name + "的跟进记录",
    })
    check("NL2SQL 客户查询",
          ok(status, data) and data.get("data", {}).get("data"),
          str(data.get("data", {}).get("msg")))
    status, data = request("POST", "/api/employee/nl2sql", {
        "token": atok, "command": "统计一下一共有多少客户",
    })
    check("NL2SQL 客户统计",
          ok(status, data) and data.get("data", {}).get("data"),
          str(data.get("data", {}).get("msg")))

    status, data = request("POST", "/api/employee/add_score", {
        "token": atok, "student_id": stu_id, "course_name": course_name,
        "score": 88.5, "semester": "2026秋",
    })
    check("员工录入成绩并返回成绩记录",
          ok(status, data)
          and data.get("data", {}).get("student_id") == stu_id
          and data.get("data", {}).get("course_name") == course_name
          and bool(data.get("data", {}).get("id")),
          str(data.get("data") or data.get("msg")))
    status, data = request("POST", "/api/content/student/country", {
        "token": stok, "country": "新加坡",
    })
    check("QA 学生设置意向国家", ok(status, data), str(data.get("msg")))
    status, data = request("GET", "/api/admin/scores", token=atok, params={
        "keyword": "QA 留学生", "country": "新加坡", "course_name": course_name,
    })
    score_rows = data.get("data", []) if ok(status, data) else []
    check("成绩查询返回学生姓名和国家并支持组合筛选",
          ok(status, data) and score_rows
          and score_rows[0].get("student_name") == "QA 留学生"
          and score_rows[0].get("target_country") == "新加坡",
          str(score_rows[:1]))
    status, data = request("GET", "/api/content/students/list", token=atok, params={
        "country": "新加坡", "keyword": "QA 留学生",
    })
    student_rows = data.get("data", []) if ok(status, data) else []
    check("学生名单支持国家和姓名组合筛选",
          ok(status, data) and any(row.get("id") == stu_id for row in student_rows),
          f"rows={len(student_rows)}")

    status, data = request(
        "GET", "/api/content/students-by-country", token=atok
    )
    directory_groups = data.get("data", []) if ok(status, data) else []
    directory_students = [
        student
        for group in directory_groups
        for student in group.get("students", [])
    ]
    directory_programs = {
        row.get("program") for row in directory_students
        if row.get("program") and row.get("program") != "未关联方案"
    }
    check("管理员学生档案可按国家与留学方案分支",
          ok(status, data)
          and len(directory_students) >= 19
          and len(directory_programs) >= 2
          and all("country_source" in row and "program_source" in row
                  for row in directory_students)
          and any(row.get("duration_pending") is True
                  for row in directory_students)
          and any(row.get("completeness_status") == "COMPLETE"
                  for row in directory_students),
          f"students={len(directory_students)}, programs={len(directory_programs)}")

    status, data = request("GET", "/api/student/my_scores", token=stok)
    check("学生查询本人成绩",
          ok(status, data) and any(r["course_name"] == course_name for r in data.get("data", [])))

    # 模拟员工和学生看到的连续两轮对话：第一轮确定学生与课程，第二轮只给
    # 新分数。必须真正 UPDATE，而不是让大模型返回一段“已更新”的假成功。
    status, data = request("POST", "/api/auth/dify-chat", {
        "token": atok,
        "query": "查询一下 QA 留学生的" + course_name + "成绩",
    })
    check("员工助手可查询学生具体课程成绩",
          ok(status, data) and course_name in data.get("data", {}).get("answer", "")
          and "|" in data.get("data", {}).get("answer", ""),
          str(data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "把成绩修改为 5.6 分",
        "action_type": "score_upsert", "user_action": "collect",
        "slots_json": json.dumps({
            "student_id": stu_id, "course_name": course_name, "score_value": 5.6,
        }, ensure_ascii=False),
    })
    first_update_prompt = data.get("data", {}).get("preview", "") if ok(status, data) else ""
    check("员工修改成绩先复述确认",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_CONFIRM",
          first_update_prompt or str(data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "确认", "action_type": "score_upsert",
        "user_action": "confirm", "slots_json": "{}",
    })
    ready_payload = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": atok,
        "confirmation_token": ready_payload.get("confirmation_token", ""),
    })
    check("员工确认后才执行成绩修改",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_EXECUTED",
          str(data.get("data") or data.get("msg")))
    status, data = request("GET", "/api/student/my_scores", token=stok)
    updated_rows = data.get("data", []) if ok(status, data) else []
    check("学生端立即读到助手修改后的成绩",
          ok(status, data)
          and any(r.get("course_name") == course_name
                  and abs(float(r.get("score", 0)) - 5.6) < 0.001
                  for r in updated_rows),
          str(updated_rows))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "QA 留学生的托福成绩更新为 6.5 分",
        "action_type": "score_upsert", "user_action": "collect",
        "slots_json": json.dumps({
            "student_id": stu_id, "course_name": "托福", "score_value": 6.5,
        }, ensure_ascii=False),
    })
    check("原无成绩时也先生成确认摘要",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_CONFIRM",
          str(data.get("data") or data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "确认", "action_type": "score_upsert",
        "user_action": "confirm", "slots_json": "{}",
    })
    ready_payload = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": atok,
        "confirmation_token": ready_payload.get("confirmation_token", ""),
    })
    check("确认后真实新建成绩",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_EXECUTED",
          str(data.get("data") or data.get("msg")))
    status, data = request("GET", "/api/student/my_scores", token=stok)
    check("学生端可读到助手新建的托福成绩",
          ok(status, data)
          and any(r.get("course_name") == "托福"
                  and abs(float(r.get("score", 0)) - 6.5) < 0.001
                  for r in data.get("data", [])),
          str(data.get("data", [])))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "删除 QA 留学生的第一次成绩",
        "action_type": "score_delete", "user_action": "collect",
        "slots_json": json.dumps({
            "student_id": stu_id, "course_name": course_name,
        }, ensure_ascii=False),
    })
    delete_prompt = data.get("data", {}).get("preview", "") if ok(status, data) else ""
    check("删除成绩先确认目标记录",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_CONFIRM",
          delete_prompt or str(data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "确认", "action_type": "score_delete",
        "user_action": "confirm", "slots_json": "{}",
    })
    ready_payload = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": atok,
        "confirmation_token": ready_payload.get("confirmation_token", ""),
    })
    check("确认后删除第一次成绩",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_EXECUTED",
          str(data.get("data") or data.get("msg")))
    status, data = request("GET", "/api/student/my_scores", token=stok)
    after_first_delete = data.get("data", []) if ok(status, data) else []
    check("第一次成绩被正确删除且其他成绩保留",
          ok(status, data)
          and not any(r.get("course_name") == course_name
                      for r in after_first_delete)
          and any(r.get("course_name") == "托福"
                  for r in after_first_delete),
          str(after_first_delete))

    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "给 QA 留学生录入雅思 6.7 分",
        "action_type": "score_upsert", "user_action": "collect",
        "slots_json": json.dumps({
            "student_id": stu_id, "course_name": "雅思", "score_value": 6.7,
        }, ensure_ascii=False),
    })
    check("成绩录入支持“雅思 6.7”并先确认",
          ok(status, data)
          and "雅思" in data.get("data", {}).get("preview", "")
          and data.get("data", {}).get("status_token") == "STATE_CONFIRM",
          str(data.get("data") or data.get("msg")))
    status, data = request("POST", "/api/actions/prepare", {
        "token": atok, "query": "确认", "action_type": "score_upsert",
        "user_action": "confirm", "slots_json": "{}",
    })
    ready_payload = data.get("data", {}) if ok(status, data) else {}
    status, data = request("POST", "/api/actions/execute", {
        "token": atok,
        "confirmation_token": ready_payload.get("confirmation_token", ""),
    })
    check("确认后录入雅思成绩",
          ok(status, data)
          and data.get("data", {}).get("status_token") == "STATE_EXECUTED",
          str(data.get("data") or data.get("msg")))
    check("成绩写法解析兼容中文逗号", _new_score_value("雅思，6.7") == 6.7)

    section("4.1 班主任员工助手确定性问答")
    homeroom_token = homeroom["token"] if homeroom else ""
    homeroom_queries = []

    status, data = request("POST", "/api/auth/dify-chat", {
        "token": atok, "query": "查看所有学生的情况",
    })
    admin_all_students = data.get("data", {}).get("answer", "") if ok(status, data) else ""
    check("管理员助手可查询全量学生信息和账号",
          ok(status, data)
          and "你可查看的学生共" in admin_all_students
          and "林子涵" in admin_all_students
          and "student01" in admin_all_students
          and "顾问" in admin_all_students
          and "|" in admin_all_students
          and "没有找到" not in admin_all_students,
          admin_all_students[:500] or str(data.get("msg")))

    status, data = request("POST", "/api/auth/dify-chat", {
        "token": atok, "query": "查看所有学生成绩情况",
    })
    all_student_scores = data.get("data", {}) if ok(status, data) else {}
    check("全量学生名单规则不会误吞成绩查询",
          ok(status, data)
          and all_student_scores.get("intent") != "student_list"
          and "可查询" in all_student_scores.get("answer", "")
          and "学生姓名" in all_student_scores.get("answer", ""),
          str(all_student_scores.get("answer") or data.get("msg")))

    def homeroom_chat(q):
        _, payload = request("POST", "/api/auth/dify-chat", {
            "token": homeroom_token,
            "query": q,
            "history": "\n".join(homeroom_queries[-6:]),
        })
        homeroom_queries.append(q)
        return payload.get("data", {}) if ok(_, payload) else {}

    directory_answer = homeroom_chat("现在公司有哪些班主任").get("answer", "")
    check("员工助手可查询在职班主任名单",
          "林班主任" in directory_answer and "黄班主任" in directory_answer
          and "|" in directory_answer,
          directory_answer)
    capability_answer = homeroom_chat("你能查询哪些东西").get("answer", "")
    check("班主任助手能力说明按真实角色生成",
          "本人班级" in capability_answer and "客户线索" not in capability_answer,
          capability_answer)
    performance_answer = homeroom_chat("汇总业绩吧").get("answer", "")
    check("班主任问业绩时返回带班运营汇总而不是泛化失败",
          "不计算销售客户业绩" in performance_answer
          and "学生数" in performance_answer and "|" in performance_answer,
          performance_answer)
    customer_answer = homeroom_chat("客户状态情况").get("answer", "")
    check("班主任查客户状态时明确解释岗位权限边界",
          "不承担 CRM" in customer_answer,
          customer_answer)
    score_picker_answer = homeroom_chat("查成绩").get("answer", "")
    check("班主任只说查成绩时列出可查询学生",
          "林子涵" in score_picker_answer
          and "|" in score_picker_answer
          and ("请回复学生姓名" in score_picker_answer
               or "请选择要查询成绩的学生" in score_picker_answer
               or "请选择要操作的学生" in score_picker_answer),
          score_picker_answer)
    all_students_answer = homeroom_chat("所有学生").get("answer", "")
    check("班主任可查询本人全部学生",
          "林子涵" in all_students_answer and "陈雨桐" in all_students_answer
          and "|" in all_students_answer,
          all_students_answer)
    student_profile_answer = homeroom_chat("林子涵").get("answer", "")
    check("班主任输入学生姓名返回档案和真实成绩",
          "现有成绩" in student_profile_answer
          and "学术英语" in student_profile_answer
          and "|" in student_profile_answer,
          student_profile_answer)
    class_answer = homeroom_chat("我负责的是哪个班级").get("answer", "")
    check("班主任可查询本人带班范围且不沿用上一条成绩",
          "班主任归属关系" in class_answer and "林子涵" in class_answer,
          class_answer)
    punctuation_answer = homeroom_chat("？").get("answer", "")
    check("单问号不会重复上一条成绩而是请求澄清",
          "没有具体问题" in punctuation_answer and "学术英语" not in punctuation_answer,
          punctuation_answer)
    add_prompt_answer = homeroom_chat("帮我添加成绩").get("answer", "")
    check("添加成绩会话能结合上一名学生继续追问分数",
          "分数" in add_prompt_answer
          and ("课程" in add_prompt_answer or "学术英语" in add_prompt_answer),
          add_prompt_answer)

    section("5. 活动、申请与推荐")
    status, data = request("GET", "/api/content/events")
    initial_events = data.get("data", []) if ok(status, data) else []
    requested_event_names = {
        "新加坡招生官见面会", "德国双元制宣讲会", "2026秋季多国名校联展",
    }
    current_event_names = {row.get("event_name") for row in initial_events}
    check("用户指定的三场活动已发布",
          ok(status, data)
          and requested_event_names.issubset(current_event_names),
          f"events={sorted(current_event_names)}")
    check("匿名活动列表只包含对外活动",
          ok(status, data)
          and all(row.get("audience_scope") in ("PUBLIC", "ALL")
                  for row in initial_events)
          and not any(row.get("audience_scope") in ("STUDENT", "EMPLOYEE")
                      for row in initial_events),
          str([(row.get("event_name"), row.get("audience_scope"))
               for row in initial_events]))
    status, data = request("GET", "/api/content/events", token=stok)
    student_events = data.get("data", []) if ok(status, data) else []
    check("学生活动列表包含学生专属活动且不含员工内部活动",
          ok(status, data)
          and any(row.get("audience_scope") == "STUDENT" for row in student_events)
          and not any(row.get("audience_scope") == "EMPLOYEE" for row in student_events),
          str([(row.get("event_name"), row.get("audience_scope"))
               for row in student_events]))
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "学校里面有什么活动",
        "visitor": "scope_school_" + stamp,
    })
    school_answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
    check("学校活动查询经过 Dify 且不泄露员工内部活动",
          ok(status, data)
          and (data.get("data") or {}).get("orchestrator") == "dify"
          and "新加坡招生官见面会" in school_answer
          and "|" in school_answer
          and "员工招生业务内训" not in school_answer,
          school_answer)
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "公司有什么活动",
        "visitor": "scope_company_" + stamp,
    })
    company_answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
    check("公司公开活动不包含学生专属和员工内部活动",
          ok(status, data)
          and (data.get("data") or {}).get("orchestrator") == "dify"
          and "新加坡招生官见面会" in company_answer
          and "|" in company_answer
          and "留学生校园经验分享会" not in company_answer
          and "员工招生业务内训" not in company_answer,
          company_answer)
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "员工内部活动",
        "visitor": "scope_employee_" + stamp,
    })
    internal_answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
    check("公开客服不泄露员工内部活动",
          ok(status, data)
          and (data.get("data") or {}).get("orchestrator") == "dify"
          and "员工招生业务内训" not in internal_answer,
          internal_answer)

    event_visitor = "qa_event_visitor_" + stamp
    event_phone = "139" + stamp[-8:]
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "最近有什么活动可以参加？",
        "visitor": event_visitor,
    })
    check("客服活动列表经过 Dify 并返回当前可报名活动",
          ok(status, data)
          and (data.get("data") or {}).get("orchestrator") == "dify"
          and "新加坡招生官见面会" in data.get("data", {}).get("answer", "")
          and "|" in data.get("data", {}).get("answer", ""),
          str(data.get("data", {}).get("answer") or data.get("msg")))
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "帮我报名1",
        "visitor": event_visitor,
    })
    visitor_pick_payload = (data.get("data") or {}) if isinstance(data, dict) else {}
    check("访客回复报名1会选定活动并要求联系方式",
          ok(status, data)
          and "姓名" in visitor_pick_payload.get("answer", "")
          and (
              "手机号" in visitor_pick_payload.get("answer", "")
              or "联系方式" in visitor_pick_payload.get("answer", "")
          ),
          str(visitor_pick_payload.get("answer") or data.get("msg")))
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "张三 " + event_phone,
        "visitor": event_visitor,
    })
    visitor_payload = (data.get("data") or {}) if isinstance(data, dict) else {}
    visitor_preview_answer = visitor_payload.get("answer", "") if ok(status, data) else ""
    check("访客补充姓名手机号后先复述确认且不写库",
          ok(status, data)
          and visitor_payload.get("changed") is not True
          and "确认" in visitor_preview_answer,
          visitor_preview_answer or str(data.get("msg")))
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "确认",
        "visitor": event_visitor,
    })
    check("访客确认后真实报名",
          ok(status, data)
          and data.get("data", {}).get("changed") is True
          and "报名" in data.get("data", {}).get("answer", "")
          and "成功" in data.get("data", {}).get("answer", ""),
          str(data.get("data", {}).get("answer") or data.get("msg")))
    check("活动报名对话返回报名记录ID",
          ok(status, data) and bool(data.get("data", {}).get("registration_id")),
          str(data.get("data")))
    registration_contacts.append(event_phone)
    db_path = Path(os.getenv(
        "YUEJIAO_DB_FILE", str(Path(__file__).resolve().parent / "yuejiao.db")
    ))
    conn = sqlite3.connect(str(db_path))
    try:
        saved_registration = conn.execute(
            "SELECT event_id,contact_info,lead_id FROM event_registration "
            "WHERE contact_info=?", (event_phone,)
        ).fetchone()
        event_lead = None
        if saved_registration and saved_registration[2]:
            event_lead = conn.execute(
                "SELECT id,owner_employee_id,status,source_event_id,"
                "       background_info,follow_up_history "
                "FROM crm_lead WHERE id=?",
                (saved_registration[2],),
            ).fetchone()
    finally:
        conn.close()
    check("报名1最终写入event_registration",
          bool(saved_registration)
          and saved_registration[1] == event_phone,
          str(saved_registration))
    check("活动报名自动生成并关联CRM线索",
          bool(saved_registration)
          and bool(saved_registration[2])
          and bool(event_lead)
          and event_lead[2] == "新增意向"
          and "活动报名" in (event_lead[4] or "")
          and "活动报名留资" in (event_lead[5] or ""),
          f"registration={saved_registration}, lead={event_lead}")
    if saved_registration and event_lead:
        owner_token = ""
        if advisor and event_lead[1] == advisor.get("user_id"):
            owner_token = advisor["token"]
        elif advisor2 and event_lead[1] == advisor2.get("user_id"):
            owner_token = advisor2["token"]
        status, data = request(
            "GET", "/api/content/events/registrations",
            token=owner_token, params={"event_id": saved_registration[0]},
        )
        matching_signup = next(
            (row for row in data.get("data", [])
             if row.get("contact_info") == event_phone),
            {},
        ) if ok(status, data) else {}
        check("顾问可在活动板块看到本人线索的访客报名",
              ok(status, data)
              and matching_signup.get("lead_id") == event_lead[0]
              and matching_signup.get("lead_owner_id") == event_lead[1],
              str(matching_signup or data.get("msg")))

    status, data = request("POST", "/api/content/events/save", {
        "token": atok, "event_name": "QA活动" + stamp,
        "event_type": "宣讲会",
        "start_time": "2026-12-10", "location": "QA测试地点",
        "max_participants": 1, "description": "自动化测试活动",
    })
    check("创建测试活动", ok(status, data), str(data.get("msg")))
    status, data = request("GET", "/api/content/events")
    created_event = None
    if ok(status, data):
        for row in data.get("data", []):
            if row.get("event_name") == "QA活动" + stamp:
                event_ids.append(row["id"])
                created_event = row
    check("活动创建字段顺序正确",
          bool(created_event)
          and created_event.get("event_type") == "宣讲会"
          and created_event.get("start_time") == "2026-12-10"
          and created_event.get("location") == "QA测试地点",
          str(created_event))
    if event_ids:
        status, data = request("GET", f"/api/content/events/{event_ids[0]}")
        check("活动详情接口可用",
              ok(status, data) and data.get("data", {}).get("id") == event_ids[0],
              str(data.get("msg")))
    status, data = request("POST", "/api/content/events/save", {
        "token": atok, "event_name": "QA员工内部活动" + stamp,
        "event_type": "内部培训", "audience_scope": "EMPLOYEE",
        "start_time": "2026-12-11", "location": "公司会议室",
        "max_participants": 10, "description": "权限测试活动",
    })
    check("创建员工内部测试活动", ok(status, data), str(data.get("msg")))
    status, data = request("GET", "/api/content/events", token=atok)
    if ok(status, data):
        for row in data.get("data", []):
            if row.get("event_name") == "QA员工内部活动" + stamp:
                event_ids.append(row["id"])
    if event_ids:
        status, data = request("POST", "/api/content/events/register", {
            "event_id": event_ids[-1], "customer_name": "QA访客",
            "contact_info": "qa_scope_guest_" + stamp + "@example.com",
        })
        check("匿名访客不能报名员工内部活动", status == 403, f"HTTP {status}")
        status, data = request("POST", "/api/auth/public-chat", {
            "query": "最近有什么活动可以参加？",
            "visitor": "qa_activity_sync_" + stamp,
        }, timeout=25)
        check("后台新增活动后客服提问立即同步",
              ok(status, data)
              and (data.get("data") or {}).get("orchestrator") == "dify"
              and ("QA活动" + stamp) in data.get("data", {}).get("answer", ""),
              str(data.get("data", {}).get("answer", ""))[:200])
        status, data = request("POST", "/api/auth/public-chat", {
            "query": "我要报名留学分享会",
            "visitor": "qa_activity_register_" + stamp,
        }, timeout=25)
        answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
        check("活动报名意图查询当前可报名活动而不是内部知识库",
              ok(status, data)
              and (data.get("data") or {}).get("orchestrator") == "dify"
              and ("新加坡招生官见面会" in answer or "2026秋季多国名校联展" in answer)
              and "王建国" not in answer,
              answer[:200])
    if event_ids:
        if homeroom:
            status, _ = request("POST", "/api/content/events/delete", {
                "token": homeroom["token"], "event_id": event_ids[0],
            })
            check("班主任不能删除活动", status == 403, f"HTTP {status}")
        status, data = request("POST", "/api/content/events/register", {
            "event_id": event_ids[0], "token": stok,
        })
        check("学生活动报名", ok(status, data), str(data.get("msg")))
        status, data = request("POST", "/api/content/events/register", {
            "event_id": event_ids[0], "token": stok,
        })
        check("重复报名被拒", status == 409, f"HTTP {status}")

    concurrent_event_name = "QA并发活动" + stamp
    status, data = request("POST", "/api/content/events/save", {
        "token": atok, "event_name": concurrent_event_name,
        "start_time": "2026-12-11", "location": "QA并发测试",
        "max_participants": 1, "description": "验证防超卖",
    })
    status, data = request("GET", "/api/content/events")
    concurrent_event_id = 0
    if ok(status, data):
        for row in data.get("data", []):
            if row.get("event_name") == concurrent_event_name:
                concurrent_event_id = row["id"]
                event_ids.append(row["id"])
    if concurrent_event_id:
        def register_guest(index):
            return request("POST", "/api/content/events/register", {
                "event_id": concurrent_event_id,
                "customer_name": "QA并发" + str(index),
                "contact_info": f"qa_concurrent_{stamp}_{index}@example.com",
            })
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(register_guest, range(4)))
        successes = sum(1 for code, _ in results if code == 200)
        rejected = sum(1 for code, _ in results if code == 400)
        check("并发报名不超卖", successes == 1 and rejected == 3,
              f"success={successes}, rejected={rejected}")

    status, data = request("POST", "/api/content/students/stage", {
        "token": atok, "student_id": stu_id, "stage": "APPLYING",
        "note": "QA 申请流程测试：从临时在读回退到申请期", "force": True,
    })
    check("管理员带原因回退到申请期", ok(status, data), str(data.get("msg")))
    status, data = request("POST", "/api/application/create", {
        "token": atok, "student_id": stu_id, "country": "德国",
        "university": "QA大学", "program": "机械工程",
    })
    check("创建申请进度", ok(status, data), str(data.get("msg")))
    if ok(status, data):
        app_ids.append(data["data"]["application_id"])
        advance_prepare, _, advance_exec = confirmed_action(
            atok, "推进申请", "application_advance",
            {
                "application_id": app_ids[0],
                "note": "QA 通用动作推进",
            },
        )
        check("员工推进申请阶段先预览确认再写库",
              advance_prepare.get("status_token") == "STATE_CONFIRM"
              and advance_exec.get("status_token") == "STATE_EXECUTED"
              and advance_exec.get("changed") is True
              and advance_exec.get("stage") == "材料准备",
              str(advance_exec or advance_prepare))
        status, data = request("POST", "/api/application/advance", {
            "token": atok, "application_id": app_ids[0],
            "stage": "已入学", "note": "不应允许跨阶段跳跃",
        })
        check("申请不能跳过中间阶段", status == 400, f"HTTP {status}")
    status, data = request("GET", "/api/application/my", token=stok)
    check("学生查看申请时间轴", ok(status, data) and bool(data.get("data")))

    status, data = request("POST", "/api/recommend/recommend", {
        "raw_text": "19岁，高中刚毕业，想继续读本科",
    })
    rec = data.get("data", {}) if ok(status, data) else {}
    check("画像推荐逐项评分",
          ok(status, data) and rec.get("best_match")
          and rec["best_match"].get("score") is not None
          and "reason" in rec["best_match"],
          str(rec.get("summary", "")))
    recommendation_answer = _direct_recommendation_answer(
        "19岁，高中刚毕业，想快点拿本科学历", "customer"
    )
    check("推荐回复包含理由、适合人群和知识库方案要点",
          "0.5/1+2" in recommendation_answer
          and "适合人群" in recommendation_answer
          and "方案要点" in recommendation_answer,
          recommendation_answer)
    phd_answer = _direct_recommendation_answer(
        "我是博士，40岁，有哪些项目适合我", "customer"
    )
    check("博士无匹配时给出自然转场且不输出0分方案",
          "博士" in phd_answer
          and "不会硬推" in phd_answer
          and "职业转型" in phd_answer
          and "匹配度 0" not in phd_answer
          and "暂无匹配项目" not in phd_answer,
          phd_answer)
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "我是博士，40岁，有哪些项目适合我",
        "visitor": "qa_phd_tone_" + stamp,
    }, timeout=30)
    public_phd_answer = (
        (data.get("data") or {}).get("answer", "") if ok(status, data) else ""
    )
    check("客服前端对博士画像不再返回机械零匹配话术",
          ok(status, data)
          and (data.get("data") or {}).get("orchestrator") == "dify"
          and ("暂未" in public_phd_answer or "没有" in public_phd_answer)
          and "匹配度 0" not in public_phd_answer
          and "暂无匹配项目" not in public_phd_answer
          and "知识库" not in public_phd_answer,
          public_phd_answer or str(data.get("msg")))
    status, data = request("POST", "/api/auth/public-chat", {
        "query": "我是博士，今年40岁，",
        "visitor": "qa_profile_only_" + stamp,
    }, timeout=30)
    profile_only_answer = (
        (data.get("data") or {}).get("answer", "") if ok(status, data) else ""
    )
    check("只提供博士年龄时先追问目标而不主动扩展签证政策",
          ok(status, data)
          and (data.get("data") or {}).get("orchestrator") == "dify"
          and ("目标" in profile_only_answer or "诉求" in profile_only_answer)
          and "机会卡" not in profile_only_answer
          and "欧盟蓝卡" not in profile_only_answer
          and "知识库" not in profile_only_answer,
          profile_only_answer or str(data.get("msg")))
    status, data = request("POST", "/api/recommend/recommend", {
        "raw_text": "19岁，高中毕业，想继续升学",
        "age": 23,
        "education": "本科",
        "intent": "升学",
    })
    structured = data.get("data", {}) if ok(status, data) else {}
    structured_best = structured.get("best_match") or {}
    check("结构化画像字段优先于raw_text正则",
          ok(status, data)
          and structured.get("profile", {}).get("age") == 23
          and structured.get("profile", {}).get("education") == "本科"
          and structured_best.get("project") == "一年制本升硕",
          str(structured))
    status, data = request("POST", "/api/recommend/recommend", {
        "raw_text": "我今年23岁，本科毕业，想就业，有德语B1，动手能力强",
    })
    employment_rec = data.get("data", {}) if ok(status, data) else {}
    employment_best = employment_rec.get("best_match") or {}
    check("本科就业意向直接推荐双元制而非本升硕",
          ok(status, data)
          and "中德" in employment_best.get("project", "")
          and "本升硕" not in employment_best.get("project", "")
          and not employment_best.get("caveats"),
          str(employment_best))
    status, data = request("POST", "/api/recommend/recommend", {
        "raw_text": "我今年23岁，本科毕业，想就业，没有德语基础",
    })
    no_german = (data.get("data", {}).get("best_match") or {}) if ok(status, data) else {}
    check("没有德语基础不能匹配德国双元制",
          ok(status, data) and "中德" not in no_german.get("project", ""),
          str(no_german))

    profile_cases = [
        ("14岁，初中毕业，想继续升学", "2+2 新加坡本科班"),
        ("18岁，高中毕业，想继续升学", "0.5/1+2 新加坡本科班"),
        ("18岁，中职毕业，想快速就业", "6+6 酒店运营大专就业班"),
        ("22岁，专科毕业，想升本科", "一年制专升本"),
        ("23岁，本科毕业，想读硕士", "一年制本升硕"),
    ]
    for raw_text, expected_project in profile_cases:
        status, data = request("POST", "/api/recommend/recommend", {
            "raw_text": raw_text,
        })
        best = (data.get("data", {}).get("best_match") or {}) if ok(status, data) else {}
        check("知识库画像规则：" + raw_text,
              ok(status, data) and best.get("project") == expected_project,
              str(best))
    for raw_text in (
        "我儿子今年19岁，高中毕业，想快速本科毕业",
        "我家孩子 19 岁、高中刚毕业、想快点拿本科学历",
    ):
        status, data = request("POST", "/api/recommend/recommend", {
            "raw_text": raw_text,
        })
        best = (data.get("data", {}).get("best_match") or {}) if ok(status, data) else {}
        check("目标学历不能覆盖当前高中学历：" + raw_text,
              ok(status, data)
              and best.get("project") == "0.5/1+2 新加坡本科班"
              and "本升硕" not in best.get("project", ""),
              str(best))
    status, data = request("GET", "/api/content/programs")
    public_programs = data.get("data", []) if ok(status, data) else []
    countries = {r.get("country") for r in public_programs}
    check("公开项目仅展示知识库已确认国家",
          ok(status, data) and countries and countries <= {"德国", "新加坡"},
          f"countries={sorted(countries)}")
    check("公开项目来源精确到知识库文档",
          bool(public_programs) and all(
              r.get("source_document")
              and r.get("source_document") != "ai知识库"
              for r in public_programs
          ),
          f"sources={sorted({r.get('source_document') for r in public_programs})}")
    status, data = request("GET", "/api/content/company")
    company_data = data.get("data", {}) if ok(status, data) else {}
    company_structured = company_data.get("structured", company_data)
    check("公司公开内容带知识库来源",
          ok(status, data)
          and company_structured.get("contact", {}).get("phone", {}).get("source_document")
          and company_data.get("source_mode") in ("DIFY_LIVE", "CACHE_FALLBACK"),
          str(data.get("msg")))

    # 通用动作链路必须也能覆盖学生主动申请，而不是只在页面按钮上可用。
    sg_program = next(
        (row for row in option_programs if row.get("country") == "新加坡"), {}
    )
    de_program = next(
        (row for row in option_programs if row.get("country") == "德国"), {}
    )
    app_action_user = "qa_app_action_" + stamp
    status, data = request("POST", "/api/auth/register", {
        "username": app_action_user,
        "password": "Qa!appaction2026",
        "real_name": "QA 申请动作学生",
        "security_question": "测试问题",
        "security_answer": "测试答案",
    })
    if ok(status, data):
        app_action_token = data["data"]["token"]
        app_action_uid = data["data"]["user_id"]
        qa_uids.append(app_action_uid)
        create_prepare, _, create_exec = confirmed_action(
            app_action_token, "我要提交就读申请", "application_create",
            {
                "country": sg_program.get("country", ""),
                "application_mode": sg_program.get("category", ""),
                "program": sg_program.get("project_name", ""),
                "remark": "QA 通用动作主动申请",
            },
        )
        action_app_id = int(create_exec.get("application_id") or 0)
        if action_app_id:
            app_ids.append(action_app_id)
        check("学生就读申请先确认再正式提交",
              create_prepare.get("status_token") == "STATE_CONFIRM"
              and create_exec.get("status_token") == "STATE_EXECUTED"
              and action_app_id > 0,
              str(create_exec or create_prepare))
        update_prepare, _, update_exec = confirmed_action(
            app_action_token, "我要修改申请", "application_update",
            {
                "application_id": action_app_id,
                "country": de_program.get("country", ""),
                "application_mode": de_program.get("category", ""),
                "program": de_program.get("project_name", ""),
                "university": "QA 申请动作大学",
                "remark": "QA 通用动作修改申请",
            },
        )
        check("学生待受理申请先确认再修改",
              update_prepare.get("status_token") == "STATE_CONFIRM"
              and update_exec.get("status_token") == "STATE_EXECUTED"
              and update_exec.get("changed") is True,
              str(update_exec or update_prepare))
        cancel_prepare, _, cancel_exec = confirmed_action(
            app_action_token, "我要撤销申请", "application_cancel",
            {"application_id": action_app_id},
        )
        check("学生待受理申请先确认再撤销",
              cancel_prepare.get("status_token") == "STATE_CONFIRM"
              and cancel_exec.get("status_token") == "STATE_EXECUTED"
              and cancel_exec.get("changed") is True,
              str(cancel_exec or cancel_prepare))
        status, data = request("GET", "/api/application/my", token=app_action_token)
        cancelled_app = next(
            (row for row in data.get("data", [])
             if row.get("id") == action_app_id),
            {},
        ) if ok(status, data) else {}
        check("撤销后申请状态为已撤销且不再参与推进",
              cancelled_app.get("status") == "已撤销",
              str(cancelled_app))

    status, data = request("POST", "/api/application/apply", {
        "token": stok, "country": "新加坡", "application_mode": "本科",
        "program": "0.5/1+2 新加坡本科班", "remark": "QA 学生主动申请",
    })
    check("学生主动发起就读申请", ok(status, data), str(data.get("msg")))
    if ok(status, data):
        student_app_id = data["data"]["application_id"]
        app_ids.append(student_app_id)

        status, data = request(
            "POST",
            f"/api/application/{student_app_id}/materials/checklist",
            {
                "token": atok,
                "stage": "选校定位",
                "material_name": "QA 当前阶段材料",
                "required_flag": True,
                "note": "请学生上传当前阶段说明材料",
                "visible_to_student": True,
            },
        )
        qa_archive_material_id = data.get("data", {}).get("material_id", 0) if ok(status, data) else 0
        check("管理员可创建当前阶段材料清单",
              ok(status, data) and qa_archive_material_id > 0,
              str(data.get("msg")))

        status, data = request(
            "POST",
            f"/api/application/{student_app_id}/materials/upload",
            {
                "token": stok,
                "stage": "选校定位",
                "material_id": qa_archive_material_id,
                "material_name": "QA 当前阶段材料",
                "file_name": qa_archive_file_name,
                "content_base64": base64.b64encode(qa_archive_file_bytes).decode("ascii"),
                "note": "QA 当前阶段补交",
            },
        )
        check("学生可向当前申请阶段补交材料附件",
              ok(status, data) and data.get("data", {}).get("version") == 1,
              str(data.get("msg")))

        status, data = request("POST", "/api/application/update", {
            "token": stok, "application_id": student_app_id,
            "country": "新加坡", "application_mode": "本科",
            "program": "2+2 新加坡本科班", "remark": "QA 修改后的申请",
        })
        check("学生修改待受理申请", ok(status, data), str(data.get("msg")))
        status, data = request("POST", "/api/application/cancel", {
            "token": stok, "application_id": student_app_id,
        })
        check("学生撤销待受理申请", ok(status, data), str(data.get("msg")))
        status, data = request("POST", "/api/application/advance", {
            "token": atok, "application_id": student_app_id, "note": "不应成功",
        })
        check("已撤销申请不能继续推进", status == 400, f"HTTP {status}")
        status, data = request("POST", "/api/content/students/stage", {
            "token": atok, "student_id": stu_id, "stage": "ENROLLED",
            "note": "QA 验证入学与申请同步",
        })
        check("后台设为在读", ok(status, data), str(data.get("msg")))
        status, data = request("GET", "/api/application/my", token=stok)
        completed = [a for a in data.get("data", []) if a.get("id") == app_ids[0]] if ok(status, data) else []
        completed_stages = {
            item.get("stage") for item in (completed[0].get("history", []) if completed else [])
        }
        expected_stages = {
            "选校定位", "材料准备", "递交申请", "面试/补充",
            "收到Offer", "办理签证", "行前准备", "已入学",
        }
        check("已入学学生申请自动完成",
              bool(completed) and completed[0].get("stage") == "已入学"
              and completed[0].get("status") == "已完成"
              and completed[0].get("progress_pct") == 100
              and expected_stages.issubset(completed_stages),
              str(completed[0] if completed else data))

        archive_app_id = app_ids[0]
        status, data = request(
            "GET", f"/api/application/{archive_app_id}/detail", token=stok
        )
        archive = data.get("data", {}) if ok(status, data) else {}
        check("学生可打开已完成申请的完整阶段档案",
              ok(status, data)
              and len(archive.get("stage_records", [])) == len(expected_stages)
              and archive.get("permissions", {}).get("is_student") is True,
              f"stages={len(archive.get('stage_records', []))}")

        status, data = request(
            "POST",
            f"/api/application/{archive_app_id}/stages/"
            + urllib.parse.quote("材料准备") + "/detail",
            {
                "token": atok,
                "summary": "QA 材料清单已核验，护照和学历材料已归档",
                "fields": {
                    "checklist_summary": "护照、学历证明、语言成绩",
                    "missing_items": "无",
                    "deadline": "2026-12-01",
                },
            },
        )
        material_stage = next(
            (row for row in data.get("data", {}).get("stage_records", [])
             if row.get("stage") == "材料准备"),
            {},
        ) if ok(status, data) else {}
        check("管理员可保存结构化阶段明细",
              ok(status, data)
              and material_stage.get("summary") == "QA 材料清单已核验，护照和学历材料已归档"
              and material_stage.get("detail", {}).get("missing_items") == "无",
              str(material_stage or data.get("msg")))

        status, data = request(
            "POST",
            f"/api/application/{archive_app_id}/materials/checklist",
            {
                "token": atok,
                "stage": "材料准备",
                "material_name": "QA 护照信息页",
                "required_flag": True,
                "note": "请上传清晰彩色扫描件",
                "visible_to_student": True,
            },
        )
        history_material_id = data.get("data", {}).get("material_id", 0) if ok(status, data) else 0
        check("管理员可为历史阶段添加材料清单",
              ok(status, data) and history_material_id > 0,
              str(data.get("msg")))

        status, data = request(
            "POST",
            f"/api/application/{archive_app_id}/materials/upload",
            {
                "token": atok,
                "stage": "材料准备",
                "material_id": history_material_id,
                "material_name": "QA 护照信息页",
                "file_name": qa_archive_file_name,
                "content_base64": base64.b64encode(qa_archive_file_bytes).decode("ascii"),
                "note": "QA 员工补录历史阶段材料",
            },
        )
        check("员工可为历史阶段补录材料附件",
              ok(status, data) and data.get("data", {}).get("version") == 1,
              str(data.get("msg")))

        status, data = request(
            "POST",
            f"/api/application/{archive_app_id}/materials/upload",
            {
                "token": stok,
                "stage": "材料准备",
                "file_name": "qa_student_history_new.txt",
                "content_base64": base64.b64encode(b"not allowed").decode("ascii"),
                "note": "学生不能自行向已完成历史阶段新增材料",
            },
        )
        check("学生不能向已完成的历史阶段直接补交材料",
              status == 400,
              str(data.get("msg")))

        status, data = request(
            "POST",
            f"/api/application/{archive_app_id}/materials/checklist",
            {
                "token": atok,
                "stage": "材料准备",
                "material_name": "QA 内部审核清单",
                "required_flag": False,
                "note": "仅员工可见",
                "visible_to_student": False,
            },
        )
        hidden_material_id = data.get("data", {}).get("material_id", 0) if ok(status, data) else 0
        check("管理员可创建内部材料清单",
              ok(status, data) and hidden_material_id > 0,
              str(data.get("msg")))

        status, data = request(
            "GET", f"/api/application/{archive_app_id}/detail", token=stok
        )
        student_materials = data.get("data", {}).get("materials", []) if ok(status, data) else []
        uploaded_material = next(
            (item for item in student_materials if item.get("id") == history_material_id), {}
        )
        check("学生档案显示附件但隐藏内部材料",
              ok(status, data)
              and uploaded_material.get("status") == "待审核"
              and uploaded_material.get("file_name") == qa_archive_file_name
              and all(item.get("id") != hidden_material_id for item in student_materials),
              str(student_materials))

        status, content_type, downloaded = request_raw(
            f"/api/application/materials/{history_material_id}/download", token=stok
        )
        check("学生可下载本人申请下的已归档材料",
              status == 200 and downloaded == qa_archive_file_bytes,
              f"HTTP {status}, {len(downloaded)} bytes")

        status, data = request(
            "POST",
            f"/api/application/materials/{history_material_id}/review",
            {
                "token": atok,
                "status": "已通过",
                "review_note": "QA 审核通过",
            },
        )
        check("管理员可审核申请材料并留存意见",
              ok(status, data), str(data.get("msg")))

        status, data = request("GET", "/api/employee/feedbacks", token=atok, params={
            "keyword": "QA 售后反馈",
        })
        feedback_row = next((
            row for row in data.get("data", [])
            if row.get("id") in ticket_ids
        ), {}) if ok(status, data) else {}
        check("售后展示申请档案中的就读国家而不是意向国家",
              feedback_row.get("study_country") == "德国"
              and feedback_row.get("intended_country") == "新加坡",
              str(feedback_row))

    status, data = request(
        "POST", f"/api/lifecycle/{stu_id}/enrollment", {
            "token": atok,
            "enrollment_date": "2026-11-20",
            "duration_months": 48,
            "duration_text": "QA 四年制培养计划",
            "note": "QA 自动化测试补录培养计划",
        },
    )
    lifecycle_enrollment = data.get("data", {}).get("enrollment", {}) if ok(status, data) else {}
    check("员工可补录学制并自动计算预计毕业日期",
          ok(status, data)
          and lifecycle_enrollment.get("expected_graduation_date") == "2030-11-20"
          and lifecycle_enrollment.get("completeness_status") == "COMPLETE",
          str(lifecycle_enrollment or data.get("msg")))
    status, data = request("POST", "/api/lifecycle/graduation/request", {
        "token": stok,
        "graduation_date": "2030-11-20",
        "completion_summary": "QA 已完成培养计划全部课程和结业要求",
        "document_ref": "QA-CERT-001",
        "student_note": "申请毕业审核",
    })
    graduation_request_id = data.get("data", {}).get("request_id") if ok(status, data) else 0
    check("学生可发起毕业审核", ok(status, data) and bool(graduation_request_id),
          str(data.get("msg")))
    status, data = request("GET", "/api/lifecycle/graduation/pending", token=atok)
    check("员工可查看待审核毕业申请",
          ok(status, data)
          and any(row.get("id") == graduation_request_id for row in data.get("data", [])),
          str(data.get("msg")))
    status, data = request("POST", "/api/lifecycle/graduation/review", {
        "token": atok, "request_id": graduation_request_id,
        "approved": True, "review_note": "QA 材料审核通过",
    })
    check("毕业审核通过", ok(status, data), str(data.get("msg")))
    status, data = request("GET", "/api/lifecycle/my", token=stok)
    final_lifecycle = data.get("data", {}) if ok(status, data) else {}
    check("毕业审核通过后学籍自动变为已毕业",
          ok(status, data)
          and final_lifecycle.get("stage") == "ALUMNI"
          and final_lifecycle.get("enrollment", {}).get("actual_graduation_date") == "2030-11-20"
          and any(event.get("source_type") == "GRADUATION_REVIEW"
                  for event in final_lifecycle.get("lifecycle_events", [])),
          str(final_lifecycle.get("stage") or data.get("msg")))

    section("6. token 撤销")
    status, data = request("POST", "/api/auth/staff", {
        "token": atok, "action": "create", "username": qa_staff_name,
        "password": staff_pwd, "real_name": "QA 员工",
        "staff_role": "ADVISOR", "department": "QA部",
        "security_question": "测试问题", "security_answer": "测试答案",
    })
    check("创建测试员工", ok(status, data), str(data.get("msg")))
    status, data = request("POST", "/api/auth/login", {
        "username": qa_staff_name, "password": staff_pwd,
    })
    if ok(status, data):
        staff = data["data"]
        qa_uids.append(staff["user_id"])
        new_staff_pwd = "Qa!staffReset2026"
        status, data = request("POST", "/api/auth/staff", {
            "token": atok, "action": "reset_password",
            "user_id": staff["user_id"], "password": new_staff_pwd,
        })
        check("重置员工密码", ok(status, data), str(data.get("msg")))
        status, data = request("GET", "/api/auth/me", token=staff["token"])
        check("重置密码后旧 token 失效", status in (401, 403), f"HTTP {status}")
        status, data = request("POST", "/api/auth/login", {
            "username": qa_staff_name, "password": staff_pwd,
        })
        check("重置后旧密码失效", status == 400, f"HTTP {status}")
        status, data = request("POST", "/api/auth/login", {
            "username": qa_staff_name, "password": new_staff_pwd,
        })
        check("重置后新密码可登录", ok(status, data), str(data.get("msg")))
        if ok(status, data):
            staff = data["data"]
            qa_uids[-1] = staff["user_id"]
        status, data = request("POST", "/api/auth/staff", {
            "token": atok, "action": "disable", "user_id": staff["user_id"],
        })
        check("停用员工", ok(status, data), str(data.get("msg")))
        status, data = request("GET", "/api/auth/me", token=staff["token"])
        check("停用后旧 token 失效", status in (401, 403), f"HTTP {status}")

    status, data = request("POST", "/api/auth/staff", {
        "token": atok, "action": "create", "username": "qa_maint_" + stamp,
        "password": staff_pwd, "real_name": "QA 维护人员", "account_role": "MAINTAINER",
        "security_question": "测试问题", "security_answer": "测试答案",
    })
    check("创建维护人员角色", ok(status, data), str(data.get("msg")))
    status, data = request("POST", "/api/auth/login", {
        "username": "qa_maint_" + stamp, "password": staff_pwd,
    })
    if ok(status, data):
        maint = data["data"]
        qa_uids.append(maint["user_id"])
        status, data = request("GET", "/api/admin/alerts", token=maint["token"])
        check("维护人员不能读业务隐私", status == 403, f"HTTP {status}")
        status, data = request("GET", "/health")
        check("维护人员可做健康检查", status == 200)

    if args.dify:
        section("7. 真实 Dify 会话（可选）")
        dify_leave_user = "qa_dify_leave_" + stamp
        status, data = request("POST", "/api/auth/register", {
            "username": dify_leave_user,
            "password": "Qa!difyleave2026",
            "real_name": "QA Dify 请假",
            "security_question": "测试问题",
            "security_answer": "测试答案",
        })
        check("Dify 请假编排测试账号注册", ok(status, data), str(data.get("msg")))
        if ok(status, data):
            dify_student = data["data"]
            dify_uid = dify_student["user_id"]
            dify_token = dify_student["token"]
            qa_uids.append(dify_uid)

            status, data = request("GET", "/api/application/my", token=dify_token)
            dify_app_before = len(data.get("data", [])) if ok(status, data) else -1
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token,
                "query": (
                    "我想申请" + sg_program.get("country", "") + "的"
                    + sg_program.get("project_name", "") + "，学习模式是"
                    + sg_program.get("category", "") + "，备注是 Dify 申请确认测试"
                ),
            }, timeout=120)
            dify_app_preview = data.get("data", {}).get("answer", "") if ok(status, data) else ""
            dify_app_conversation = (
                data.get("data", {}).get("conversation_id", "")
                if ok(status, data) else ""
            )
            status, count_data = request("GET", "/api/application/my", token=dify_token)
            dify_app_after_preview = (
                len(count_data.get("data", [])) if ok(status, count_data) else -2
            )
            check("真实 Dify 就读申请先预览确认且不提前写库",
                  ok(status, count_data)
                  and "确认" in dify_app_preview
                  and dify_app_after_preview == dify_app_before,
                  dify_app_preview)
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": dify_app_conversation,
                "query": "确认",
            }, timeout=120)
            dify_app_confirm = data.get("data", {}).get("answer", "") if ok(status, data) else ""
            dify_app_conversation = (
                data.get("data", {}).get("conversation_id", dify_app_conversation)
                if ok(status, data) else dify_app_conversation
            )
            status, count_data = request("GET", "/api/application/my", token=dify_token)
            dify_apps = count_data.get("data", []) if ok(status, count_data) else []
            dify_action_app_id = (
                int(dify_apps[0]["id"]) if len(dify_apps) > dify_app_before else 0
            )
            if dify_action_app_id:
                app_ids.append(dify_action_app_id)
            check("真实 Dify 用户确认后提交就读申请",
                  ok(status, count_data)
                  and dify_action_app_id > 0
                  and "申请" in dify_app_confirm,
                  dify_app_confirm)
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": dify_app_conversation,
                "query": (
                    "我要修改申请，改到" + de_program.get("country", "")
                    + "，模式" + de_program.get("category", "")
                    + "，项目" + de_program.get("project_name", "")
                    + "，备注是 Dify 修改确认测试"
                ),
            }, timeout=120)
            dify_app_update_preview = (
                data.get("data", {}).get("answer", "") if ok(status, data) else ""
            )
            dify_app_conversation = (
                data.get("data", {}).get("conversation_id", dify_app_conversation)
                if ok(status, data) else dify_app_conversation
            )
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": dify_app_conversation,
                "query": "确认",
            }, timeout=120)
            dify_app_update_result = (
                data.get("data", {}).get("answer", "") if ok(status, data) else ""
            )
            dify_app_conversation = (
                data.get("data", {}).get("conversation_id", dify_app_conversation)
                if ok(status, data) else dify_app_conversation
            )
            status, data = request("GET", "/api/application/my", token=dify_token)
            updated_app = next((
                row for row in data.get("data", [])
                if int(row.get("id") or 0) == dify_action_app_id
            ), {}) if ok(status, data) else {}
            check("真实 Dify 待受理申请先确认再修改",
                  "确认" in dify_app_update_preview
                  and updated_app.get("country") == de_program.get("country")
                  and updated_app.get("program") == de_program.get("project_name")
                  and "修改" in dify_app_update_result,
                  str(updated_app or dify_app_update_preview))
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": dify_app_conversation,
                "query": "撤销申请 " + str(dify_action_app_id),
            }, timeout=120)
            dify_app_cancel_preview = (
                data.get("data", {}).get("answer", "") if ok(status, data) else ""
            )
            dify_app_conversation = (
                data.get("data", {}).get("conversation_id", dify_app_conversation)
                if ok(status, data) else dify_app_conversation
            )
            status, count_data = request("GET", "/api/application/my", token=dify_token)
            app_before_cancel_confirm = next((
                row for row in count_data.get("data", [])
                if int(row.get("id") or 0) == dify_action_app_id
            ), {}) if ok(status, count_data) else {}
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": dify_app_conversation,
                "query": "确认",
            }, timeout=120)
            status, count_data = request("GET", "/api/application/my", token=dify_token)
            cancelled_app = next((
                row for row in count_data.get("data", [])
                if int(row.get("id") or 0) == dify_action_app_id
            ), {}) if ok(status, count_data) else {}
            check("真实 Dify 撤销申请确认前不写库且确认后正式撤销",
                  "确认" in dify_app_cancel_preview
                  and app_before_cancel_confirm.get("status") == "待受理"
                  and cancelled_app.get("status") == "已撤销",
                  str({"before": app_before_cancel_confirm.get("status"),
                       "after": cancelled_app.get("status"),
                       "preview": dify_app_cancel_preview}))

            status, data = request("POST", "/api/application/create", {
                "token": atok, "student_id": dify_uid,
                "country": de_program.get("country", ""),
                "university": "QA Dify 推进大学",
                "program": de_program.get("project_name", ""),
            })
            dify_advance_app_id = (
                int(data.get("data", {}).get("application_id") or 0)
                if ok(status, data) else 0
            )
            if dify_advance_app_id:
                app_ids.append(dify_advance_app_id)
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": atok,
                "query": "推进申请 " + str(dify_advance_app_id)
                         + "，备注是 Dify 推进确认测试",
            }, timeout=120)
            dify_advance_preview = (
                data.get("data", {}).get("answer", "") if ok(status, data) else ""
            )
            dify_advance_conversation = (
                data.get("data", {}).get("conversation_id", "")
                if ok(status, data) else ""
            )
            status, count_data = request(
                "GET", "/api/application/my", token=dify_token
            )
            before_advance_confirm = next((
                row for row in count_data.get("data", [])
                if int(row.get("id") or 0) == dify_advance_app_id
            ), {}) if ok(status, count_data) else {}
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": atok, "conversation_id": dify_advance_conversation,
                "query": "确认",
            }, timeout=120)
            status, count_data = request(
                "GET", "/api/application/my", token=dify_token
            )
            after_advance_confirm = next((
                row for row in count_data.get("data", [])
                if int(row.get("id") or 0) == dify_advance_app_id
            ), {}) if ok(status, count_data) else {}
            check("真实 Dify 申请推进确认前不写库且确认后只前进一阶段",
                  "确认" in dify_advance_preview
                  and before_advance_confirm.get("stage") == "选校定位"
                  and after_advance_confirm.get("stage") == "材料准备",
                  str({"before": before_advance_confirm.get("stage"),
                       "after": after_advance_confirm.get("stage"),
                       "preview": dify_advance_preview}))

            status, data = request("GET", "/api/employee/reports", token=atok)
            dify_report_before = len(data.get("data", [])) if ok(status, data) else -1
            dify_report_content = "QA日报 Dify" + stamp
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": atok,
                "query": "帮我写今天的日报，内容是：" + dify_report_content,
            }, timeout=120)
            dify_report_preview = (
                data.get("data", {}).get("answer", "") if ok(status, data) else ""
            )
            dify_report_conversation = (
                data.get("data", {}).get("conversation_id", "")
                if ok(status, data) else ""
            )
            status, count_data = request("GET", "/api/employee/reports", token=atok)
            dify_report_mid = (
                len(count_data.get("data", [])) if ok(status, count_data) else -2
            )
            check("真实 Dify 日报先预览确认且不提前写库",
                  "确认" in dify_report_preview
                  and dify_report_mid == dify_report_before,
                  dify_report_preview)
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": atok, "conversation_id": dify_report_conversation,
                "query": "确认",
            }, timeout=120)
            status, count_data = request("GET", "/api/employee/reports", token=atok)
            dify_report_rows = count_data.get("data", []) if ok(status, count_data) else []
            check("真实 Dify 用户确认后提交员工日报",
                  ok(status, count_data)
                  and len(dify_report_rows) == dify_report_before + 1
                  and any(row.get("content") == dify_report_content
                          for row in dify_report_rows),
                  str(dify_report_rows[:1]))

            status, data = request("POST", "/api/content/students/stage", {
                "token": atok, "student_id": dify_uid, "stage": "ENROLLED",
                "note": "QA Dify 请假编排测试", "force": True,
                "effective_date": "2026-11-20",
            })
            check("Dify 请假编排测试账号进入在读", ok(status, data), str(data.get("msg")))
            status, data = request("GET", "/api/student/my_leaves", token=dify_token)
            dify_leave_before = len(data.get("data", [])) if ok(status, data) else -1
            request("POST", "/api/student/leave/prepare", {
                "token": dify_token,
                "query": "事假，2026-10-07到2026-10-09，家里有事",
                "action": "collect",
                "leave_type": "事假",
                "date_text": "2026-10-07到2026-10-09",
                "reason": "家里有事",
            })
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "query": "我要请假",
            }, timeout=120)
            first_answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
            conversation_id = data.get("data", {}).get("conversation_id", "") if ok(status, data) else ""
            check("真实 Dify 请假首轮由 LLM 追问缺失字段",
                  ok(status, data)
                  and data.get("data", {}).get("orchestrator") != "backend_fallback"
                  and "类型" in first_answer
                  and "日期" in first_answer
                  and "原因" in first_answer
                  and "2026-10-07" not in first_answer
                  and "2026-10-09" not in first_answer,
                  first_answer or str(data.get("msg")))
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": conversation_id,
                "query": "事假，2028-03-01到2028-03-03，因为家里有事",
            }, timeout=120)
            confirm_chat_ok = ok(status, data)
            confirm_answer = data.get("data", {}).get("answer", "") if confirm_chat_ok else ""
            conversation_id = data.get("data", {}).get("conversation_id", conversation_id) if confirm_chat_ok else conversation_id
            status, count_data = request("GET", "/api/student/my_leaves", token=dify_token)
            count_after_prompt = len(count_data.get("data", [])) if ok(status, count_data) else -2
            check("真实 Dify 字段齐全后复述确认且不提前写库",
                  confirm_chat_ok
                  and data.get("data", {}).get("orchestrator") != "backend_fallback"
                  and "3" in confirm_answer
                  and "确认" in confirm_answer
                  and count_after_prompt == dify_leave_before,
                  confirm_answer or str(data.get("msg")))
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": conversation_id,
                "query": "确认",
            }, timeout=120)
            final_chat_ok = ok(status, data)
            final_answer = data.get("data", {}).get("answer", "") if final_chat_ok else ""
            status, count_data = request("GET", "/api/student/my_leaves", token=dify_token)
            dify_leave_rows = count_data.get("data", []) if ok(status, count_data) else []
            check("真实 Dify 用户确认后经 confirm_leave 写入正式请假单",
                  final_chat_ok
                  and data.get("data", {}).get("orchestrator") != "backend_fallback"
                  and len(dify_leave_rows) == dify_leave_before + 1
                  and any(
                      row.get("start_time") == "2028-03-01"
                      and row.get("end_time") == "2028-03-03"
                      for row in dify_leave_rows
                  ),
                  final_answer + " rows=" + str(len(dify_leave_rows)))
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": conversation_id,
                "query": "事假，2026-02-30 至 2026-03-02，原因家里有事",
            }, timeout=120)
            invalid_answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
            status, count_data = request("GET", "/api/student/my_leaves", token=dify_token)
            rows_after_invalid = count_data.get("data", []) if ok(status, count_data) else []
            check("真实 Dify 将 FastAPI 非法日期错误自然语言返回且不写库",
                  ok(status, data)
                  and data.get("data", {}).get("orchestrator") != "backend_fallback"
                  and "2026-02-30" in invalid_answer
                  and len(rows_after_invalid) == len(dify_leave_rows),
                  invalid_answer or str(data.get("msg")))
            status, data = request("POST", "/api/auth/dify-chat", {
                "token": dify_token, "conversation_id": conversation_id,
                "query": "事假，2026-10-07到2026-10-09，家里有事",
            }, timeout=120)
            past_answer = data.get("data", {}).get("answer", "") if ok(status, data) else ""
            status, count_data = request("GET", "/api/student/my_leaves", token=dify_token)
            rows_after_past = count_data.get("data", []) if ok(status, count_data) else []
            check("真实 Dify 将历史日期错误自然语言返回且不写库",
                  ok(status, data)
                  and data.get("data", {}).get("orchestrator") != "backend_fallback"
                  and ("早于今天" in past_answer
                       or "早于系统要求的今天" in past_answer)
                  and len(rows_after_past) == len(dify_leave_rows),
                  past_answer or str(data.get("msg")))

        status, data = request("GET", "/api/student/my_leaves", token=stok)
        leave_count_before = len(data.get("data", [])) if ok(status, data) else -1
        status, data = request("POST", "/api/auth/dify-chat", {
            "token": stok, "query": "查一下我的成绩",
        }, timeout=95)
        check("学生 Dify 经 Authorization 头调用成绩工具",
              ok(status, data) and bool(data.get("data", {}).get("answer"))
              and "|" in data.get("data", {}).get("answer", ""),
              str(data.get("msg")))
        status, data = request("POST", "/api/auth/dify-chat", {
            "token": stok, "query": "我的学籍阶段是什么？",
        }, timeout=95)
        check("学生 Dify 查询学籍阶段",
              ok(status, data) and bool(data.get("data", {}).get("answer")),
              str(data.get("msg")))

        status, data = request("POST", "/api/auth/dify-chat", {
            "token": stok, "query": "请假流程是什么？",
        }, timeout=95)
        check("学生请假咨询走知识库", ok(status, data) and bool(data.get("data", {}).get("answer")),
              str(data.get("msg")))
        status, data = request("GET", "/api/student/my_leaves", token=stok)
        leave_count_after = len(data.get("data", [])) if ok(status, data) else -2
        check("请假咨询不会误写请假单", leave_count_before == leave_count_after,
              f"{leave_count_before} -> {leave_count_after}")

        status, data = request("POST", "/api/auth/dify-chat", {
            "token": atok, "query": "查一下" + lead_name + "的跟进记录",
        }, timeout=95)
        check("员工 Dify 客户查询", ok(status, data) and bool(data.get("data", {}).get("answer")),
              str(data.get("msg")))
        status, data = request("POST", "/api/auth/dify-chat", {
            "token": atok, "query": "新员工入职流程是什么？",
        }, timeout=95)
        check("员工 Dify 知识问答", ok(status, data) and bool(data.get("data", {}).get("answer")),
              str(data.get("msg")))
        status, data = request("POST", "/api/auth/public-chat", {
            "query": "我19岁，高中刚毕业，想继续读本科，推荐什么项目？",
            "visitor": "qa_guest_" + stamp,
        }, timeout=95)
        check("客服 Dify 画像推荐", ok(status, data) and bool(data.get("data", {}).get("answer")),
              str(data.get("msg")))
        status, data = request("POST", "/api/auth/public-chat", {
            "query": "有哪些课程项目？", "visitor": "qa_guest_" + stamp,
        }, timeout=95)
        check("客服 Dify 课程查询", ok(status, data)
              and "|" in data.get("data", {}).get("answer", ""),
              str(data.get("msg")))
        status, data = request("POST", "/api/auth/public-chat", {
            "query": "介绍一下你们公司", "visitor": "qa_guest_company_" + stamp,
        }, timeout=95)
        company_chat = (data.get("data") or {}) if ok(status, data) else {}
        check("客服公司介绍经过 Dify 检索并注入公司资料",
              company_chat.get("orchestrator") == "dify"
              and "粤教服务" in company_chat.get("answer", "")
              and "暂未提供这项信息" not in company_chat.get("answer", ""),
              str(company_chat.get("answer") or data.get("msg")))

    section("清理")
    cleanup(qa_uids, lead_ids, event_ids, app_ids, registration_contacts)
    return finish()


def cleanup(qa_uids, lead_ids, event_ids, app_ids, registration_contacts=None):
    db_path = Path(os.getenv(
        "YUEJIAO_DB_FILE",
        str(Path(__file__).resolve().parent / "yuejiao.db"),
    ))
    if not db_path.exists():
        print("[WARN] 找不到测试数据库，跳过清理:", db_path)
        return
    conn = sqlite3.connect(str(db_path))
    try:
        registration_contacts = registration_contacts or []
        if registration_contacts:
            q = ",".join("?" * len(registration_contacts))
            registration_lead_ids = [
                row[0] for row in conn.execute(
                    "SELECT DISTINCT lead_id FROM event_registration "
                    "WHERE contact_info IN (" + q + ") AND COALESCE(lead_id,0)>0",
                    registration_contacts,
                ).fetchall()
            ]
            # 先按活动统计并归还名额，再删除本轮访客报名，避免测试永久占用真实活动席位。
            counts = conn.execute(
                "SELECT event_id, COUNT(*) FROM event_registration "
                "WHERE contact_info IN (" + q + ") GROUP BY event_id",
                registration_contacts,
            ).fetchall()
            for event_id, count in counts:
                conn.execute(
                    "UPDATE event_lecture "
                    "SET current_participants=MAX(COALESCE(current_participants,0)-?,0) "
                    "WHERE id=?",
                    (count, event_id),
                )
            conn.execute(
                "DELETE FROM event_registration WHERE contact_info IN (" + q + ")",
                registration_contacts,
            )
            if registration_lead_ids:
                lead_q = ",".join("?" * len(registration_lead_ids))
                # 只删除已无任何报名引用的测试线索，复用中的真实客户不会被误删。
                conn.execute(
                    "DELETE FROM crm_lead WHERE id IN (" + lead_q + ") "
                    "AND id NOT IN (SELECT lead_id FROM event_registration "
                    "               WHERE COALESCE(lead_id,0)>0)",
                    registration_lead_ids,
                )
        conn.execute(
            "DELETE FROM event_registration_draft "
            "WHERE visitor_key LIKE 'qa_event_visitor_%'"
        )
        conn.execute(
            "DELETE FROM business_action_draft "
            "WHERE draft_key LIKE 'VISITOR:qa_event_visitor_%'"
        )
        # 管理员账号是环境中的长期账号，不能删除；只清理本测试生成的日报。
        conn.execute("DELETE FROM employee_daily_report WHERE content LIKE 'QA日报%'")
        if app_ids:
            q = ",".join("?" * len(app_ids))
            material_files = conn.execute(
                f"SELECT storage_name FROM application_material "
                f"WHERE application_id IN ({q}) AND storage_name IS NOT NULL",
                app_ids,
            ).fetchall()
            upload_root = Path(os.getenv(
                "YUEJIAO_UPLOAD_DIR",
                str(Path(__file__).resolve().parent / "uploads" / "application_materials"),
            )).resolve()
            for (storage_name,) in material_files:
                file_path = (upload_root / str(storage_name or "")).resolve()
                if upload_root in file_path.parents and file_path.is_file():
                    file_path.unlink()
            conn.execute(
                f"DELETE FROM application_material WHERE application_id IN ({q})",
                app_ids,
            )
            conn.execute(
                f"DELETE FROM application_stage_record WHERE application_id IN ({q})",
                app_ids,
            )
            conn.execute(f"DELETE FROM application_history WHERE application_id IN ({q})", app_ids)
            conn.execute(f"DELETE FROM application_progress WHERE id IN ({q})", app_ids)
        if event_ids:
            q = ",".join("?" * len(event_ids))
            conn.execute(f"DELETE FROM event_registration WHERE event_id IN ({q})", event_ids)
            conn.execute(f"DELETE FROM event_lecture WHERE id IN ({q})", event_ids)
        if lead_ids:
            q = ",".join("?" * len(lead_ids))
            conn.execute(f"DELETE FROM crm_lead WHERE id IN ({q})", lead_ids)
        # 并发报名和公开客服会在通用 lead_ids 之外生成带 QA 前缀的线索。
        # 用本轮独有的 QA 命名清理，避免测试通知、对话和客户留在演示数据中。
        conn.execute(
            "DELETE FROM crm_lead "
            "WHERE customer_name LIKE 'QA%' OR contact_info LIKE 'qa_%'"
        )
        conn.execute(
            "DELETE FROM notification "
            "WHERE content LIKE '%QA 留学生%' OR title LIKE '%QA%' "
            "OR content LIKE '%QA公开线索_%' OR content LIKE '%QA并发%' "
            "OR content LIKE '%QA活动%' OR content LIKE '%qa_concurrent_%'"
        )
        if registration_contacts:
            notification_where = " OR ".join(
                "content LIKE ?" for _ in registration_contacts
            )
            conn.execute(
                "DELETE FROM notification WHERE " + notification_where,
                ["%" + value + "%" for value in registration_contacts],
            )
            conn.execute(
                "DELETE FROM conversation_log "
                "WHERE user_input LIKE '%QA%' OR user_input LIKE '%qa_%' "
                "OR ai_output LIKE '%QA活动%' OR ai_output LIKE '%QA并发%'"
            )
        if qa_uids:
            q = ",".join("?" * len(qa_uids))
            conn.execute(
                "DELETE FROM business_action_result WHERE draft_key IN ("
                f"SELECT 'STUDENT:' || id FROM sys_user WHERE id IN ({q}) "
                "UNION "
                f"SELECT 'EMPLOYEE:' || id FROM sys_user WHERE id IN ({q}) "
                "UNION "
                f"SELECT 'ADMIN:' || id FROM sys_user WHERE id IN ({q}))",
                qa_uids * 3,
            )
            # 测试学生报名活动后先归还名额，再删除报名记录。
            for event_id, count in conn.execute(
                "SELECT event_id,COUNT(*) FROM event_registration "
                f"WHERE customer_id IN ({q}) GROUP BY event_id",
                qa_uids,
            ).fetchall():
                conn.execute(
                    "UPDATE event_lecture SET current_participants="
                    "MAX(COALESCE(current_participants,0)-?,0) WHERE id=?",
                    (count, event_id),
                )
            for table, col in [
                ("business_action_draft", "actor_id"),
                ("application_history", "operator_id"),
                ("application_progress", "student_id"),
                ("conversation_log", "user_id"),
                ("notification", "user_id"),
                ("student_admin_service", "student_id"),
                ("student_feedback_ticket", "student_id"),
                ("student_psych_alert", "student_id"),
                ("student_psych_profile", "student_id"),
                ("student_score", "student_id"),
                ("event_registration", "customer_id"),
                ("student_graduation_request", "student_id"),
                ("student_lifecycle_event", "student_id"),
                ("student_enrollment", "student_id"),
                ("leave_request_draft", "student_id"),
                ("employee_daily_report", "employee_id"),
                ("crm_lead", "owner_employee_id"),
            ]:
                conn.execute(f"DELETE FROM {table} WHERE {col} IN ({q})", qa_uids)
            conn.execute(f"DELETE FROM sys_user WHERE id IN ({q})", qa_uids)
        conn.commit()
        print("已清理 QA 账号及本轮精确 ID 的业务数据")
    finally:
        conn.close()


def finish():
    print("\n" + "=" * 68)
    print(f"总计 {PASS + FAIL} 项，通过 {PASS}，失败 {FAIL}")
    if FAILED:
        print("失败项：", ", ".join(FAILED))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
