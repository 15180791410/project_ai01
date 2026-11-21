/* ============================================================
   ai智能平台 - 公共工具
   ============================================================ */

// 默认同源访问 FastAPI；使用独立静态服务器 8080 时改为访问 8000 后端。
// 也可以通过 window.YJ_API_BASE 或 localStorage.yj_api_base 精确覆盖。
const YJ_API_OVERRIDE = window.YJ_API_BASE || localStorage.getItem("yj_api_base") || "";
const API_BASE = YJ_API_OVERRIDE || (
    (location.protocol === "http:" || location.protocol === "https:")
        ? (location.port === "8080" ? "http://127.0.0.1:8000" : location.origin)
        : "http://127.0.0.2:8000"
);

// ---- 会话管理 ----
const Session = {
    KEY: "yj_user",
    get() {
        const raw = sessionStorage.getItem(this.KEY);
        if (!raw) return null;
        try { return JSON.parse(raw); } catch (e) { return null; }
    },
    set(user) { sessionStorage.setItem(this.KEY, JSON.stringify(user)); },
    clear() { sessionStorage.removeItem(this.KEY); },
    // 要求已登录；否则回官网重新选择入口（各页面可传自己的登录页）
    require(redirect) {
        const u = this.get();
        if (!u) { location.href = redirect || "index.html"; return null; }
        return u;
    }
};

// ---- 请求封装 ----
function authHeaders(extra) {
    const headers = Object.assign({}, extra || {});
    const user = (typeof Session !== "undefined") ? Session.get() : null;
    if (user && user.token) headers.Authorization = "Bearer " + user.token;
    return headers;
}

async function apiPost(path, body) {
    const res = await fetch(API_BASE + path, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body)
    });
    const data = await res.json();
    data.http_status = res.status;
    return data;
}

async function apiPut(path, body) {
    const res = await fetch(API_BASE + path, {
        method: "PUT",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body)
    });
    const data = await res.json();
    data.http_status = res.status;
    return data;
}

async function apiGet(path) {
    const res = await fetch(API_BASE + path, { headers: authHeaders() });
    const data = await res.json();
    data.http_status = res.status;
    return data;
}

// ---- 轻量 Markdown 渲染 ----
// 助手回复天然带 ### / ** / - 这类符号，直接放进 textContent 会原样露出来，读着很不舒服。
// 这里做一层"够用就好"的渲染：先整体转义防注入，再按行解析块级结构，最后处理行内标记。
// 不依赖任何第三方库，离线也能跑。
function renderMarkdown(src) {
    if (src === null || src === undefined) return "";
    var text = String(src).replace(/\r\n?/g, "\n");

    // 先抽出围栏代码块，避免内部内容被当成 markdown 解析
    var codes = [];
    text = text.replace(/```[a-zA-Z0-9+#._-]*[ \t]*\n?([\s\S]*?)```/g, function (_, body) {
        codes.push(body.replace(/\n+$/, ""));
        return "\u0000C" + (codes.length - 1) + "\u0000";
    });

    function e(s) {
        return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    // 行内标记：粗体 / 斜体 / 删除线 / 行内代码 / 链接
    function inline(s) {
        var t = e(s);
        t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
        t = t.replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>");
        t = t.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
        t = t.replace(/(^|[\s(（【])_{1,2}([^_\n]+)_{1,2}(?=[\s)）】。，,！!？?：:]|$)/g, "$1<strong>$2</strong>");
        t = t.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
        t = t.replace(/(^|[\s(（【])[*_]([^*_\n]+)[*_](?=[\s)）】。，,！!？?：:]|$)/g, "$1<em>$2</em>");
        t = t.replace(/\[([^\]\n]+)\]\((https?:[^)\s]+)\)/g,
            '<a href="$2" target="_blank" rel="noopener">$1</a>');
        return t;
    }

    var RE_UL = /^[-*·•+](?:\s+|(?=\*\*))(.*)$/;
    var RE_OL = /^\d{1,2}[.)]\s+(.*)$/;
    var RE_H = /^(#{1,6})\s+(.+?)\s*#*$/;

    function isTableSep(s) {
        var t = (s || "").trim();
        return t.indexOf("-") >= 0 && /^\|?[\s:|-]+\|[\s:|-]*$/.test(t);
    }

    var lines = text.split("\n");
    var html = [];
    var i = 0;

    while (i < lines.length) {
        var line = lines[i];
        var tr = line.trim();
        if (!tr) { i++; continue; }

        // 代码块占位
        var mc = tr.match(/^\u0000C(\d+)\u0000$/);
        if (mc) {
            html.push('<pre class="md-code"><code>' + e(codes[+mc[1]]) + "</code></pre>");
            i++; continue;
        }

        // 标题
        var mh = tr.match(RE_H);
        if (mh) {
            var tag = mh[1].length <= 2 ? "h4" : "h5";
            html.push("<" + tag + ' class="md-h">' + inline(mh[2]) + "</" + tag + ">");
            i++; continue;
        }

        // 分割线
        if (/^(-{3,}|\*{3,}|_{3,})$/.test(tr)) { html.push('<hr class="md-hr">'); i++; continue; }

        // 表格：当前行含 |，且下一行是分隔行
        if (tr.indexOf("|") >= 0 && i + 1 < lines.length && isTableSep(lines[i + 1])) {
            var head = tr.replace(/^\||\|$/g, "").split("|");
            var rows = [];
            i += 2;
            while (i < lines.length && lines[i].trim() && lines[i].trim().indexOf("|") >= 0) {
                rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|"));
                i++;
            }
            html.push('<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
                head.map(function (c) { return "<th>" + inline(c.trim()) + "</th>"; }).join("") +
                "</tr></thead><tbody>" +
                rows.map(function (r) {
                    return "<tr>" + r.map(function (c) { return "<td>" + inline(c.trim()) + "</td>"; }).join("") + "</tr>";
                }).join("") +
                "</tbody></table></div>");
            continue;
        }

        // 引用
        if (/^>\s?/.test(tr)) {
            var qs = [];
            while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
                qs.push(lines[i].replace(/^\s*>\s?/, "")); i++;
            }
            html.push('<blockquote class="md-quote">' + inline(qs.join(" ")) + "</blockquote>");
            continue;
        }

        // 无序列表
        if (RE_UL.test(tr) && !/^\*{2}/.test(tr)) {
            var uls = [];
            while (i < lines.length && RE_UL.test(lines[i].trim())) {
                uls.push(lines[i].trim().replace(RE_UL, "$1")); i++;
            }
            html.push('<ul class="md-ul">' + uls.map(function (t) {
                return "<li>" + inline(t) + "</li>";
            }).join("") + "</ul>");
            continue;
        }

        // 有序列表
        if (RE_OL.test(tr)) {
            var ols = [];
            while (i < lines.length && RE_OL.test(lines[i].trim())) {
                ols.push(lines[i].trim().replace(RE_OL, "$1")); i++;
            }
            html.push('<ol class="md-ol">' + ols.map(function (t) {
                return "<li>" + inline(t) + "</li>";
            }).join("") + "</ol>");
            continue;
        }

        // 普通段落：一直吃到空行或下一个块级标记
        var para = [tr];
        i++;
        while (i < lines.length) {
            var nx = lines[i].trim();
            if (!nx || RE_H.test(nx) || RE_UL.test(nx) || RE_OL.test(nx) ||
                /^>/.test(nx) || /^\u0000C\d+\u0000$/.test(nx) ||
                /^(-{3,}|\*{3,}|_{3,})$/.test(nx)) break;
            para.push(nx); i++;
        }
        html.push('<p class="md-p">' + para.map(inline).join("<br>") + "</p>");
    }

    return html.join("");
}

