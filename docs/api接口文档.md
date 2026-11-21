# ai API 接口文档

- 服务与前端地址：`http://127.0.0.1:8000`
- 交互式文档：启动后访问 `/docs`（Swagger）或 `/redoc`
- 接口总数：**96 个业务路径、97 个业务操作**（含两个健康检查兼容路径）
- 本文档与代码同步方式：`python -c "import main; ..."` 从 `app.openapi()` 导出，改路由后请重新生成

## 通用约定

**返回体**（所有接口一致）

```json
{ "code": 200, "msg": "操作成功", "data": {} }
```

`code`：成功为 200；业务参数错误为 400；未登录为 401；权限不足为 403；不存在为 404；冲突为 409；未处理异常为 500。HTTP 状态码与 `code` 一致。

**鉴权**：登录后拿到 `token`（HMAC 签名，含 user_id、角色、token_version 与过期时间）。

- GET：只接受 `Authorization: Bearer <token>`；禁止通过 URL 查询参数传 Token
- POST/PUT body：`{ "token": "..." }`
- 服务端每次请求都会检查账号是否存在、是否停用、角色和 token_version；改密或停用后旧 token 立即失效。

标记说明：**公开** = 无需 token；**登录** = 任一有效账号；**学生** = 仅学生且数据归属自动取当前 token；**员工** = 仅 `EMPLOYEE` / `TEACHER`。

## 学籍阶段门禁（v5）

学生不是注册后就能用全部服务，阶段决定能做什么：

| 阶段 | 含义 | 已解锁 |
|---|---|---|
| `CONSULT` | 咨询期，刚注册 | 智能助手、留学方案、活动报名、心情记录 |
| `APPLYING` | 申请期，已建档 | 以上 + 申请进度时间轴 |
| `ENROLLED` | 在读，已入学 | 全部（+ 请假、成绩、售后） |
| `ALUMNI` | 已毕业，只读 | 保留查询，关闭请假与申请 |

规则：**查询类一律放行，产生单据的提交类才拦**。提交被拦时返回 `code=403`，`data` 内附完整阶段信息，前端据此渲染引导：

```json
{
  "code": 403,
  "msg": "「请假」是在读学生的服务，你目前处于「咨询期」。入学后会自动解锁，在那之前可以让助手帮你规划入学准备。",
  "data": { "stage": "CONSULT", "capabilities": { "leave": false, "chat": true }, "next_step": "..." }
}
```

阶段自动流转：员工创建申请 → `APPLYING`；申请推进到「已入学」→ `ENROLLED`；员工也可通过 `/api/content/students/stage` 手动调整（含降级、标记毕业）。心情记录属于关怀类，**不设门槛**。

---

## 1. 登录认证 `/api/auth`（18）

> 系统**无内置账号**。全库员工数为 0 时，`/setup` 是唯一能创建管理员的人口；学生走 `/register` 自助注册。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/setup-status` | 公开 | 是否还需要首次初始化，返回 `needs_setup` / `staff_count` / `require_code` |
| POST | `/setup` | 公开（仅一次性） | 创建第一个管理员（`EMPLOYEE`），密码 ≥8 位；已有员工则拒绝。若设置了 `YUEJIAO_SETUP_CODE` 需带 `setup_code` |
| POST | `/register` | 公开 | 学生注册，创建 `sys_user` 并直接返回 token |
| POST | `/login` | 公开 | 页面登录，返回 `token` + 用户资料；无密码账号一律拒绝 |
| POST | `/recovery-question` | 公开 | 根据账号读取密码找回安全问题 |
| POST | `/forgot-password` | 公开 | 校验安全问题答案并重置密码，旧 token 立即失效 |
| GET | `/me` | 登录 | 校验 token 是否有效 |
| POST | `/verify-admin` | 登录 | 后台权限校验，非员工拒绝 |
| POST | `/dify-chat` | 登录 | 代理调用 Dify，自动带 `inputs.auth_token` 与用户 id |
| POST | `/public-chat` | 公开 | 客服与活动公开聊天，走客服工作流，免登录 |
| GET | `/profile` | 登录 | 用户中心：资料 + 数据概览（请假数、成绩、未读等） |
| POST | `/change-password` | 登录 | 修改密码（旧密码必填） |
| GET | `/users` | 员工 | 按角色查用户；学生访问返回 403 |
| GET | `/staff` | 员工 | 员工账号列表（含状态、是否已设密码） |
| POST | `/staff` | 员工 | 员工账号管理：`action=create` 新建（密码 ≥8 位）/ `disable` 停用 / `enable` 启用；不能停用自己、不能停到只剩 0 个 |

```bash
# 首次初始化（同一个库只能成功一次）
curl -X POST http://127.0.0.1:8000/api/auth/setup \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-8-plus-pwd","real_name":"管理员"}'

