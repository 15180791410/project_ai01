"""员工智能助手的成绩问答与确定性写库。

大模型可以组织文字，但不能假装已经修改数据库。本模块负责识别
“查询/录入/修改/删除成绩”的真实业务动作，校验员工权限和学生数据范围，
执行 SQL 后返回 changed 标记，供前端触发学生端实时刷新。
"""
from __future__ import annotations

import re

from db import get_db
from service import rbac


SCORE_WORDS = ("成绩", "分数", "雅思", "托福", "PTE", "GRE", "绩点")
COURSE_WORDS = (
    "雅思", "托福", "PTE", "GRE", "数学", "英语", "学术英语",
    "德语", "海外学习适应",
)


def _md_table(headers, rows):
    """把确定性成绩结果输出为前端可渲染的 Markdown 表格。"""
    def clean(value):
        return str(value if value is not None else "").replace("|", "／").replace("\n", " ")

    lines = [
        "| " + " | ".join(clean(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(clean(item) for item in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def _affected_students(conn, user):
    scope, args = rbac.student_scope(user)
    return conn.execute(
        "SELECT u.id, u.username, u.real_name "
        "FROM sys_user u WHERE u.user_type='STUDENT'" + scope +
        " ORDER BY u.id",
        tuple(args),
    ).fetchall()


def _pick_student(students, text):
    """按完整姓名、账号或“姓氏 + 同学”选择唯一学生。"""
    exact = [
        row for row in students
        if row["real_name"] and row["real_name"] in text
    ]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, "匹配到多名学生，请补充完整姓名或账号。"

    by_username = [
        row for row in students
        if row["username"] and str(row["username"]) in text
    ]
    if len(by_username) == 1:
        return by_username[0], ""

    surnames = []
    for row in students:
        name = row["real_name"] or ""
        if name and re.search(re.escape(name[:1]) + r"\s*同学", text):
            surnames.append(row)
    if len(surnames) == 1:
        return surnames[0], ""
    if len(surnames) > 1:
        return None, "匹配到多名同姓学生，请补充完整姓名或账号。"
    return None, "没有找到匹配的学生，请确认姓名或账号。"


def _pick_course(text, scores, allow_single_fallback=True):
    """从文本中选课程，优先采用最先出现的课程名。

    不能直接按 COURSE_WORDS 的固定顺序返回，否则历史消息里提过“雅思”、
    本次实际要说“数学”时，会被错误地拿去修改雅思。allow_single_fallback
    只用于用户只回复“改成 5.6 分”这类连续对话，并且该生本来只有一条成绩。
    """
    lowered = (text or "").lower()
    # 课程不仅可能是内置常见科目，也可能是运营人员录入的自定义课程。
    # 因此同时比较数据库里已有的课程名，并按“出现位置 + 名称长度”选择，
    # 例如“综合英语”应优先于其中的短词“英语”。
    course_names = list(dict.fromkeys(
        list(COURSE_WORDS) + [
            str(row["course_name"]) for row in scores if row["course_name"]
        ]
    ))
    matched = [
        (lowered.find(word.lower()), word)
        for word in course_names
        if word.lower() in lowered
    ]
    if matched:
        # 同一起点的候选中优先更长、更具体的课程名。
        return min(matched, key=lambda item: (item[0], -len(item[1])))[1]
    if allow_single_fallback and len(scores) == 1:
        return scores[0]["course_name"]
    return ""


def _query_scores(conn, student_id):
    return conn.execute(
        "SELECT id, course_name, score, semester FROM student_score "
        "WHERE student_id=? ORDER BY id ASC",
        (student_id,),
    ).fetchall()


def _chinese_number(text):
    """把“一”到“十”转换成整数，供“第一次成绩”等表达使用。"""
    digits = {
        "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }
    if text.isdigit():
        return int(text)
    return digits.get(text)


def _score_by_reference(text, scores):
    """按 ID 或“第一次/第二条/最后一次”选择一条成绩。

    返回值是 (是否检测到引用, 成绩行, 错误说明)。只要消息里明确写了
    序号或成绩 ID，就不能再用历史对话中的课程名覆盖，否则“删除第一次
    成绩”可能被上一轮提到的“雅思”带偏，误删另一门课程。
    """
    current = text or ""

    # 用户可以直接说“删除成绩 ID 29”或“删除编号29”，这是最精确的方式。
    id_match = re.search(
        r"(?:成绩\s*)?(?:id|编号)\s*[:：]?\s*(\d+)",
        current,
        re.I,
    )
    if id_match:
        score_id = int(id_match.group(1))
        row = next((item for item in scores if int(item["id"]) == score_id), None)
        if row:
            return True, row, ""
        return True, None, "没有找到成绩 ID " + str(score_id) + "。"

    # “最后一次/最新一条”与“第一次/最早一条”按数据库创建顺序判断。
    latest_words = ("最后一次", "最后一条", "最后一个", "最新一条", "最新成绩")
    earliest_words = ("第一次", "第一条", "第一个", "首条", "最早一条", "最早成绩")
    if any(word in current for word in latest_words):
        if not scores:
            return True, None, "该学生目前没有成绩记录。"
        return True, scores[-1], ""
    if any(word in current for word in earliest_words):
        if not scores:
            return True, None, "该学生目前没有成绩记录。"
        return True, scores[0], ""

    ordinal_match = re.search(
        r"第\s*([一二三四五六七八九十\d]+)\s*(?:条|次|个|项)?\s*(?:成绩|分数|记录)",
        current,
    )
    if ordinal_match:
        index = _chinese_number(ordinal_match.group(1))
        if not index or index < 1:
            return True, None, "成绩序号必须从 1 开始。"
        if index > len(scores):
            return True, None, "该学生只有 " + str(len(scores)) + " 条成绩记录。"
        return True, scores[index - 1], ""
    return False, None, ""


def _new_score_value(query):
    """新分数只从当前消息提取，兼容“雅思 6.7”“雅思，6.7”“6.7分”。"""
    text = (query or "").strip()
    match = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*分", text)
    if not match:
        match = re.search(
            r"(雅思|托福|PTE|GRE|数学|英语|德语)"
            r"\s*[,，:：]?\s*(\d{1,3}(?:\.\d+)?)",
            text,
            re.I,
        )
        number = match.group(2) if match else ""
    else:
        number = match.group(1)
    if not number:
        # 自定义课程也可能使用“课程名，分数”的简写。只允许整句由
        # 一个课程名、分隔符和一个数字组成，避免把“第 2 次 6.7 分”
        # 中的序号误当成最终成绩。
        compact = re.fullmatch(
            r"([\u4e00-\u9fa5A-Za-z ]{1,20})\s*[,，:：]\s*"
            r"(\d{1,3}(?:\.\d+)?)",
            text,
        )
        number = compact.group(2) if compact else ""
    if not number and re.fullmatch(r"\d{1,3}(?:\.\d+)?", text):
        number = text
    if not number:
        return None
    value = float(number)
    return value if 0 <= value <= 100 else None


def _recent_user_turns(history, limit=6):
    """把前端传来的用户问题文本拆成最近几轮，过滤空行。"""
    lines = [line.strip() for line in str(history or "").splitlines() if line.strip()]
    return lines[-limit:]


def _is_score_request(query, history):
    """判断本轮消息是否真的属于成绩业务。

    旧逻辑只要最近六轮出现过“成绩”就继续拦截，导致用户后来问班级或输入
    “？”时仍重复返回上一条成绩。现在当前消息优先；只有当前是成绩操作的
    合理续答时，才允许借用上一轮成绩上下文。
    """
    current = str(query or "").strip()
    if any(word.lower() in current.lower() for word in SCORE_WORDS):
        return True
    turns = _recent_user_turns(history)
    if not turns:
        return False
    # “雅思，6.7”“6.7分”“改成 6.7”这类续答必须在本轮仍明确指向成绩。
    if _new_score_value(current) is not None:
        return True
    if any(word in current for word in ("添加", "录入", "修改", "更新", "删除", "改成", "改为")):
        return True
    # 上一轮明确在查成绩时，用户只回复一个短姓名属于正常续答。
    previous = turns[-1]
    previous_score = any(word.lower() in previous.lower() for word in SCORE_WORDS)
    return previous_score and bool(re.fullmatch(r"[\u4e00-\u9fa5·]{2,12}", current))


def _student_from_recent_history(students, history):
    """从最近一轮提到姓名的历史消息中找回成绩操作对象。"""
    for line in reversed(_recent_user_turns(history)):
        student, _ = _pick_student(students, line)
        if student:
            return student
    return None


def _student_picker_answer(students):
    """没有指定学生时，先展示当前角色可操作的学生，而不是回答“没找到”。"""
    if not students:
        return "你当前的数据范围内没有学生，或当前角色没有查看学生的权限。"
    table = _md_table(
        ("序号", "学生姓名", "账号"),
        (
            (index, row["real_name"] or row["username"], row["username"])
            for index, row in enumerate(students[:12], 1)
        ),
    )
    suffix = "\n\n" + "其余 " + str(len(students) - 12) + " 名学生请继续补充姓名或账号。" if len(students) > 12 else ""
    return (
        "你当前可查询 " + str(len(students)) + " 名学生：\n\n"
        + table + suffix
        + "\n\n请回复学生姓名，并可同时补充科目，例如“林子涵 雅思”。"
    )


def handle_score_management(user, query, history=""):
    """返回成绩操作结果；不是成绩管理问题时返回 None。"""
    if not rbac.has_capability(user, "score.read"):
        return None
    current = (query or "").strip()
    context = ((history or "") + "\n" + current).strip()
    if not _is_score_request(current, history):
        return None

    operation = "query"
    if "删除" in context:
        operation = "delete"
    elif any(word in context for word in ("修改", "更新", "改成", "改为", "调整")):
        operation = "update"
    elif any(word in context for word in ("录入", "新增", "添加", "记录")):
        operation = "add"
    if operation != "query" and not rbac.has_capability(user, "score.manage"):
        return {
            "answer": "当前角色无权录入、修改或删除成绩。",
            "changed": False,
        }

    conn = get_db()
    try:
        students = _affected_students(conn, user)
        student, error = _pick_student(students, current)
        # “查成绩”是独立查询入口，必须让用户先明确学生；只有新增、
        # 修改、删除成绩时才允许承接上一轮学生上下文。
        if not student and operation != "query":
            student = _student_from_recent_history(students, history)
        if not student:
            # 用户只说“查成绩/添加成绩”时，列出可选学生并等待补充。
            if operation in ("query", "add", "update"):
                return {
                    "answer": _student_picker_answer(students),
                    "changed": False,
                }
            return {"answer": error, "changed": False}
        scores = _query_scores(conn, student["id"])
        has_reference, referenced_target, reference_error = _score_by_reference(
            current, scores
        )
        if reference_error:
            return {"answer": reference_error, "changed": False}
        # 当前消息中的课程优先级最高；只有当前消息没写课程时，才从最近
        # 几轮对话中补全，避免把上一轮查询的科目误当成这一轮修改目标。
        course = _pick_course(current, scores)
        if not has_reference and not course:
            course = _pick_course(context, scores, allow_single_fallback=False)
        student_name = student["real_name"] or student["username"]

        if operation == "query":
            if not scores:
                return {
                    "answer": student_name + "目前没有成绩记录。",
                    "changed": False,
                }
            table = _md_table(
                ("课程名称", "成绩", "学期"),
                (
                    (
                        row["course_name"],
                        str(row["score"]) + " 分",
                        row["semester"] or "未填写",
                    )
                    for row in scores
                ),
            )
            return {
                "answer": "已查到 " + student_name + " 的成绩：\n\n" + table,
                "changed": False,
            }

        # “全部/所有”只看本轮消息。不能因为上一轮说过“查看全部成绩”，
        # 下一轮“删除第一次成绩”就误删整张成绩表。
        if "全部" in current or "所有" in current:
            if operation != "delete":
                return {
                    "answer": "不能一次修改全部成绩，请指定具体课程和分数。",
                    "changed": False,
                }
            cursor = conn.execute(
                "DELETE FROM student_score WHERE student_id=?", (student["id"],)
            )
            conn.commit()
            return {
                "answer": "已删除 " + student_name + " 的全部 "
                          + str(cursor.rowcount) + " 条成绩记录。",
                "changed": cursor.rowcount > 0,
            }

        target = referenced_target
        if not target and course:
            target = next((
                row for row in scores
                if course.lower() in str(row["course_name"]).lower()
            ), None)

        if operation == "delete":
            if not target:
                return {
                    "answer": "没有找到要删除的课程成绩，请补充课程名称。",
                    "changed": False,
                }
            conn.execute("DELETE FROM student_score WHERE id=?", (target["id"],))
            conn.commit()
            return {
                "answer": "已删除 " + student_name + " 的"
                          + str(target["course_name"]) + "成绩"
                          + "（ID " + str(target["id"]) + "）。",
                "changed": True,
            }

        value = _new_score_value(current)
        if value is None:
            return {
                "answer": "请提供要录入或修改的具体分数，例如“5.6分”。",
                "changed": False,
            }
        if not course and not target:
            return {
                "answer": "请补充课程或科目名称，例如“雅思”。",
                "changed": False,
            }
        if target and not course:
            # “把第一条成绩改成 5.6 分”没有重复写课程名，但目标记录
            # 已经由序号或 ID 唯一确定，此时直接沿用该记录自己的课程名。
            course = str(target["course_name"])

        if operation == "update":
            if not target:
                # “更新为 5.6 分”在实际咨询中也常用于“该科目原本没有记录，
                # 现在补录”。此时执行安全 upsert，避免员工看到假更新后学生端
                # 仍然为空；回复会明确说明是新建，而不是谎称修改旧记录。
                conn.execute(
                    "INSERT INTO student_score "
                    "(student_id, course_name, score, semester) VALUES (?,?,?,?)",
                    (student["id"], course, value, "智能助手录入"),
                )
                conn.commit()
                return {
                    "answer": "原无 " + student_name + " 的" + course
                              + "记录，已新建成绩：" + str(value) + " 分。",
                    "changed": True,
                }
            conn.execute(
                "UPDATE student_score SET score=? WHERE id=?",
                (value, target["id"]),
            )
            conn.commit()
            return {
                "answer": "已将 " + student_name + " 的" + str(target["course_name"])
                          + "成绩更新为 " + str(value) + " 分。",
                "changed": True,
            }

        if target:
            conn.execute(
                "UPDATE student_score SET score=? WHERE id=?",
                (value, target["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO student_score "
                "(student_id, course_name, score, semester) VALUES (?,?,?,?)",
                (student["id"], course, value, "智能助手录入"),
            )
        conn.commit()
        return {
            "answer": "已录入 " + student_name + " 的" + course
                      + "成绩：" + str(value) + " 分。",
            "changed": True,
        }
    finally:
        conn.close()