// ---- 聊天消息渲染 ----
function appendMsg(boxId, who, text) {
    const box = document.getElementById(boxId);
    if (!box) return;
    const isUser = who === "你";
    const wrap = document.createElement("div");
    wrap.className = "msg " + (isUser ? "user" : "ai");
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = isUser ? "我" : "粤";
    const content = document.createElement("div");
    content.className = "content";
    const whoDiv = document.createElement("div");
    whoDiv.className = "who";
    whoDiv.textContent = who;
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (isUser) {
        // 用户输入按纯文本显示，避免把 * 号之类当成格式
        bubble.textContent = text;
    } else {
        bubble.className = "bubble md";
        bubble.innerHTML = renderMarkdown(text);
    }
    content.appendChild(whoDiv);
    content.appendChild(bubble);
    wrap.appendChild(avatar);
    wrap.appendChild(content);
    box.appendChild(wrap);
    box.scrollTop = box.scrollHeight;
}

// ---- HTML 转义（展示后端数据时防注入） ----
function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
}

// ---- 访客标识（客服免登录聊天用） ----
function getVisitorId() {
    let v = sessionStorage.getItem("yj_visitor");
    if (!v) {
        v = "guest_" + Math.random().toString(36).slice(2, 10);
        sessionStorage.setItem("yj_visitor", v);
    }
    return v;
}

// ---- 通用：Enter 发送 ----
function bindEnter(inputId, fn) {
    const el = document.getElementById(inputId);
    if (!el) return;
    el.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); fn(); }
    });
}

/* ============================================================
   企业级公共组件（v2）
   ============================================================ */

// ---- Toast 轻提示 ----
function toast(msg, type) {
    let box = document.getElementById("toastBox");
    if (!box) {
        box = document.createElement("div");
        box.id = "toastBox";
        document.body.appendChild(box);
    }
    const t = document.createElement("div");
    t.className = "toast" + (type ? " " + type : "");
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(function () {
        t.style.opacity = "0";
        t.style.transition = "opacity .3s";
        setTimeout(function () { t.remove(); }, 300);
    }, 3200);
}

// ---- 确认弹窗（Promise） ----
function confirmBox(title, message) {
    return new Promise(function (resolve) {
        const mask = document.createElement("div");
        mask.className = "modal-mask";
        mask.innerHTML =
            '<div class="modal-box"><h3>' + esc(title) + '</h3>' +
            '<div style="color:#6b7280;font-size:14px;">' + esc(message) + '</div>' +
            '<div class="modal-actions">' +
            '<button class="btn-sm-outline" id="mbCancel">取消</button>' +
            '<button id="mbOk">确定</button></div></div>';
        document.body.appendChild(mask);
        mask.querySelector("#mbCancel").onclick = function () { mask.remove(); resolve(false); };
        mask.querySelector("#mbOk").onclick = function () { mask.remove(); resolve(true); };
        mask.onclick = function (e) { if (e.target === mask) { mask.remove(); resolve(false); } };
    });
}

// ---- 输入弹窗（Promise，返回输入的文本；取消返回 null）----
// opts: {title, message, placeholder, required, multiline}
function promptBox(title, message, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
        const mask = document.createElement("div");
        mask.className = "modal-mask";
        const field = opts.multiline
            ? '<textarea id="pbInput" rows="4" placeholder="' + esc(opts.placeholder || "") + '"></textarea>'
            : '<input type="' + esc(opts.inputType || "text") + '" id="pbInput" placeholder="' + esc(opts.placeholder || "") + '">';
        mask.innerHTML =
            '<div class="modal-box"><h3>' + esc(title) + '</h3>' +
            (message ? '<div style="color:#6b7280;font-size:14px;margin-bottom:12px;">' + esc(message) + '</div>' : '') +
            field +
            '<div id="pbErr" class="form-msg msg-err"></div>' +
            '<div class="modal-actions">' +
            '<button class="btn-sm-outline" id="pbCancel">取消</button>' +
            '<button id="pbOk">确定</button></div></div>';
        document.body.appendChild(mask);
        const input = mask.querySelector("#pbInput");
        if (input) setTimeout(function () { input.focus(); }, 50);
        function close(val) { mask.remove(); resolve(val); }
        mask.querySelector("#pbCancel").onclick = function () { close(null); };
        mask.querySelector("#pbOk").onclick = function () {
            const val = (input.value || "").trim();
            if (opts.required && !val) {
                mask.querySelector("#pbErr").innerText = "此处必填，请填写后继续";
                return;
            }
            close(val);
        };
        mask.onclick = function (e) { if (e.target === mask) close(null); };
    });
}

// ---- 权限守卫：要求指定角色，否则跳回首页 ----
function requireRole(allowed) {
    const u = Session.get();
    const roles = Array.isArray(allowed) ? allowed : [allowed];
    if (!u || roles.indexOf(u.user_type) < 0) {
        Session.clear();
        location.href = "index.html";
        return null;
    }
    return u;
}

// ---- 工作台侧边栏渲染 ----
// items: [{id, icon, label, badge}]  groups 用 {group:'分组名'}
function renderWorkspaceSidebar(opts) {
    const el = document.getElementById("wsSide");
    if (!el) return;
    const u = opts.user;
    const initial = (u.real_name || u.username || "?").slice(0, 1);
    let html =
        '<div class="ws-profile">' +
        '<div class="ws-avatar">' + esc(initial) + '</div>' +
        '<div><div class="pname">' + esc(u.real_name || u.username) + '</div>' +
        '<div class="prole">' + esc(opts.roleLabel || "") + '</div></div></div>';
    opts.items.forEach(function (it) {
        if (it.group) { html += '<div class="nav-group">' + esc(it.group) + '</div>'; return; }
        html += '<div class="nav-item' + (it.locked ? ' locked' : '') + '" data-page="' + it.id + '"' +
            (it.locked ? ' data-lock-tip="' + esc(it.lockTip || "该模块尚未解锁") + '"' : '') +
            ' role="button" tabindex="0">' +
            '<span class="ni-icon">' + it.icon + '</span><span class="ni-label">' + esc(it.label) + '</span>' +
            (it.locked ? '<span class="ni-lock">🔒</span>' : '') +
            (it.badge ? '<span class="ni-badge" id="badge-' + it.id + '">' + it.badge + '</span>' : '') +
            '</div>';
    });
    html += '<div class="ws-foot">' + esc(opts.foot || "ai智能平台") + '</div>';
    el.innerHTML = html;

    // 使用正式事件监听，不再依赖动态生成的内联 onclick。
    // 页面可以传入 onNavigate 做角色权限/学籍阶段二次校验。
    el.querySelectorAll(".nav-item[data-page]").forEach(function (item) {
        const pageId = item.getAttribute("data-page");
        function navigate() {
            if (typeof opts.onNavigate === "function") opts.onNavigate(pageId);
            else switchPage(pageId);
        }
        item.addEventListener("click", navigate);
        item.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                navigate();
            }
        });
    });
}

