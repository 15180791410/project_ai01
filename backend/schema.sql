-- 1. 统一用户表
CREATE TABLE IF NOT EXISTS sys_user (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 用户唯一编号，自动增长
    username TEXT NOT NULL,               -- 登录账号，不能为空
    real_name TEXT,                       -- 真实姓名
    user_type TEXT,                       -- 用户类型：学生(STUDENT)/员工(EMPLOYEE)/客户(CUSTOMER)
    contact_info TEXT,                    -- 手机号或者邮箱
    status TEXT DEFAULT '正常',            -- 账号状态，默认是正常
    password_hash TEXT,                   -- 密码哈希（PBKDF2）
    target_country TEXT,                  -- 学生意向留学国家
    lifecycle_stage TEXT DEFAULT 'CONSULT', -- 学籍阶段：CONSULT咨询期/APPLYING申请期/ENROLLED在读/ALUMNI已毕业
    token_version INTEGER DEFAULT 0,      -- 密码变更后递增，使旧令牌立即失效
    staff_role TEXT,                      -- 员工子角色：ADVISOR顾问/HOMEROOM班主任/LEADER领导
    department TEXT,                      -- 所属部门
    manager_id INTEGER,                   -- 直属负责人
    advisor_id INTEGER,                   -- 学生归属顾问
    homeroom_id INTEGER,                  -- 学生归属班主任
    security_question TEXT,               -- 找回密码用安全问题
    security_answer_hash TEXT             -- 安全问题答案的加盐哈希
);

-- 2. 学生行政服务表（请假等） 
CREATE TABLE IF NOT EXISTS student_admin_service (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 请假单编号
    student_id INTEGER NOT NULL,          -- 哪个学生请的假
    service_type TEXT NOT NULL,           -- 业务类型，目前固定是"请假"（必填，不能为空）
    start_time TEXT,                      -- 开始日期
    end_time TEXT,                        -- 结束日期
    reason TEXT,                          -- 请假理由（原话）
    status TEXT DEFAULT '待审批',          -- 状态：待审批/已通过/已驳回
    approver_id INTEGER,                  -- 审批人的ID
    approver_comment TEXT,                -- 审批留言 / 驳回理由
    approve_time TEXT,                    -- 审批时间
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP -- 提交时间，自动获取当前时间
);

-- 3. 心理健康画像表
CREATE TABLE IF NOT EXISTS student_psych_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER UNIQUE NOT NULL,   -- 学生编号（UNIQUE代表一个学生只有一条记录）
    latest_emotion_tag TEXT,              -- 最新情绪标签（高危/关注/正常）
    emotion_score INTEGER,                -- 最新情绪分数（0-100）
    emotion_history TEXT,                 -- 历史情绪记录（存成JSON字符串，保留最近50条）
    last_interaction_time TEXT            -- 最近一次情绪交互时间
);

