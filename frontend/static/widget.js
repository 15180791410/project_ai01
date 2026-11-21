/* ============================================================
   ai · 页面悬浮客服挂件
   ------------------------------------------------------------
   与「免费咨询」按钮能力一致：调用 /api/auth/public-chat
    （客服与活动咨询），免登录、可多轮对话。
   同时提供留资入口，答不上来的问题转给顾问跟进。
   ============================================================ */
(function () {
    "use strict";

    var API = (typeof API_BASE !== "undefined") ? API_BASE : "http://127.0.0.2:8000";
    var WELCOME = "你好，我是粤晓助手 👋\n留学费用、申请条件、院校专业、签证材料、近期活动，都可以直接问我。";
    var QUICK = [
        "新加坡留学一年大概多少钱？",
        "申请需要什么条件？",
        "最近有什么活动可以参加？",
        "德国双元制怎么报名？"
    ];

    var state = {
        conv: sessionStorage.getItem("yw_conv") || "",
        msgs: [],
        sending: false,
        inited: false
    };
    try { state.msgs = JSON.parse(sessionStorage.getItem("yw_msgs") || "[]") || []; } catch (e) { state.msgs = []; }

    function visitorId() {
        var v = localStorage.getItem("yw_visitor");
        if (!v) {
            v = "guest_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
            localStorage.setItem("yw_visitor", v);
        }
        return v;
    }

    /* ---------------- DOM ---------------- */
    var root = document.createElement("div");
    root.className = "yw-root";
    root.innerHTML =
        '<div class="yw-teaser" id="ywTeaser">留学问题？点我问问 →</div>' +
        '<button class="yw-ball" id="ywBall" aria-label="在线客服">💬<span class="yw-dot" id="ywDot"></span></button>' +
        '<div class="yw-panel hide" id="ywPanel">' +
            '<div class="yw-head">' +
                '<div class="yw-avatar">🎓</div>' +
                '<div><div class="yw-tt">粤晓助手</div><div class="yw-st">专业留学与升学顾问</div></div>' +
                '<button class="yw-close" id="ywClose" aria-label="收起">✕</button>' +
            '</div>' +
            '<div class="yw-body" id="ywBody"></div>' +
            '<div class="yw-foot">' +
                '<div class="yw-inrow">' +
                    '<textarea id="ywInput" rows="1" placeholder="输入你的问题，回车发送"></textarea>' +
                    '<button class="yw-send" id="ywSend">发送</button>' +
                '</div>' +
                '<div class="yw-links">' +
                    '<span>内容仅供参考，具体以顾问答复为准</span>' +
                    '<a id="ywLeadBtn">留下联系方式，顾问回电 →</a>' +
                '</div>' +
            '</div>' +
            '<div class="yw-lead hide" id="ywLead">' +
                '<h4>让顾问联系你</h4>' +
                '<p>留下称呼和联系方式，顾问会尽快回电（工作时间 9:00–18:00）。</p>' +
                '<input id="ywLeadName" placeholder="怎么称呼你？">' +
                '<input id="ywLeadContact" placeholder="手机号 / 微信号">' +
                '<input id="ywLeadCountry" placeholder="意向国家（选填，如 新加坡）">' +
                '<div class="yw-lead-actions">' +
                    '<button class="yw-lead-cancel" id="ywLeadCancel">取消</button>' +
                    '<button class="yw-lead-ok" id="ywLeadOk">提交</button>' +
                '</div>' +
                '<div class="yw-lead-msg" id="ywLeadMsg"></div>' +
            '</div>' +
        '</div>';
    document.body.appendChild(root);

    var ball = document.getElementById("ywBall");
    var panel = document.getElementById("ywPanel");
    var body = document.getElementById("ywBody");
    var input = document.getElementById("ywInput");
    var sendBtn = document.getElementById("ywSend");
    var teaser = document.getElementById("ywTeaser");
    var dot = document.getElementById("ywDot");
    var leadBox = document.getElementById("ywLead");

    /* ---------------- 消息渲染 ---------------- */
    function scrollBottom() {
        body.scrollTop = body.scrollHeight;
    }

    function addMsg(who, text, persist) {
        var wrap = document.createElement("div");
        wrap.className = "yw-msg " + (who === "me" ? "me" : "ai");
        var face = document.createElement("div");
        face.className = "yw-face";
        face.textContent = who === "me" ? "我" : "🎓";
        var bubble = document.createElement("div");
        bubble.className = "yw-b";
        if (who === "me") {
            bubble.textContent = text;
        } else {
            // 助手回复走富文本渲染，避免 ### 和 ** 原样露出
            bubble.className = "yw-b md";
            if (typeof renderMarkdown === "function") {
                bubble.innerHTML = renderMarkdown(text);
            } else {
                bubble.textContent = text;
            }
        }
        wrap.appendChild(face);
        wrap.appendChild(bubble);
        body.appendChild(wrap);
        scrollBottom();
        if (persist !== false) {
            state.msgs.push({ who: who, text: text });
            saveMsgs();
        }
        return wrap;
    }

    function saveMsgs() {
        try {
            sessionStorage.setItem("yw_msgs", JSON.stringify(state.msgs.slice(-40)));
            sessionStorage.setItem("yw_conv", state.conv || "");
        } catch (e) {}
    }

    function renderQuick() {
        var old = document.getElementById("ywQuick");
        if (old) old.remove();
        var box = document.createElement("div");
        box.className = "yw-quick";
        box.id = "ywQuick";
        QUICK.forEach(function (q) {
            var chip = document.createElement("div");
            chip.className = "yw-chip";
            chip.textContent = q;
            chip.onclick = function () { send(q); };
            box.appendChild(chip);
        });
        body.appendChild(box);
        scrollBottom();
    }

    function showTyping() {
        var wrap = document.createElement("div");
        wrap.className = "yw-msg ai";
        wrap.id = "ywTyping";
        wrap.innerHTML = '<div class="yw-face">🎓</div><div class="yw-b"><span class="yw-typing"><i></i><i></i><i></i></span></div>';
        body.appendChild(wrap);
        scrollBottom();
        return wrap;
    }

    function removeTyping(el) {
        if (el && el.parentNode) el.parentNode.removeChild(el);
    }

    /* ---------------- 对话 ---------------- */
    async function send(text) {
        text = (text || "").trim();
        if (!text || state.sending) return;
        var quick = document.getElementById("ywQuick");
        if (quick) quick.remove();

        state.sending = true;
        sendBtn.disabled = true;
        addMsg("me", text);
        input.value = "";
        input.style.height = "40px";
        var typing = showTyping();

        try {
            var res = await fetch(API + "/api/auth/public-chat", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ query: text, conversation_id: state.conv, visitor: visitorId() })
            });
            var d = await res.json();
            removeTyping(typing);
            if (d && d.code === 200 && d.data) {
                if (d.data.conversation_id) state.conv = d.data.conversation_id;
                addMsg("ai", d.data.answer || "抱歉，我没太理解，可以换个说法再问一次吗？");
            } else {
                addMsg("ai", "抱歉，客服暂时没能回答（" + ((d && d.msg) || "服务繁忙") +
                    "）。\n你可以点下方「留下联系方式」，让顾问直接联系你。");
            }
        } catch (e) {
            removeTyping(typing);
            addMsg("ai", "网络连接失败，暂时联系不上客服。\n你可以点下方「留下联系方式」，顾问会主动找你。");
        } finally {
            state.sending = false;
            sendBtn.disabled = false;
            saveMsgs();
            input.focus();
        }
    }

    /* ---------------- 打开 / 收起 ---------------- */
    function openPanel() {
        panel.classList.remove("hide");
        teaser.classList.add("hide");
        dot.classList.add("hide");
        if (!state.inited) {
            state.inited = true;
            if (state.msgs.length) {
                state.msgs.forEach(function (m) { addMsg(m.who, m.text, false); });
            } else {
                addMsg("ai", WELCOME);
                renderQuick();
            }
        }
        setTimeout(function () { input.focus(); scrollBottom(); }, 60);
    }

    function closePanel() {
        panel.classList.add("hide");
        leadBox.classList.add("hide");
    }

    ball.onclick = function () {
        if (panel.classList.contains("hide")) openPanel();
        else closePanel();
    };
    document.getElementById("ywClose").onclick = closePanel;

    /* ---------------- 输入 ---------------- */
    input.addEventListener("input", function () {
        input.style.height = "40px";
        input.style.height = Math.min(input.scrollHeight, 96) + "px";
    });
    input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            send(input.value);
        }
    });
    sendBtn.onclick = function () { send(input.value); };

    /* ---------------- 留资 ---------------- */
    function leadMsg(text, cls) {
        var el = document.getElementById("ywLeadMsg");
        el.textContent = text || "";
        el.className = "yw-lead-msg" + (cls ? " " + cls : "");
    }

    document.getElementById("ywLeadBtn").onclick = function () {
        leadBox.classList.remove("hide");
        leadMsg("");
        setTimeout(function () { document.getElementById("ywLeadName").focus(); }, 50);
    };
    document.getElementById("ywLeadCancel").onclick = function () {
        leadBox.classList.add("hide");
    };
    document.getElementById("ywLeadOk").onclick = async function () {
        var name = document.getElementById("ywLeadName").value.trim();
        var contact = document.getElementById("ywLeadContact").value.trim();
        var country = document.getElementById("ywLeadCountry").value.trim();
        if (!name || !contact) { leadMsg("请填写称呼和联系方式", "err"); return; }
        var btn = this;
        btn.disabled = true;
        leadMsg("提交中...");
        try {
            var res = await fetch(API + "/api/lead/submit", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    customer_name: name,
                    contact_info: contact,
                    target_country: country,
                    background_info: "官网悬浮客服留资"
                })
            });
            var d = await res.json();
            if (d && d.code === 200) {
                leadMsg("提交成功，顾问会尽快联系你 ✅", "ok");
                document.getElementById("ywLeadName").value = "";
                document.getElementById("ywLeadContact").value = "";
                document.getElementById("ywLeadCountry").value = "";
                setTimeout(function () { leadBox.classList.add("hide"); }, 1800);
            } else {
                leadMsg((d && d.msg) || "提交失败，请稍后再试", "err");
            }
        } catch (e) {
            leadMsg("网络异常，请稍后再试", "err");
        } finally {
            btn.disabled = false;
        }
    };

    // 深链唤起：?yw=open 或 #ask 直接展开面板（官网按钮/外部链接可用）
    if (/[?&]yw=open/.test(location.search) || location.hash === "#ask") {
        setTimeout(openPanel, 500);
    }

    // 页面加载 1.2 秒后若未交互，显示一次气泡提示（同一会话只提示一次）
    if (!sessionStorage.getItem("yw_teased")) {
        setTimeout(function () {
            if (!panel.classList.contains("hide")) return;
            teaser.classList.remove("hide");
            sessionStorage.setItem("yw_teased", "1");
            setTimeout(function () { teaser.classList.add("hide"); }, 4200);
        }, 1200);
    } else {
        teaser.classList.add("hide");
    }
})();