# 之后正常登录
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-8-plus-pwd"}'
```

## 2. 学生端 `/api/student`（10）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/leave/prepare` | 学生 | Dify 请假编排准备：接收 LLM 提取字段，归一化日期、保存草稿、检查缺失和重叠；不创建正式请假单 |
| POST | `/leave/confirm` | 学生 | 消费 `prepare` 返回的一次性确认令牌，再次执行权限、日期、重叠和事务校验后才写正式请假单 |
| POST | `/leave` | 学生 | 提交请假；开始日期不得早于当天；相同或重叠的待审批申请会被拒绝 **← 需在读阶段** |
| POST | `/emotion` | 学生 | 提交情绪倾诉；支持可选外部模型标签/分值，高危时写预警并通知员工 |
| GET | `/my_leaves` | 学生 | 我的请假记录（含审批留言/驳回理由） |
| POST | `/feedback` | 学生 | 提交售后反馈并返回完整工单记录 **← 需在读阶段** |
| GET | `/my_feedbacks` | 学生 | 我的售后工单 |
| GET | `/my_scores` | 学生 | 我的成绩 |
| GET | `/stage` | 学生 | 我的学籍阶段、进度、已解锁服务与下一步指引 |
| GET | `/psych-profile` | 学生 | 我的心理画像、最近交互时间和最多 50 条情绪历史 |

## 3. 请假审批 `/api/teacher`（3）

老师与员工共用，功能已并入员工后台。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/pending-leaves` | 员工 | 待审批列表（含学生姓名） |
| GET | `/all-leaves` | 员工 | 全部请假记录 |
| POST | `/approve` | 员工 | 审批：通过可留言，**驳回必须填理由**；结果写通知 |

## 4. 员工端 `/api/employee`（12）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/search` | 员工 | 按姓名统一搜索学生档案与客户线索，并按角色数据范围返回 |
| POST | `/add_lead` | 员工 | 录入意向客户并返回新客户记录 |
| PUT | `/update_lead_status` | 员工 | 更新客户销售阶段 |
| GET | `/search_advanced` | 员工 | 按状态、负责人、关键词组合筛选 |
| POST | `/report` | 员工 | 提交日报并返回新日报记录 |
| GET | `/reports` | 员工 | 查询日报 |
| POST | `/add_score` | 员工 | 录入学生成绩并返回新成绩记录 |
| GET | `/search_scores` | 员工 | 查询成绩（可按学生/课程） |
| PUT | `/approve_leave` | 员工 | 旧版审批接口（新代码请用 `/api/teacher/approve`） |
| PUT | `/resolve_feedback` | 员工 | 解决售后工单并通知学生 |
| GET | `/feedbacks` | 员工 | 按角色数据范围查询售后工单，支持状态、国家和关键词筛选 |
| POST | `/nl2sql` | 员工 | 自然语言转 SQL 统计（内置白名单） |

## 5. 管理后台 `/api/admin`（4）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/alerts` | 员工 | 心理预警列表（含学生姓名） |
| PUT | `/handle_alert` | 员工 | 处理预警（旧版，新代码用 `/api/content/alert/handle`） |
| GET | `/leads` | 员工 | 全部客户 |
| GET | `/scores` | 员工 | 全部成绩 |