// ---- 工作台页面切换 ----
function switchPage(id) {
    // 锁定模块：不切页，交给页面自己的 onLockedPage 展示解锁说明
    const target = document.querySelector('.ws-side .nav-item[data-page="' + id + '"]');
    if (target && target.classList.contains("locked")) {
        const tip = target.getAttribute("data-lock-tip") || "该模块尚未解锁";
        if (typeof window.onLockedPage === "function") { window.onLockedPage(id, tip); }
        else { toast(tip, "err"); }
        return;
    }
    document.querySelectorAll(".ws-page").forEach(function (p) { p.classList.remove("active"); });
    document.querySelectorAll(".ws-side .nav-item").forEach(function (n) { n.classList.remove("active"); });
    const page = document.getElementById("page-" + id);
    if (page) page.classList.add("active");
    const nav = document.querySelector('.ws-side .nav-item[data-page="' + id + '"]');
    if (nav) nav.classList.add("active");
    if (typeof window.onPageShow === "function") window.onPageShow(id);
}

// ---- 用户中心：渲染资料卡 + 数据概览 + 修改密码表单 ----
async function renderUserCenter(containerId, user) {
    const box = document.getElementById(containerId);
    if (!box) return;
    let profile = null;
    try { profile = await apiGet("/api/auth/profile"); } catch (e) {}
    const data = (profile && profile.code === 200) ? profile.data : null;
    const info = data || {
        id: user.user_id, username: user.username, real_name: user.real_name,
        user_type: user.user_type, role_label: user.role_label,
        contact_info: "—", status: "正常", stats: {}
    };
    const initial = (info.real_name || info.username || "?").slice(0, 1);
    let statsHtml = "";
    const stats = info.stats || {};
    const statKeys = Object.keys(stats);
    if (statKeys.length) {
        statsHtml = '<div class="overview-grid">' + statKeys.map(function (k) {
            return '<div class="ov-card"><div class="ov-ico ico-blue">📊</div>' +
                '<div><div class="ov-num">' + esc(stats[k]) + '</div>' +
                '<div class="ov-label">' + esc(k) + '</div></div></div>';
        }).join("") + '</div>';
    }
    const stageHtml = info.lifecycle ? '<div id="ucStage"></div>' : "";
    const stageRow = info.lifecycle
        ? '<div class="info-item"><div class="k">学籍阶段</div><div class="v">' + esc(info.lifecycle.stage_label) +
          ' <span class="stage-tag st-' + String(info.lifecycle.stage).toLowerCase() + '">已解锁 ' +
          Object.keys(info.lifecycle.capabilities || {}).filter(function (k) { return info.lifecycle.capabilities[k]; }).length + ' 项</span></div></div>'
        : "";
    const accountSecurityHtml = user.is_role_preview
        ? '<div class="panel"><div class="panel-title">演示角色视图</div>' +
          '<p class="panel-desc">当前正在以其他角色查看后台，不能修改该代表账号的密码或安全问题。切回管理员视图后可管理真实账号。</p></div>'
        : '<div class="panel"><div class="panel-title">账户安全 · 修改密码</div>' +
          '<p class="panel-desc">修改密码后立即生效，下次登录请使用新密码。</p>' +
          '<div class="form-grid">' +
          '<div><label>原密码</label><input type="password" id="ucOldPw" placeholder="请输入当前密码"></div>' +
          '<div><label>新密码</label><input type="password" id="ucNewPw" placeholder="至少 6 位"></div>' +
          '<div class="full"><label>确认新密码</label><input type="password" id="ucNewPw2" placeholder="再次输入新密码"></div>' +
          '</div>' +
          '<div class="form-actions"><button onclick="doChangePassword()">保存修改</button></div>' +
          '<div id="ucPwMsg" class="form-msg"></div></div>' +
          '<div class="panel"><div class="panel-title">密码找回问题</div>' +
          '<div class="form-grid"><div><label>安全问题</label><select id="ucQuestion"><option>你的出生城市是？</option><option>你的第一所学校是？</option></select></div>' +
          '<div><label>答案</label><input id="ucAnswer" placeholder="请牢记，用于找回密码"></div></div>' +
          '<div class="form-actions"><button onclick="saveSecurityQuestion()">保存找回问题</button></div>' +
          '<div id="ucSecurityMsg" class="form-msg"></div></div>';
    box.innerHTML =
        '<div class="uc-profile-card">' +
        '<div class="uc-avatar">' + esc(initial) + '</div>' +
        '<div><div class="uc-name">' + esc(info.real_name || info.username) + '</div>' +
        '<div class="uc-meta">账号：' + esc(info.username) + ' · 联系方式：' + esc(info.contact_info || "未填写") + '</div></div>' +
        '<div class="uc-role">' + esc(info.role_label || info.user_type) + '</div></div>' +
        stageHtml +
        statsHtml +
        '<div class="panel"><div class="panel-title">基本资料</div>' +
        '<div class="info-list">' +
        '<div class="info-item"><div class="k">用户 ID</div><div class="v">' + esc(info.id) + '</div></div>' +
        '<div class="info-item"><div class="k">姓名</div><div class="v">' + esc(info.real_name || "—") + '</div></div>' +
        stageRow +
        '<div class="info-item"><div class="k">账号状态</div><div class="v">' + esc(info.status || "正常") + '</div></div>' +
        '<div class="info-item"><div class="k">账户类型</div><div class="v">' + esc(info.role_label || info.user_type) + '</div></div>' +
        '</div></div>' +
        accountSecurityHtml;
    if (info.lifecycle) renderStageTimeline("ucStage", info.lifecycle);
}