-- 4. 心理预警表
CREATE TABLE IF NOT EXISTS student_psych_alert (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,          -- 哪个学生触发了预警
    trigger_reason TEXT,                  -- 触发原因（他原话说了什么）
    risk_level TEXT,                      -- 危险等级（高/中/低）
    emotion_score INTEGER,                -- 触发本次预警的情绪分值（0-100）
    status TEXT DEFAULT '未处理',          -- 老师处理状态（未处理/已解决）
    handler_id INTEGER,                   -- 处理人ID
    handle_note TEXT,                     -- 处理备注
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 5. 售后反馈工单表
CREATE TABLE IF NOT EXISTS student_feedback_ticket (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,          -- 谁投诉的
    content TEXT NOT NULL,                -- 投诉摘要
    detail TEXT,                          -- 详细描述
    solution TEXT,                        -- 【新增修复】：解决方案
    status TEXT DEFAULT '待处理',          -- 处理状态
    is_notified INTEGER DEFAULT 0,        -- 是否已通知学生（0否，1是）
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 6. 意向客户表
CREATE TABLE IF NOT EXISTS crm_lead (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,          -- 客户姓名
    contact_info TEXT,                    -- 联系方式
    background_info TEXT,                 -- 客户背景
    target_country TEXT,                  -- 客户意向国家
    follow_up_history TEXT,               -- 跟进历史
    status TEXT DEFAULT '新增意向',        -- 销售阶段
    owner_employee_id INTEGER NOT NULL,   -- 负责员工ID
    source_event_id INTEGER,              -- 首次获取该线索的活动ID；活动报名自动转入时写入
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time TEXT                      -- 最近一次跟进或资料更新时间
);

-- 7. 员工日报表
CREATE TABLE IF NOT EXISTS employee_daily_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,         -- 哪个员工写的
    report_date TEXT NOT NULL,            -- 日报日期
    content TEXT NOT NULL,                -- 日报内容
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 8. 学生成绩表
CREATE TABLE IF NOT EXISTS student_score (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,          -- 哪个学生的成绩
    course_name TEXT NOT NULL,            -- 课程名称
    score REAL NOT NULL,                  -- 分数（支持小数）
    semester TEXT                         -- 学期
);

-- 9. 活动与讲座表
CREATE TABLE IF NOT EXISTS event_lecture (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name TEXT NOT NULL,             -- 活动名称
    event_type TEXT,                      -- 活动类型：宣讲会/展会/讲座/其他
    start_time TEXT NOT NULL,             -- 活动开始时间
    location TEXT,                        -- 地点
    country TEXT,                         -- 关联国家
    description TEXT,                     -- 活动简介
    audience_scope TEXT DEFAULT 'PUBLIC', -- PUBLIC/STUDENT/EMPLOYEE/ALL
    max_participants INTEGER,             -- 最多能来多少人
    current_participants INTEGER DEFAULT 0, -- 现在已经报了多少人
    create_time TEXT                      -- 创建时间
);

-- 11. 活动报名表
CREATE TABLE IF NOT EXISTS event_registration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,            -- 报名的哪个活动
    customer_id INTEGER NOT NULL,         -- 谁报的名
    status TEXT DEFAULT '已报名',          -- 状态
    contact_info TEXT,                    -- 报名时留下的手机号或邮箱
    target_country TEXT,                  -- 报名时填写的意向国家
    lead_id INTEGER,                      -- 自动转入或关联的 CRM 客户线索ID
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 11.1 活动报名草稿（免登录访客先选场次，再补充姓名和联系方式）
CREATE TABLE IF NOT EXISTS event_registration_draft (
    visitor_key TEXT PRIMARY KEY,        -- 浏览器访客标识
    event_id INTEGER NOT NULL,
    event_name TEXT,
    customer_name TEXT,
    contact_info TEXT,
    target_country TEXT,
    update_time TEXT
);

-- 12. 对话记录表
CREATE TABLE IF NOT EXISTS conversation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,             -- 谁聊的
    agent_type TEXT,                      -- 跟哪个机器人聊的
    user_input TEXT,                      -- 用户说了什么
    ai_output TEXT,                       -- AI回复了什么
    emotion_tag TEXT,                     -- 本次对话的情绪标签
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 13. 留学申请进度表（学生申请时间轴：选校→材料→递交→offer→签证→入学）
CREATE TABLE IF NOT EXISTS application_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,          -- 哪个学生的申请
    country TEXT,                         -- 申请国家
    university TEXT,                      -- 目标院校
    program TEXT,                         -- 目标专业
    stage TEXT DEFAULT '选校定位',         -- 当前阶段
    stage_detail TEXT,                    -- 当前阶段说明
    status TEXT DEFAULT '进行中',          -- 进行中/已完成/已暂停
    operator_id INTEGER,                  -- 最近更新的员工
    initiator_type TEXT DEFAULT 'STAFF',  -- STAFF员工建档 / STUDENT学生主动申请
    application_mode TEXT,                -- 学生选择的学习模式/项目类别
    student_remark TEXT,                  -- 学生申请说明
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 14. 申请进度历史表（每次阶段变更留痕，形成可追溯时间轴）
CREATE TABLE IF NOT EXISTS application_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,      -- 关联申请
    stage TEXT,                           -- 到达的阶段
    note TEXT,                            -- 本阶段说明
    operator_id INTEGER,                  -- 操作人
    action TEXT,                          -- 做了什么：创建/推进/修改/上传/审核
    source_type TEXT DEFAULT 'MANUAL',    -- MANUAL真实操作 / BACKFILL历史回填 / SYSTEM系统联动
    related_material_id INTEGER,          -- 关联材料
    detail_json TEXT,                     -- 本次操作的结构化变更快照
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 14.1 申请阶段档案：每个阶段保存结构化明细，不只保存一个“已完成”状态。
CREATE TABLE IF NOT EXISTS application_stage_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    status TEXT DEFAULT '待进行',          -- 待进行/进行中/已完成/待补充
    summary TEXT,                         -- 阶段结论或当前处理说明
    detail_json TEXT,                     -- 国家、院校、面试、Offer、签证等结构化明细
    started_at TEXT,
    completed_at TEXT,
    source_type TEXT DEFAULT 'MANUAL',    -- MANUAL / BACKFILL / SYSTEM
    operator_id INTEGER,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(application_id, stage)
);

-- 14.2 申请材料：清单、附件元数据、审核状态和版本。
-- 文件正文存放在 backend/uploads/application_materials，不写入 SQLite。
CREATE TABLE IF NOT EXISTS application_material (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL,
    stage TEXT NOT NULL,
    material_name TEXT NOT NULL,
    required_flag INTEGER DEFAULT 0,
    status TEXT DEFAULT '待提交',          -- 待提交/待审核/已通过/已退回
    file_name TEXT,
    storage_name TEXT,
    mime_type TEXT,
    file_size INTEGER DEFAULT 0,
    content_note TEXT,
    review_note TEXT,
    uploaded_by INTEGER,
    reviewed_by INTEGER,
    version INTEGER DEFAULT 1,
    visible_to_student INTEGER DEFAULT 1,
    is_active INTEGER DEFAULT 1,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 15. 站内通知表（审批结果、预警处理、活动报名、进度更新等主动推送给学生）
CREATE TABLE IF NOT EXISTS notification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,             -- 接收人
    type TEXT,                            -- 类型：审批/预警/活动/申请/系统
    title TEXT,                           -- 标题
    content TEXT,                         -- 内容
    is_read INTEGER DEFAULT 0,            -- 0未读 1已读
    link_page TEXT,                       -- 点击后跳转的页面锚点
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 16. 入学档案：把申请项目转成个人培养计划快照，并计算预计毕业日期。
-- 知识库内容只在入学时快照保存，后续知识库变化不能反向改写历史学生。
CREATE TABLE IF NOT EXISTS student_enrollment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER UNIQUE NOT NULL,
    application_id INTEGER,
    country TEXT,
    university TEXT,
    program TEXT,
    study_mode TEXT,
    enrollment_date TEXT,
    standard_duration_months INTEGER,
    duration_text TEXT,
    expected_graduation_date TEXT,
    actual_graduation_date TEXT,
    status TEXT DEFAULT 'ACTIVE',
    source_document TEXT,
    snapshot_json TEXT,
    completeness_status TEXT DEFAULT 'COMPLETE',
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 17. 生命周期流水：只追加不覆盖，保证每次阶段变化有日期、原因和操作人。
CREATE TABLE IF NOT EXISTS student_lifecycle_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    from_stage TEXT,
    to_stage TEXT NOT NULL,
    effective_date TEXT,
    reason TEXT,
    operator_id INTEGER,
    operator_role TEXT,
    source_type TEXT DEFAULT 'MANUAL',
    related_id INTEGER,
    snapshot_json TEXT,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 18. 毕业审核：毕业是审批结果，不是员工直接改一个下拉框。
CREATE TABLE IF NOT EXISTS student_graduation_request (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    enrollment_id INTEGER,
    graduation_date TEXT,
    completion_summary TEXT,
    document_ref TEXT,
    status TEXT DEFAULT '待审核',
    student_note TEXT,
    reviewer_id INTEGER,
    review_note TEXT,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    review_time TEXT
);

-- 19. 智能助手请假草稿：保存多轮补齐和确认状态，避免模型猜测后直接写库。
CREATE TABLE IF NOT EXISTS leave_request_draft (
    student_id INTEGER PRIMARY KEY,
    leave_type TEXT,
    start_date TEXT,
    end_date TEXT,
    days REAL,
    reason TEXT,
    state TEXT DEFAULT 'COLLECTING',
    original_text TEXT,
    confirmation_token TEXT,
    confirmation_expires_at TEXT,
    request_version INTEGER DEFAULT 0,
    update_time TEXT
);

-- 20. 通用业务动作草稿：模糊输入先补字段、消歧和确认，确认令牌只用于最终执行。
CREATE TABLE IF NOT EXISTS business_action_draft (
    draft_key TEXT PRIMARY KEY,
    actor_id INTEGER,
    actor_type TEXT,
    action_type TEXT NOT NULL,
    state TEXT DEFAULT 'COLLECTING',
    slots_json TEXT,
    candidates_json TEXT,
    missing_json TEXT,
    confirmation_token TEXT,
    confirmation_expires_at TEXT,
    update_time TEXT
);

-- 21. 通用业务动作最近一次执行结果：供匿名客服聊天回传 changed / 报名编号，
-- 结果由 draft_key 隔离，下一次新草稿会覆盖，不保存任何知识库正文。
CREATE TABLE IF NOT EXISTS business_action_result (
    draft_key TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    result_json TEXT NOT NULL,
    update_time TEXT NOT NULL
);

