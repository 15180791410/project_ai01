// 使用本机 Edge 的 DevTools Protocol 实际点击后台导航。
// 运行前提：后端已在 http://127.0.0.1:8000 启动。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9223;
const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), "yuejiao-nav-"));

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForTarget() {
    for (let i = 0; i < 40; i++) {
        try {
            const response = await fetch(`http://127.0.0.1:${PORT}/json/list`);
            const targets = await response.json();
            const page = targets.find((item) => item.type === "page");
            if (page) return page;
        } catch (error) {
            // Edge 启动需要短暂时间。
        }
        await sleep(250);
    }
    throw new Error("Edge 调试端口未就绪");
}

async function main() {
    if (!fs.existsSync(EDGE)) throw new Error("未找到本机 Edge");
    const edge = spawn(EDGE, [
        "--headless=new",
        "--disable-gpu",
        `--remote-debugging-port=${PORT}`,
        `--user-data-dir=${profileDir}`,
        "http://127.0.0.1:8000/admin.html",
    ], { windowsHide: true, stdio: "ignore" });

    try {
        const target = await waitForTarget();
        const ws = new WebSocket(target.webSocketDebuggerUrl);
        const pending = new Map();
        let sequence = 0;
        ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            if (!message.id || !pending.has(message.id)) return;
            const { resolve, reject } = pending.get(message.id);
            pending.delete(message.id);
            if (message.error) reject(new Error(JSON.stringify(message.error)));
            else resolve(message.result);
        };
        await new Promise((resolve, reject) => {
            ws.onopen = resolve;
            ws.onerror = reject;
        });
        function send(method, params = {}) {
            const id = ++sequence;
            ws.send(JSON.stringify({ id, method, params }));
            return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
        }
        async function evaluate(expression) {
            const result = await send("Runtime.evaluate", {
                expression, awaitPromise: true, returnByValue: true,
            });
            if (result.exceptionDetails) {
                throw new Error(result.exceptionDetails.text || "页面脚本执行失败");
            }
            return result.result.value;
        }

        await send("Runtime.enable");
        await sleep(800);
        await evaluate(`
            fetch('/api/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: 'admin', password: 'Admin@2026'})
            }).then(r => r.json()).then(d => {
                if (d.code !== 200) throw new Error(d.msg || '登录失败');
                return fetch('/api/auth/switch-role', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({token: d.data.token, role: 'HOMEROOM'})
                });
            }).then(r => r.json()).then(d => {
                if (d.code !== 200) throw new Error(d.msg || '切换班主任失败');
                Session.set(d.data);
                enterWorkspace(d.data);
                return true;
            })
        `);
        await sleep(600);
        const navCount = await evaluate(`document.querySelectorAll('.ws-side .nav-item[data-page]').length`);
        const clicked = await evaluate(`
            (() => {
                const item = document.querySelector('.ws-side .nav-item[data-page="students"]');
                if (!item) return false;
                item.click();
                return document.getElementById('page-students').classList.contains('active');
            })()
        `);
        const keyboard = await evaluate(`
            (() => {
                const item = document.querySelector('.ws-side .nav-item[data-page="alerts"]');
                if (!item) return false;
                item.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
                return document.getElementById('page-alerts').classList.contains('active');
            })()
        `);
        const feedback = await evaluate(`
            (() => {
                const item = document.querySelector('.ws-side .nav-item[data-page="feedback"]');
                if (!item) return false;
                item.click();
                return document.getElementById('page-feedback').classList.contains('active');
            })()
        `);
        await sleep(700);
        const feedbackLoaded = await evaluate(`
            (() => {
                const body = document.getElementById('feedbackBody');
                return !!body && body.innerText.indexOf('加载中') < 0;
            })()
        `);
        console.log("导航项数量:", navCount);
        console.log("鼠标点击导航:", clicked ? "PASS" : "FAIL");
        console.log("键盘激活导航:", keyboard ? "PASS" : "FAIL");
        console.log("售后服务导航与列表:", feedback && feedbackLoaded ? "PASS" : "FAIL");
        ws.close();
        if (!clicked || !keyboard || !feedback || !feedbackLoaded) process.exitCode = 1;
    } finally {
        edge.kill();
        await sleep(300);
        const tempRoot = path.resolve(os.tmpdir());
        const resolvedProfile = path.resolve(profileDir);
        if (resolvedProfile.startsWith(tempRoot + path.sep)) {
            fs.rmSync(resolvedProfile, { recursive: true, force: true });
        }
    }
}

main().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
});