// ---- 执行修改密码 ----
async function doChangePassword() {
    const u = Session.get();
    if (!u) return;
    const old_password = (document.getElementById("ucOldPw") || {}).value || "";
    const new_password = (document.getElementById("ucNewPw") || {}).value || "";
    const confirm = (document.getElementById("ucNewPw2") || {}).value || "";
    const msg = document.getElementById("ucPwMsg");
    if (new_password !== confirm) { if (msg) { msg.className = "form-msg msg-err"; msg.innerText = "两次输入的新密码不一致"; } return; }
    try {
        const d = await apiPost("/api/auth/change-password", { token: u.token, old_password, new_password });
        if (msg) { msg.className = "form-msg " + (d.code === 200 ? "msg-ok" : "msg-err"); msg.innerText = d.msg || ""; }
        toast(d.msg || "", d.code === 200 ? "ok" : "err");
        if (d.code === 200) {
            document.getElementById("ucOldPw").value = "";
            document.getElementById("ucNewPw").value = "";
            document.getElementById("ucNewPw2").value = "";
            const relogin = u.user_type === "STUDENT" ? "student.html" : "admin.html";
            Session.clear();
            setTimeout(function () { location.href = relogin; }, 800);
        }
    } catch (e) { toast("无法连接后端服务", "err"); }
}

async function saveSecurityQuestion() {
    const u = Session.get();
    const security_question = (document.getElementById("ucQuestion") || {}).value || "";
    const security_answer = (document.getElementById("ucAnswer") || {}).value || "";
    const msg = document.getElementById("ucSecurityMsg");
    if (!security_answer.trim()) {
        if (msg) { msg.className = "form-msg msg-err"; msg.innerText = "请填写安全问题答案"; }
        return;
    }
    const d = await apiPost("/api/auth/set-security-question", {
        token: u.token, security_question, security_answer: security_answer.trim()
    });
    if (msg) { msg.className = "form-msg " + (d.code === 200 ? "msg-ok" : "msg-err"); msg.innerText = d.msg || ""; }
    if (d.code === 200) document.getElementById("ucAnswer").value = "";
}

// ---- 退出登录 ----
function logout() {
    Session.clear();
    location.href = "index.html";
}

// ---- 通用：渲染数据表格 ----
// cols: [{key,label,render(row)}]  rows: []
function renderTable(tbodyId, cols, rows, emptyText) {
    const body = document.getElementById(tbodyId);
    if (!body) return;
    if (!rows || !rows.length) {
        const span = cols.length;
        body.innerHTML = '<tr><td colspan="' + span + '" class="center muted">' + esc(emptyText || "暂无数据") + '</td></tr>';
        return;
    }
    body.innerHTML = rows.map(function (r) {
        return "<tr>" + cols.map(function (c) {
            return "<td>" + (c.render ? c.render(r) : esc(r[c.key])) + "</td>";
        }).join("") + "</tr>";
    }).join("");
}

// ---- 状态徽章 ----
function statusBadge(status) {
    const s = String(status || "");
    let cls = "badge-wait";
    if (s.indexOf("已通过") >= 0 || s.indexOf("已解决") >= 0 || s.indexOf("正常") >= 0 || s.indexOf("已签约") >= 0) cls = "badge-done";
    else if (s.indexOf("已驳回") >= 0 || s.indexOf("高危") >= 0 || s.indexOf("已流失") >= 0) cls = "badge-reject";
    return '<span class="badge ' + cls + '">' + esc(s) + "</span>";
}

/* ============================================================
   通用业务组件（v3）：通知铃铛 / 申请时间轴 / 自定义表单弹窗
   ============================================================ */

// ---- 通知铃铛：在顶部栏渲染一个铃铛，点击展开通知面板 ----
// containerId: 承载铃铛的容器；user: 当前登录用户（含 token）
async function renderNotificationBell(containerId, user) {
    const box = document.getElementById(containerId);
    if (!box || !user) return;
    box.innerHTML =
        '<div class="notif-wrap">' +
        '<button class="notif-bell" id="notifBell" title="通知中心" onclick="toggleNotifPanel()">🔔' +
        '<span class="notif-dot" id="notifDot" style="display:none;"></span></button>' +
        '<div class="notif-panel" id="notifPanel" style="display:none;">' +
        '<div class="notif-head"><b>通知中心</b><button class="link-btn" onclick="readAllNotif()">全部已读</button></div>' +
        '<div id="notifList" class="notif-list"><div class="muted center" style="padding:18px;">加载中...</div></div>' +
        '</div></div>';
    await refreshNotifBadge(user);
}

async function refreshNotifBadge(user) {
    if (!user) return;
    try {
        const d = await apiGet("/api/notification/unread");
        const dot = document.getElementById("notifDot");
        if (dot && d.code === 200) {
            const n = d.data.unread || 0;
            dot.style.display = n > 0 ? "block" : "none";
            dot.textContent = n > 99 ? "99+" : n;
        }
    } catch (e) {}
}

async function toggleNotifPanel() {
    const panel = document.getElementById("notifPanel");
    if (!panel) return;
    const show = panel.style.display === "none";
    panel.style.display = show ? "block" : "none";
    if (show) await loadNotifList();
}

async function loadNotifList() {
    const user = Session.get();
    const box = document.getElementById("notifList");
    if (!box || !user) return;
    try {
        const d = await apiGet("/api/notification/list");
        const list = (d.code === 200 && d.data.list) ? d.data.list : [];
        if (!list.length) {
            box.innerHTML = '<div class="empty-state"><div class="es-ico">🔔</div><div class="es-text">暂无通知</div></div>';
            return;
        }
        box.innerHTML = list.map(function (n) {
            const unread = n.is_read === 0;
            const icons = { "审批": "📋", "预警": "💛", "活动": "📅", "申请": "🎓", "线索": "👥", "系统": "ℹ️" };
            return '<div class="notif-item' + (unread ? ' unread' : '') + '" onclick="readOneNotif(' + n.id + ')">' +
                '<div class="ni-ico">' + (icons[n.type] || "🔔") + '</div>' +
                '<div class="ni-body"><div class="ni-title">' + esc(n.title) + (unread ? '<span class="ni-new"></span>' : '') + '</div>' +
                '<div class="ni-content">' + esc(n.content) + '</div>' +
                '<div class="ni-time">' + esc(n.create_time) + '</div></div></div>';
        }).join("");
    } catch (e) {
        box.innerHTML = '<div class="muted center" style="padding:18px;">加载失败</div>';
    }
}

async function readOneNotif(id) {
    const user = Session.get();
    if (!user) return;
    await apiPost("/api/notification/read", { token: user.token, notify_id: id });
    await loadNotifList();
    await refreshNotifBadge(user);
}

async function readAllNotif() {
    const user = Session.get();
    if (!user) return;
    const d = await apiPost("/api/notification/read", { token: user.token, notify_id: 0 });
    toast(d.msg || "已读", "ok");
    await loadNotifList();
    await refreshNotifBadge(user);
}