## 6. 内容管理 `/api/content`（16）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/events` | 公开 | 活动列表 |
| GET | `/events/{event_id}` | 公开 | 活动详情；不存在返回 404 |
| POST | `/events/save` | 管理员 | 新增/编辑活动 |
| POST | `/events/delete` | 管理员 | 删除活动 |
| POST | `/events/register` | 公开 | 报名（登录学生或访客填联系方式；防重复、限名额，返回报名记录） |
| GET | `/events/registrations` | 员工 | 活动报名名单（含姓名） |
| GET | `/my-registrations` | 登录 | 我报名的活动 |
| GET | `/programs` | 公开 | 留学方案列表 |
| POST | `/student/country` | 登录 | 学生设置意向国家 |
| GET | `/students-by-country` | 员工 | 按意向国家分组的学生（含学籍阶段） |
| POST | `/alert/handle` | 员工 | 处理心理预警（新版） |
| GET | `/students/list` | 员工 | 学生名单，可按阶段/关键词筛选 |
| POST | `/students/stage` | 员工 | **设置学生学籍阶段**（阶段变化自动通知学生） |
| GET | `/stages` | 公开 | 学籍阶段字典（前端下拉用） |

## 7. 客户线索 `/api/lead`（4）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/submit` | 公开 | 官网留资，自动分配给线索最少的员工并通知 |
| GET | `/list` | 员工 | 全部线索（含负责人姓名） |
| POST | `/follow-up` | 员工 | 跟进：更新阶段 + 追加记录 |
| GET | `/{lead_id}` | 员工 | 客户线索详情，按数据范围校验 |

## 8. 申请进度与案件档案 `/api/application`（13）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/options` | 公开 | 可选国家、学习模式和项目目录 |
| POST | `/apply` | 学生 | 学生主动提交就读申请，按国家/模式/项目选择，自动分配顾问 |
| POST | `/update` | 学生 | 修改尚未受理的本人申请 |
| POST | `/cancel` | 学生 | 撤销尚未受理的本人申请 |
| POST | `/create` | 员工 | 为学生创建申请 |
| POST | `/advance` | 员工 | 推进阶段（自动留痕 + 通知学生） |
| GET | `/all` | 员工 | 全部申请进度 |
| GET | `/my` | 学生 | 我的申请（含时间轴、阶段档案和材料摘要） |
| GET | `/{application_id}/detail` | 登录 | 管理端/学生端共用的完整案件档案 |
| POST | `/{application_id}/stages/{stage}/detail` | 员工 | 保存阶段结论和结构化明细 |
| POST | `/{application_id}/materials/checklist` | 员工 | 新增材料清单，可设为内部或学生可见 |
| POST | `/{application_id}/materials/upload` | 登录 | 学生当前阶段补交或员工补录材料，最大 8 MB |
| POST | `/materials/{material_id}/review` | 员工 | 材料审核通过或退回，退回必须填写原因 |
| GET | `/materials/{material_id}/download` | 登录 | 按学生归属、角色范围和可见性下载附件 |

## 9. 通知中心 `/api/notification`（3）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/list` | 登录 | 我的通知 + 未读数 |
| GET | `/unread` | 登录 | 未读数（铃铛角标轮询） |
| POST | `/read` | 登录 | 标记已读（传 id 单条，不传全部） |

## 10. 客服端 `/api/customer`（3）

早期版本，供客服工作流调用；新功能已迁至 `/api/content`。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/events` | 公开 | 查询所有活动讲座 |
| GET | `/courses` | 公开 | 查询课程项目 |
| GET | `/registrations` | 员工 | 查询旧版报名记录；新版请用 `/api/content/events/registrations` |

## 11. 共享画像推荐 `/api/recommend`（1）

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/recommend` | 公开 | 从自然语言抽取年龄、学历、意向、语言与动手能力，返回逐项目分数、理由、学制和最佳推荐 |

## 健康检查

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 公开 | 返回 `{"status":"ok"}` |
| GET | `/api/health` | 公开 | 需求规格兼容路径，返回 `{"status":"ok"}` |

---

## 与 Dify 工具的对应关系

`docs/dify_dsl/yuejiao_api_tools.yaml` 中的 52 个工具由后端 OpenAPI 生成。留学方案只有实时读取接口，没有后端写入接口。GET 工具通过 `Authorization` 头传 token，POST 工具通过 body 传 token。学生请假在 `patch_workflows.py` 后还需执行 `patch_leave_orchestration.py`，再执行 `publish_runtime.py --apply`。
