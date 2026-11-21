"""统一角色权限矩阵。

本模块只负责回答两个问题：
1. 当前身份能不能执行某类功能（功能权限）；
2. 当前身份能看到哪些学生数据（数据范围）。

接口层仍必须校验数据归属。例如“班主任能审批请假”只是功能权限，
并不代表班主任能审批其他班级学生的请假。
"""

from __future__ import annotations


# 权限码使用“资源.动作”格式，便于接口和前端共用同一份语义。
PERMISSIONS = {
    # 员工工作台
    "business.overview": "查看工作台概览",
    "report.self": "提交并查看本人日报",
    "report.read_all": "查看全部员工日报",
    "recommend.use": "使用画像推荐",

    # 学生与学籍
    "student.read": "查看负责范围内的学生",
    "student.stage.manage": "调整学生学籍阶段",
    "student.enrollment.manage": "补录负责范围内学生的入学档案",
    "graduation.manage": "审核负责范围内学生的毕业申请",

    # CRM 客户线索
    "crm.read": "查看负责范围内的客户线索",
    "crm.manage": "新增、跟进和流转负责范围内的客户线索",

    # 请假、预警与售后
    "leave.read": "查看负责范围内的请假记录",
    "leave.approve": "审批负责范围内学生的请假",
    "alert.read": "查看负责范围内的心理预警",
    "alert.handle": "处理负责范围内的心理预警",
    "feedback.resolve": "处理负责范围内学生的售后工单",

    # 成绩与申请
    "score.read": "查看负责范围内的学生成绩",
    "score.manage": "录入负责范围内的学生成绩",
    "application.read": "查看负责范围内的留学申请",
    "application.manage": "创建并推进负责范围内的留学申请",

    # 活动与系统配置
    "event_registration.read": "查看活动报名名单",
    "content.manage": "管理运营活动",
    "account.manage": "管理后台账号与员工角色",
    "system.health": "查看系统健康状态",

    # 学生本人能力
    "student.self": "使用学生本人服务",
    "application.self": "管理学生本人的就读申请",
    "notification.self": "管理学生本人的通知",
}


# “*”表示全部权限。此矩阵是后端事实源，前端菜单只作为易用性补充。
ROLE_CAPABILITIES = {
    "ADMIN": {"*"},
    "LEADER": {
        "business.overview",
        "report.self",
        "report.read_all",
        "recommend.use",
        "student.read",
        "student.stage.manage",
        "student.enrollment.manage",
        "graduation.manage",
        "crm.read",
        "crm.manage",
        "leave.read",
        "leave.approve",
        "alert.read",
        "alert.handle",
        "feedback.resolve",
        "score.read",
        "score.manage",
        "application.read",
        "application.manage",
        "event_registration.read",
    },
    "ADVISOR": {
        "business.overview",
        "report.self",
        "recommend.use",
        "student.read",
        "alert.read",
        "feedback.resolve",
        "crm.read",
        "crm.manage",
        "score.read",
        "score.manage",
        "application.read",
        "application.manage",
        "event_registration.read",
    },
    "HOMEROOM": {
        "business.overview",
        "report.self",
        "recommend.use",
        "student.read",
        "student.enrollment.manage",
        "graduation.manage",
        "leave.read",
        "leave.approve",
        "alert.read",
        "alert.handle",
        "feedback.resolve",
        "score.read",
        "score.manage",
        "application.read",
    },
    "MAINTAINER": {
        "system.health",
    },
    "STUDENT": {
        "student.self",
        "application.self",
        "notification.self",
        "recommend.use",
    },
}


def effective_role(user) -> str:
    """把账号类型与员工子角色合并成一个权限角色。

    管理员和维护人员直接按账号类型判断；业务员工再读取 staff_role。
    如果老数据没有 staff_role，则按权限最小的普通顾问处理，避免误授权。
    """
    if not user:
        return ""
    # 管理员演示模式可以携带明确的视图角色。它是权限判断的最高优先级，
    # 因此切换到顾问/班主任后不会继续享受管理员通配权限。
    view_role = str(user.get("view_role") or "").upper()
    if view_role in ("ADMIN", "LEADER", "ADVISOR", "HOMEROOM", "MAINTAINER"):
        return view_role
    user_type = str(user.get("user_type") or "").upper()
    if user_type in ("ADMIN", "MAINTAINER", "STUDENT"):
        return user_type
    subrole = str(user.get("staff_role") or "ADVISOR").upper()
    return subrole if subrole in ("ADVISOR", "HOMEROOM", "LEADER") else "ADVISOR"


def capabilities_for(user) -> list[str]:
    """返回当前用户拥有的权限码，供登录响应和前端菜单共同使用。"""
    role = effective_role(user)
    caps = ROLE_CAPABILITIES.get(role, set())
    if "*" in caps:
        return ["*"] + sorted(PERMISSIONS.keys())
    return sorted(caps)


def has_capability(user, permission: str) -> bool:
    """判断用户是否拥有某个权限码。"""
    caps = set(capabilities_for(user))
    return "*" in caps or permission in caps


def student_scope(user, alias: str = "u") -> tuple[str, list]:
    """返回学生数据范围的 SQL 片段与参数。

    - 管理员、领导：全部学生；
    - 班主任：本人担任班主任的学生；
    - 顾问及其他普通业务员工：本人负责的学生。

    只允许传固定表别名，不接受前端输入，避免 SQL 注入。
    """
    role = effective_role(user)
    if role in ("ADMIN", "LEADER"):
        return "", []
    if role == "HOMEROOM":
        return f" AND {alias}.homeroom_id=?", [user["id"]]
    return f" AND {alias}.advisor_id=?", [user["id"]]