// ---- 申请进度时间轴渲染 ----
// app: application 对象（含 history 数组、stage、progress_pct、stage_index）
function renderApplicationTimeline(app) {
    const stages = ["选校定位", "材料准备", "递交申请", "面试/补充", "收到Offer", "办理签证", "行前准备", "已入学"];
    const cur = app.stage_index || 0;
    const done = app.status === "已完成";
    const histMap = {};
    (app.history || []).forEach(function (h) { histMap[h.stage] = h; });
    const recordMap = {};
    (app.stage_records || []).forEach(function (record) { recordMap[record.stage] = record; });
    return '<div class="timeline">' + stages.map(function (s, i) {
        let cls = "tl-item";
        if (i < cur || (done && i === cur)) cls += " done";
        else if (i === cur) cls += " current";
        else cls += " future";
        const h = histMap[s];
        const record = recordMap[s] || {};
        // 节点状态不能只看 history 是否存在。旧数据可能通过“设置已入学”
        // 直接完成流程，未逐条写历史；此时前置节点仍应按当前阶段显示已完成。
        let detail;
        if (record.summary) detail = esc(record.summary);
        else if (h) detail = esc(h.note || "");
        else if (i < cur || (done && i <= cur)) detail = "已完成";
        else if (i === cur) detail = "进行中";
        else detail = "待进行";
        const time = record.completed_at || record.started_at || (h ? h.create_time : "");
        const source = record.source_type || (h && h.source_type) || "";
        const sourceLabel = source === "BACKFILL" ? "历史回填" :
            (source === "MANUAL" ? "人工记录" : (source === "SYSTEM" ? "系统生成" : ""));
        const materialCount = (app.materials || []).filter(function (m) { return m.stage === s; }).length;
        return '<div class="' + cls + '"><div class="tl-dot"></div>' +
            '<div class="tl-content"><div class="tl-stage">' + esc(s) +
            (sourceLabel ? '<span class="archive-source src-' + esc(source.toLowerCase()) + '">' + sourceLabel + '</span>' : '') +
            '</div>' +
            '<div class="tl-detail">' + detail + '</div>' +
            '<div class="tl-time">' + (time ? esc(time) : "日期待补录") +
            (materialCount ? ' · 材料 ' + materialCount + ' 项' : '') + '</div>' +
            '<button class="link-btn tl-open" onclick="openApplicationStage(' + app.id + ',' +
            esc(JSON.stringify(s)) + ')">查看阶段档案 →</button>' +
            '</div></div>';
    }).join("") + '</div>';
}

const APPLICATION_STAGE_FIELDS = {
    "选校定位": [
        ["country", "国家 / 地区"], ["application_mode", "学习模式"],
        ["program", "具体留学方案"], ["university", "目标院校"],
        ["backup_schools", "备选院校"], ["decision_reason", "选择依据"]
    ],
    "材料准备": [
        ["checklist_summary", "材料清单概况"], ["completed_items", "已完成材料"],
        ["missing_items", "缺失材料"], ["deadline", "补交截止日期"],
        ["material_note", "材料说明"]
    ],
    "递交申请": [
        ["application_no", "申请编号"], ["submission_target", "递交院校 / 系统"],
        ["submitted_at", "递交时间"], ["receipt_no", "回执编号"],
        ["submission_note", "递交说明"]
    ],
    "面试/补充": [
        ["interview_at", "面试时间"], ["interview_mode", "面试方式"],
        ["interviewer", "面试官 / 参与人"], ["supplement", "补充材料"],
        ["result", "面试结果"]
    ],
    "收到Offer": [
        ["offer_no", "Offer 编号"], ["university", "录取院校"],
        ["program", "录取专业"], ["conditions", "录取条件"],
        ["accept_deadline", "接受截止日期"], ["result", "处理结果"]
    ],
    "办理签证": [
        ["visa_type", "签证类型"], ["appointment_at", "预约时间"],
        ["visa_no", "签证 / 受理编号"], ["submitted_items", "递交材料"],
        ["result", "签证结果"]
    ],
    "行前准备": [
        ["flight_info", "航班信息"], ["accommodation", "住宿安排"],
        ["insurance", "保险信息"], ["orientation", "行前培训"],
        ["emergency_contact", "紧急联系人"]
    ],
    "已入学": [
        ["enrollment_date", "入学日期"], ["study_country", "就读国家"],
        ["campus", "校区"], ["student_no", "学号"],
        ["study_major", "就读专业"], ["duration", "标准学制"]
    ]
};

function applicationFieldLabel(key) {
    const all = Object.keys(APPLICATION_STAGE_FIELDS).reduce(function (out, stage) {
        APPLICATION_STAGE_FIELDS[stage].forEach(function (field) { out[field[0]] = field[1]; });
        return out;
    }, {});
    return all[key] || key;
}

function applicationSourceBadge(source) {
    const s = String(source || "");
    if (s === "BACKFILL") return '<span class="archive-source src-backfill">历史回填</span>';
    if (s === "MANUAL") return '<span class="archive-source src-manual">人工记录</span>';
    if (s === "SYSTEM") return '<span class="archive-source src-system">系统生成</span>';
    return "";
}

function applicationStatusBadge(status) {
    const s = String(status || "待进行");
    const cls = s === "已完成" ? "badge-done" :
        (s === "待补充" || s === "已退回" ? "badge-reject" : "badge-wait");
    return '<span class="badge ' + cls + '">' + esc(s) + '</span>';
}

function openApplicationStage(applicationId, stage) {
    openApplicationArchive(applicationId, { stage: stage });
}

async function openApplicationArchive(applicationId, opts) {
    opts = opts || {};
    if (!Session.get()) { toast("请先登录", "err"); return; }
    const old = document.getElementById("applicationArchiveMask");
    if (old) old.remove();
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.id = "applicationArchiveMask";
    mask.innerHTML = '<div class="modal-box application-archive">' +
        '<div class="archive-loading">正在读取申请案件档案…</div></div>';
    document.body.appendChild(mask);
    mask.onclick = function (event) {
        if (event.target === mask) mask.remove();
    };
    await refreshApplicationArchive(applicationId, opts.stage || "");
}

