"""请假时间的确定性解析。

支持明确日期、日期区间、今天/明天/后天、下周几，以及中文或阿拉伯数字时长。
"""
import re
import calendar
from datetime import datetime, timedelta


CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def _cn_number(text: str):
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if not text:
        return None
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = CN_DIGITS.get(left, 1) if left else 1
        ones = CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(text) == 1:
        return CN_DIGITS.get(text)
    return None


def _extract_dates(text: str):
    patterns = [
        r"(?P<y>\d{4})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?",
        r"(?<!\d)(?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?",
    ]
    now = datetime.now()
    found = []
    for pattern in patterns:
        for match in re.finditer(pattern, text or ""):
            try:
                year = int(match.groupdict().get("y") or now.year)
                month = int(match.group("m"))
                day = int(match.group("d"))
                found.append((match.start(), datetime(year, month, day)))
            except (TypeError, ValueError):
                continue
        if found:
            break
    found.sort(key=lambda item: item[0])
    unique = []
    for _, value in found:
        if value not in unique:
            unique.append(value)
    return unique


def _invalid_explicit_date(text: str):
    """返回第一个格式正确但不存在的日期，例如 2026-02-30。"""
    now = datetime.now()
    patterns = [
        r"(?P<y>\d{4})[-/.年](?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?",
        r"(?<!\d)(?P<m>\d{1,2})[-/.月](?P<d>\d{1,2})日?",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text or ""):
            try:
                datetime(
                    int(match.groupdict().get("y") or now.year),
                    int(match.group("m")),
                    int(match.group("d")),
                )
            except ValueError:
                return match.group(0)
    return ""


def _duration_days(text: str):
    if "半天" in text:
        return 0.5
    match = re.search(r"(\d+|[零一二两三四五六七八九十]+)\s*天", text)
    if match:
        number = _cn_number(match.group(1))
        if number and 0 < number <= 60:
            return float(number)
    return None


def _relative_dates(text: str):
    """解析一个或多个“下周X / 周X”，支持“下周一到下周三”区间。"""
    now = datetime.now()
    weekdays = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    matches = list(re.finditer(r"下周([一二三四五六日天])", text))
    if matches:
        days_to_next_monday = 7 - now.weekday()
        return [
            now + timedelta(days=days_to_next_monday + weekdays[match.group(1)])
            for match in matches
        ]
    # “周三”“星期三”“这周三”默认指最近一个尚未过去的对应日期。
    # 例如周五说“周三请假1天”，应登记到下周三，而不是今天。
    matches = list(re.finditer(r"(?<!下)(?:这|本)?(?:周|星期)([一二三四五六日天])", text))
    result = []
    for match in matches:
        target = weekdays[match.group(1)]
        days_ahead = (target - now.weekday()) % 7
        result.append(now + timedelta(days=days_ahead))
    return result


def _relative_start(text: str):
    now = datetime.now()
    if "后天" in text:
        return now + timedelta(days=2)
    if "明天" in text:
        return now + timedelta(days=1)
    if "今天" in text:
        return now
    dates = _relative_dates(text)
    return dates[0] if dates else None


def _day_of_month_dates(text: str):
    """解析“25号”“二十五日”“25号到27号”等省略年月的中文日期。

    未写月份时，优先使用当前月；如果该日已经过去，则自动使用下个月，
    避免用户输入 25 号时被错误地登记为今天或过去的日期。
    """
    now = datetime.now()
    matches = list(re.finditer(r"(?<!\d)(\d{1,2}|[一二三四五六七八九十]+)\s*[号日]", text))
    if not matches:
        return []
    explicit_current_month = any(word in text for word in ("本月", "这个月"))
    dates = []
    for match in matches:
        day = _cn_number(match.group(1))
        if not day or not 1 <= day <= 31:
            continue
        year, month = now.year, now.month
        if not explicit_current_month and day < now.day:
            month += 1
            if month == 13:
                year += 1
                month = 1
        last_day = calendar.monthrange(year, month)[1]
        day = min(day, last_day)
        dates.append(datetime(year, month, day))
    return dates