async function refreshApplicationArchive(applicationId, selectedStage) {
    const mask = document.getElementById("applicationArchiveMask");
    if (!mask) return;
    const d = await apiGet("/api/application/" + applicationId + "/detail");
    if (d.code !== 200) {
        mask.querySelector(".modal-box").innerHTML =
            '<div class="archive-loading">' + esc(d.msg || "申请档案加载失败") + '</div>' +
            '<div class="modal-actions"><button onclick="closeApplicationArchive()">关闭</button></div>';
        return;
    }
    const app = d.data || {};
    activeApplicationArchiveId = Number(app.id || 0);
    activeApplicationArchiveStage = selectedStage || app.stage || "选校定位";
    const records = app.stage_records || [];
    const stage = selectedStage || app.stage || "选校定位";
    const record = records.find(function (item) { return item.stage === stage; }) || {
        stage: stage, status: "待进行", summary: "", detail: {}, source_type: "SYSTEM"
    };
    const perms = app.permissions || {};
    const fields = record.detail || {};
    const fieldRows = Object.keys(fields).length
        ? Object.keys(fields).map(function (key) {
            return '<div class="archive-field"><span>' + esc(applicationFieldLabel(key)) +
                '</span><b>' + esc(fields[key]) + '</b></div>';
        }).join("")
        : '<div class="archive-empty">该阶段明细还没有录入，当前仅保留流程状态。</div>';
    const materials = (app.materials || []).filter(function (item) { return item.stage === stage; });
    const canUploadHere = !!perms.can_upload && (
        !perms.is_student
        || stage === app.stage
        || record.status === "待补充"
        || materials.some(function (item) {
            return item.status === "待提交" || item.status === "已退回";
        })
    );
    const materialRows = materials.length ? materials.map(function (item) {
        const actions = [];
        if (item.download_available) {
            actions.push('<button class="btn-xs btn-sm-outline" onclick="downloadApplicationMaterial(' +
                item.id + ',' + esc(JSON.stringify(item.file_name || "材料文件")) + ')">下载</button>');
        }
        if (canUploadHere && item.status !== "已通过") {
            actions.push('<button class="btn-xs" onclick="openMaterialUpload(' + app.id + ',' +
                esc(JSON.stringify(stage)) + ',' + esc(JSON.stringify(item)) + ')">' +
                (item.download_available ? "替换" : "上传") + '</button>');
        }
        if (perms.can_review && item.download_available) {
            actions.push('<button class="btn-xs btn-success" onclick="reviewApplicationMaterial(' +
                item.id + ',\'已通过\')">通过</button>');
            actions.push('<button class="btn-xs btn-danger" onclick="reviewApplicationMaterial(' +
                item.id + ',\'已退回\')">退回</button>');
        }
        return '<div class="archive-material"><div><strong>' + esc(item.material_name) +
            (item.required_flag ? ' <span class="badge badge-reject">必需</span>' : '') +
            '</strong><div class="archive-meta">' + esc(item.file_name || "尚无附件") +
            (item.file_size ? ' · ' + formatFileSize(item.file_size) : '') +
            (item.version ? ' · V' + item.version : '') + '</div>' +
            (item.review_note ? '<div class="archive-review-note">审核意见：' + esc(item.review_note) + '</div>' : '') +
            '</div><div class="archive-material-side">' + applicationStatusBadge(item.status) +
            '<div class="row-actions">' + actions.join("") + '</div></div></div>';
    }).join("") : '<div class="archive-empty">该阶段暂无材料清单。' +
        (perms.can_review ? '负责人可先添加清单，再通知学生补交。' :
            (canUploadHere ? '如有材料，可点击“上传材料”。' : '该历史阶段仅可查看。')) + '</div>';
    const history = (app.history || []).filter(function (item) { return item.stage === stage; });
    const historyRows = history.length ? history.map(function (item) {
        return '<div class="archive-history"><div><strong>' + esc(item.action || "阶段操作") +
            '</strong> ' + applicationSourceBadge(item.source_type) + '</div>' +
            '<div>' + esc(item.note || "—") + '</div>' +
            '<div class="archive-meta">' + esc(item.operator_name || item.operator_username || "系统") +
            ' · ' + esc(item.create_time || "—") + '</div></div>';
    }).join("") : '<div class="archive-empty">暂无该阶段操作日志。</div>';
    const stageTabs = records.map(function (item) {
        return '<button class="archive-tab' + (item.stage === stage ? ' active' : '') +
            '" onclick="refreshApplicationArchive(' + app.id + ',' +
            esc(JSON.stringify(item.stage)) + ')"><span>' + esc(item.stage) + '</span>' +
            '<small>' + esc(item.status || "待进行") + '</small></button>';
    }).join("");
    const summary = app.material_summary || {};
    mask.querySelector(".modal-box").innerHTML =
        '<div class="archive-head"><div><div class="archive-kicker">APPLICATION CASE FILE</div>' +
        '<h3>' + esc(app.student_name || app.student_username || app.university || "申请案件档案") +
        '</h3><p>' + esc(app.country || "—") + ' · ' + esc(app.university || "院校待定") +
        ' · ' + esc(app.program || "具体方案待定") + '</p></div>' +
        '<button class="archive-close" onclick="closeApplicationArchive()">×</button></div>' +
        '<div class="archive-overview"><div><span>当前阶段</span><b>' + esc(app.stage) + '</b></div>' +
        '<div><span>申请进度</span><b>' + Number(app.progress_pct || 0) + '%</b></div>' +
        '<div><span>材料状态</span><b>' + Number(summary.total || 0) + ' 项 / 待审 ' +
        Number(summary.pending_review || 0) + '</b></div>' +
        '<div><span>案件状态</span>' + applicationStatusBadge(app.status) + '</div></div>' +
        '<div class="archive-tabs">' + stageTabs + '</div>' +
        '<div class="archive-grid"><section class="archive-main">' +
        '<div class="archive-section-head"><div><h4>' + esc(stage) + ' · 阶段明细</h4>' +
        '<p>阶段状态 ' + applicationStatusBadge(record.status) + ' ' +
        applicationSourceBadge(record.source_type) + '</p></div>' +
        (perms.can_manage_stage ? '<button class="btn-sm-outline" onclick="editApplicationStageArchive(' +
            app.id + ',' + esc(JSON.stringify(stage)) + ',' + esc(JSON.stringify(record.summary || "")) +
            ',' + esc(JSON.stringify(fields)) + ')">完善阶段信息</button>' : '') + '</div>' +
        '<div class="archive-summary">' + esc(record.summary || "该阶段尚未填写详细结论。") + '</div>' +
        '<div class="archive-fields">' + fieldRows + '</div>' +
        '<div class="archive-section-head"><div><h4>材料与附件</h4><p>学生可查看本人材料；审核意见会保留</p></div>' +
        '<div class="row-actions">' + (canUploadHere ? '<button class="btn-sm-outline" onclick="openMaterialUpload(' +
            app.id + ',' + esc(JSON.stringify(stage)) + ',null)">上传材料</button>' : '') +
        (perms.can_review ? '<button class="btn-sm-outline" onclick="addApplicationMaterialChecklist(' +
            app.id + ',' + esc(JSON.stringify(stage)) + ')">添加材料清单</button>' : '') +
        '</div></div><div class="archive-materials">' + materialRows + '</div>' +
        '</section><aside class="archive-side">' +
        '<div class="archive-section-head"><div><h4>阶段操作日志</h4><p>谁在什么时候做了什么</p></div></div>' +
        '<div class="archive-history-list">' + historyRows + '</div>' +
        '</aside></div>';
}

function closeApplicationArchive() {
    const mask = document.getElementById("applicationArchiveMask");
    if (mask) mask.remove();
}

let activeApplicationArchiveId = 0;
let activeApplicationArchiveStage = "";

async function editApplicationStageArchive(applicationId, stage, currentSummary, currentFields) {
    const defs = APPLICATION_STAGE_FIELDS[stage] || [];
    const values = currentFields || {};
    const fields = [{
        key: "summary", label: "阶段结论 / 说明", type: "textarea",
        value: currentSummary || "", placeholder: "说明这一阶段实际完成了什么，学生可见"
    }].concat(defs.map(function (field) {
        return {
            key: field[0], label: field[1], value: values[field[0]] || "",
            placeholder: "未填写可留空"
        };
    }));
    const result = await formBox("完善「" + stage + "」阶段档案", fields, {
        okText: "保存阶段档案",
        message: "这里填写的内容会展示给该申请学生，请不要填写内部私密备注。"
    });
    if (!result) return;
    const payloadFields = {};
    defs.forEach(function (field) {
        if (result[field[0]]) payloadFields[field[0]] = result[field[0]];
    });
    const d = await apiPost("/api/application/" + applicationId + "/stages/" +
        encodeURIComponent(stage) + "/detail", {
        token: Session.get().token,
        summary: result.summary || "",
        fields: payloadFields
    });
    toast(d.msg || "", d.code === 200 ? "ok" : "err");
    if (d.code === 200) {
        await refreshApplicationArchive(applicationId, stage);
        document.dispatchEvent(new CustomEvent("yj-application-changed", {
            detail: { applicationId: applicationId }
        }));
    }
}

async function addApplicationMaterialChecklist(applicationId, stage) {
    const result = await formBox("添加材料清单 · " + stage, [
        { key: "material_name", label: "材料名称", required: true, placeholder: "例如：护照信息页" },
        { key: "required_flag", label: "是否必需", type: "select",
          options: [{ value: "1", label: "必需" }, { value: "0", label: "选填" }], value: "1" },
        { key: "note", label: "补充说明", type: "textarea", placeholder: "学生看到后应如何准备" },
        { key: "visible_to_student", label: "学生可见", type: "select",
          options: [{ value: "1", label: "可见" }, { value: "0", label: "内部清单" }], value: "1" }
    ], { okText: "添加清单" });
    if (!result) return;
    const d = await apiPost("/api/application/" + applicationId + "/materials/checklist", {
        token: Session.get().token,
        stage: stage,
        material_name: result.material_name,
        required_flag: result.required_flag === "1",
        note: result.note,
        visible_to_student: result.visible_to_student === "1"
    });
    toast(d.msg || "", d.code === 200 ? "ok" : "err");
    if (d.code === 200) {
        await refreshApplicationArchive(applicationId, stage);
        document.dispatchEvent(new CustomEvent("yj-application-changed", {
            detail: { applicationId: applicationId }
        }));
    }
}

function formatFileSize(size) {
    const value = Number(size || 0);
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
    return (value / 1024 / 1024).toFixed(1) + " MB";
}

function openMaterialUpload(applicationId, stage, material) {
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = '<div class="modal-box form-box"><h3>' +
        esc(material ? "替换材料 · " + material.material_name : "上传申请材料 · " + stage) +
        '</h3><div class="form-modal-desc">支持 PDF、Word、Excel、图片、TXT、ZIP，单个文件不超过 8 MB。</div>' +
        (material ? '' : '<label>材料名称</label><input id="archiveMaterialName" placeholder="例如：护照信息页">') +
        '<label>选择文件</label><input id="archiveMaterialFile" type="file" accept=".pdf,.doc,.docx,.xls,.xlsx,.jpg,.jpeg,.png,.webp,.txt,.zip">' +
        '<label>补充说明</label><textarea id="archiveMaterialNote" rows="3" placeholder="例如：按顾问要求补交更新版本"></textarea>' +
        '<div id="archiveUploadMsg" class="form-msg"></div>' +
        '<div class="modal-actions"><button class="btn-sm-outline" id="archiveUploadCancel">取消</button>' +
        '<button id="archiveUploadOk">上传</button></div></div>';
    document.body.appendChild(mask);
    mask.querySelector("#archiveUploadCancel").onclick = function () { mask.remove(); };
    mask.onclick = function (event) { if (event.target === mask) mask.remove(); };
    mask.querySelector("#archiveUploadOk").onclick = async function () {
        const file = mask.querySelector("#archiveMaterialFile").files[0];
        const nameInput = mask.querySelector("#archiveMaterialName");
        const materialName = material ? material.material_name :
            (nameInput ? nameInput.value.trim() : "");
        const message = mask.querySelector("#archiveUploadMsg");
        if (!file) {
            message.className = "form-msg msg-err";
            message.innerText = "请选择文件";
            return;
        }
        if (file.size > 8 * 1024 * 1024) {
            message.className = "form-msg msg-err";
            message.innerText = "文件不能超过 8 MB";
            return;
        }
        if (!material && !materialName) {
            message.className = "form-msg msg-err";
            message.innerText = "请填写材料名称";
            return;
        }
        message.className = "form-msg";
        message.innerText = "正在上传…";
        const reader = new FileReader();
        reader.onload = async function () {
            const d = await apiPost("/api/application/" + applicationId + "/materials/upload", {
                token: Session.get().token,
                stage: stage,
                material_id: material ? material.id : 0,
                material_name: materialName,
                file_name: file.name,
                content_base64: reader.result,
                note: mask.querySelector("#archiveMaterialNote").value.trim()
            });
            if (d.code !== 200) {
                message.className = "form-msg msg-err";
                message.innerText = d.msg || "上传失败";
                return;
            }
            mask.remove();
            toast(d.msg || "材料已上传", "ok");
            await refreshApplicationArchive(applicationId, stage);
            document.dispatchEvent(new CustomEvent("yj-application-changed", {
                detail: { applicationId: applicationId }
            }));
        };
        reader.onerror = function () {
            message.className = "form-msg msg-err";
            message.innerText = "读取文件失败";
        };
        reader.readAsDataURL(file);
    };
}

async function reviewApplicationMaterial(materialId, status) {
    let note = "";
    if (status === "已退回") {
        note = await promptBox("退回材料", "请填写退回原因，学生可以看到。", {
            required: true, multiline: true
        });
        if (note === null) return;
    } else {
        note = await promptBox("通过材料审核", "可填写审核备注（选填）。", { multiline: true });
        if (note === null) return;
    }
    const d = await apiPost("/api/application/materials/" + materialId + "/review", {
        token: Session.get().token, status: status, review_note: note
    });
    toast(d.msg || "", d.code === 200 ? "ok" : "err");
    if (d.code === 200) {
        if (activeApplicationArchiveId) {
            await refreshApplicationArchive(
                activeApplicationArchiveId, activeApplicationArchiveStage
            );
        }
        document.dispatchEvent(new CustomEvent("yj-application-changed", {
            detail: { materialId: materialId }
        }));
    }
}