def has_leave_date_expression(text: str):
    """判断本轮文本是否真的包含日期或明确的时间模糊表达。"""
    value = str(text or "")
    if not value.strip():
        return False
    if _invalid_explicit_date(value):
        return True
    if _extract_dates(value) or _day_of_month_dates(value) or _relative_dates(value):
        return True
    return any(word in value for word in (
        "今天", "明天", "后天", "过几天", "过几天再", "过阵子", "改天",
        "找个时间", "以后", "下个月", "下周", "回头", "有空",
        "本月", "这个月",
    ))


def validate_leave_window(start_date: str, end_date: str):
    """校验请假日期窗口，返回用户可读错误；空字符串表示通过。"""
    try:
        start = datetime.strptime(str(start_date), "%Y-%m-%d").date()
        end = datetime.strptime(str(end_date), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return "请假起止日期格式不正确"
    today = datetime.now().date()
    if start < today:
        return (
            "开始日期不能早于今天（" + today.isoformat()
            + "），请提供今天或之后的日期。"
        )
    if end < start:
        return "结束日期不能早于开始日期"
    if end < today:
        return (
            "结束日期不能早于今天（" + today.isoformat()
            + "），请提供今天或之后的日期。"
        )
    return ""


def extract_leave_date_fields(text: str):
    """给多轮请假对话提取日期，返回结构化结果。

    与直接提交接口不同，这里不会把“无法识别”悄悄替换成今天；无法确定时
    明确返回 missing/ambiguous，交给对话层继续追问。
    """
    text = (text or "").strip()
    invalid_date = _invalid_explicit_date(text)
    if invalid_date:
        return {
            "start": "", "end": "", "days": None,
            "ambiguous": False, "missing": True,
            "error": "“" + invalid_date + "”不是有效日期，请重新提供正确的请假日期。",
        }
    dates = _extract_dates(text) or _day_of_month_dates(text)
    relative_dates = _relative_dates(text)
    start = dates[0] if dates else (
        relative_dates[0] if relative_dates else _relative_start(text)
    )
    end = dates[1] if len(dates) > 1 else (
        relative_dates[1] if len(relative_dates) > 1 else None
    )
    days = _duration_days(text)
    ambiguous_words = (
        "过几天", "过几天再说", "过阵子", "改天", "找个时间", "以后",
        "下个月", "下周", "回头", "有空",
    )
    ambiguous = False
    if not start and any(word in text for word in ambiguous_words):
        ambiguous = True
    elif any(word in text for word in ("过几天", "过阵子", "改天", "找个时间", "以后")):
        ambiguous = True
    if start and end is None and days:
        end = start if days == 0.5 else start + timedelta(days=int(days) - 1)
    if start and end and days is None:
        days = float((end.date() - start.date()).days + 1)
    return {
        "start": start.strftime("%Y-%m-%d") if start else "",
        "end": end.strftime("%Y-%m-%d") if end else "",
        "days": days,
        "ambiguous": ambiguous or (not start and bool(text)),
        "missing": not bool(start),
        "error": "",
    }


def parse_leave_time(text: str, start_time: str = "", end_time: str = ""):
    """返回 (start_text, end_text, days, error)，明确传入日期优先。"""
    text = (text or "").strip()
    explicit_start = _extract_dates(start_time)
    explicit_end = _extract_dates(end_time)
    reason_dates = _extract_dates(text)
    if not reason_dates:
        reason_dates = _day_of_month_dates(text)

    start = explicit_start[0] if explicit_start else (
        reason_dates[0] if reason_dates else (_relative_start(text) or datetime.now())
    )
    end = explicit_end[0] if explicit_end else (
        reason_dates[1] if len(reason_dates) > 1 else None
    )
    days = _duration_days(text)

    if end is None:
        if days is None:
            days = 1.0
        end = start if days == 0.5 else start + timedelta(days=int(days) - 1)
    window_error = validate_leave_window(
        start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    )
    if window_error:
        return "", "", 0, window_error
    if days is None:
        days = (end.date() - start.date()).days + 1
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), days, ""