async function downloadApplicationMaterial(materialId, fileName) {
    if (!Session.get()) return;
    try {
        const response = await fetch(
            API_BASE + "/api/application/materials/" + materialId + "/download",
            { headers: authHeaders() }
        );
        if (!response.ok) {
            let message = "下载失败";
            try {
                const data = await response.json();
                message = data.msg || message;
            } catch (e) {}
            toast(message, "err");
            return;
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = fileName || "申请材料";
        document.body.appendChild(link);
        link.click();
        link.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    } catch (e) {
        toast("无法连接文件服务", "err");
    }
}

// ---- 自定义表单弹窗（多字段，返回对象；取消返回 null）----
// fields: [{key,label,type,placeholder,value,options,required}]  type: text/textarea/select/number/date
function formBox(title, fields, opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
        const mask = document.createElement("div");
        mask.className = "modal-mask";
        const fieldsHtml = fields.map(function (f) {
            const val = f.value == null ? "" : f.value;
            let input;
            if (f.type === "textarea") {
                input = '<textarea data-key="' + f.key + '" rows="3" placeholder="' + esc(f.placeholder || "") + '">' + esc(val) + '</textarea>';
            } else if (f.type === "select") {
                input = '<select data-key="' + f.key + '">' + (f.options || []).map(function (o) {
                    const value = (o && typeof o === "object") ? o.value : o;
                    const label = (o && typeof o === "object") ? o.label : o;
                    return '<option value="' + esc(value) + '"' + (value === val ? ' selected' : '') + '>' + esc(label) + '</option>';
                }).join("") + '</select>';
            } else {
                input = '<input type="' + (f.type || "text") + '" data-key="' + f.key + '" value="' + esc(val) + '" placeholder="' + esc(f.placeholder || "") + '">';
            }
            return '<div><label>' + esc(f.label) + (f.required ? ' <span style="color:#b91c1c;">*</span>' : '') + '</label>' + input + '</div>';
        }).join("");
        mask.innerHTML =
            '<div class="modal-box form-box"><h3>' + esc(title) + '</h3>' +
            (opts.message ? '<div class="form-modal-desc">' + esc(opts.message) + '</div>' : '') +
            fieldsHtml +
            '<div id="fbErr" class="form-msg msg-err"></div>' +
            '<div class="modal-actions">' +
            '<button class="btn-sm-outline" id="fbCancel">取消</button>' +
            '<button id="fbOk">' + esc(opts.okText || "提交") + '</button></div></div>';
        document.body.appendChild(mask);
        mask.querySelector("#fbCancel").onclick = function () { mask.remove(); resolve(null); };
        mask.querySelector("#fbOk").onclick = function () {
            const result = {};
            let missing = "";
            fields.forEach(function (f) {
                const el = mask.querySelector('[data-key="' + f.key + '"]');
                const v = el ? (el.value || "").trim() : "";
                result[f.key] = v;
                if (f.required && !v && !missing) missing = f.label;
            });
            if (missing) {
                mask.querySelector("#fbErr").innerText = "请填写：" + missing;
                return;
            }
            mask.remove();
            resolve(result);
        };
        mask.onclick = function (e) { if (e.target === mask) { mask.remove(); resolve(null); } };
    });
}

/* 图表钻取预览：把统计值对应的业务明细放进统一弹窗。
   records: [{title,badge,meta,fields:[[label,value],...]}]
   opts.onAction 存在时显示“进入模块处理”按钮。 */
function showRecordPreview(title, subtitle, records, opts) {
    opts = opts || {};
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    const rows = records || [];
    const body = rows.length
        ? rows.map(function (record, index) {
            const fields = (record.fields || []).map(function (field) {
                return '<div class="preview-field"><span>' + esc(field[0]) +
                    '</span><b>' + esc(field[1] == null || field[1] === "" ? "—" : field[1]) +
                    '</b></div>';
            }).join("");
            return '<article class="preview-record">' +
                '<div class="preview-record-head"><div><em>#' + (index + 1) + '</em>' +
                    '<strong>' + esc(record.title || "业务记录") + '</strong></div>' +
                    (record.badge ? '<span class="badge">' + esc(record.badge) + '</span>' : '') +
                '</div>' +
                (record.meta ? '<p>' + esc(record.meta) + '</p>' : '') +
                '<div class="preview-fields">' + fields + '</div>' +
            '</article>';
        }).join("")
        : '<div class="yj-chart-empty"><span>◌</span><p>' +
            esc(opts.emptyText || "当前条件下没有明细记录") + '</p></div>';
    mask.innerHTML =
        '<div class="modal-box preview-box">' +
            '<div class="preview-head"><div><span>DATA PREVIEW</span><h3>' +
                esc(title) + '</h3><p>' + esc(subtitle || ("共 " + rows.length + " 条明细")) +
                '</p></div><button type="button" class="preview-close" aria-label="关闭">×</button></div>' +
            '<div class="preview-list">' + body + '</div>' +
            '<div class="modal-actions">' +
                '<button class="btn-sm-outline" data-preview-close>关闭</button>' +
                (opts.onAction ? '<button data-preview-action>' +
                    esc(opts.actionText || "进入模块处理") + '</button>' : '') +
            '</div>' +
        '</div>';
    document.body.appendChild(mask);
    const close = function () { mask.remove(); };
    mask.querySelector(".preview-close").onclick = close;
    mask.querySelector("[data-preview-close]").onclick = close;
    const action = mask.querySelector("[data-preview-action]");
    if (action) {
        action.onclick = function () {
            close();
            opts.onAction();
        };
    }
    mask.onclick = function (event) {
        if (event.target === mask) close();
    };
}

/* ============================================================
   学籍阶段组件（v5）
   ============================================================ */

// 渲染四步留学进程条：咨询期 → 申请期 → 在读 → 已毕业
function renderStageTimeline(containerId, lc) {
    const box = document.getElementById(containerId);
    if (!box || !lc || !lc.timeline) return;
    const steps = lc.timeline.map(function (s) {
        const cls = s.current ? "current" : (s.done ? "done" : "todo");
        return '<div class="st-step ' + cls + '">' +
            '<div class="st-dot">' + (s.done ? "✓" : (s.current ? "●" : "")) + '</div>' +
            '<div class="st-label">' + esc(s.label) + '</div>' +
            '</div>';
    }).join('<div class="st-line"></div>');
    box.innerHTML =
        '<div class="stage-card">' +
        '<div class="stage-top">' +
        '<div><div class="stage-kicker">我的留学进程</div>' +
        '<div class="stage-now">当前：<b>' + esc(lc.stage_label) + '</b>　<span>' + esc(lc.stage_desc) + '</span></div></div>' +
        '<div class="stage-hint">下一步：' + esc(lc.next_step) + '</div>' +
        '</div>' +
        '<div class="stage-track">' + steps + '</div>' +
        '</div>';
}

// 渲染"未解锁"引导卡：说明缺什么 + 现在能做什么
function renderLockedCard(containerId, title, reason, actions) {
    const box = document.getElementById(containerId);
    if (!box) return;
    const btns = (actions || []).map(function (a) {
        return '<button class="' + (a.primary ? "" : "btn-sm-outline") + '" onclick="' + a.onclick + '">' + esc(a.label) + '</button>';
    }).join("");
    box.innerHTML =
        '<div class="locked-card">' +
        '<div class="locked-ico">🔒</div>' +
        '<h3>' + esc(title) + '</h3>' +
        '<p>' + esc(reason) + '</p>' +
        '<div class="locked-actions">' + btns + '</div>' +
        '<div class="locked-foot">服务会随你的学籍阶段自动解锁，无需申请。</div>' +
        '</div>';
}

