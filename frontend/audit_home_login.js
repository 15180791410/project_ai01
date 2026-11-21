// 验证官网右上角登录入口和服务体系登录按钮清理。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9225;
const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), "yuejiao-home-"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitTarget() {
    for (let i = 0; i < 40; i++) {
        try {
            const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
            const page = targets.find((item) => item.type === "page");
            if (page) return page;
        } catch (error) {}
        await sleep(250);
    }
    throw new Error("Edge 调试端口未就绪");
}

async function main() {
    const edge = spawn(EDGE, [
        "--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
        `--user-data-dir=${profileDir}`, "http://127.0.0.1:8000/",
    ], { windowsHide: true, stdio: "ignore" });
    try {
        const target = await waitTarget();
        const ws = new WebSocket(target.webSocketDebuggerUrl);
        const pending = new Map();
        let sequence = 0;
        ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            if (!pending.has(message.id)) return;
            const task = pending.get(message.id);
            pending.delete(message.id);
            if (message.error) task.reject(new Error(JSON.stringify(message.error)));
            else task.resolve(message.result);
        };
        await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
        const send = (method, params = {}) => {
            const id = ++sequence;
            ws.send(JSON.stringify({ id, method, params }));
            return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
        };
        const evaluate = async (expression) => {
            const result = await send("Runtime.evaluate", {
                expression, awaitPromise: true, returnByValue: true,
            });
            if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
            return result.result.value;
        };
        await send("Runtime.enable");
        await sleep(600);
        const menuPASS = await evaluate(`
            (() => {
                Session.clear();
                location.reload();
                return true;
            })()
        `);
        await sleep(700);
        const opened = await evaluate(`
            (() => {
                const button = document.querySelector('.login-toggle');
                if (!button) return false;
                button.click();
                const menu = document.getElementById('loginMenu');
                return document.getElementById('loginEntry').classList.contains('open')
                    && getComputedStyle(menu).display !== 'none';
            })()
        `);
        const serviceButtons = await evaluate(`
            document.querySelectorAll('.pcard.student .p-btn, .pcard.admin .p-btn').length
        `);
        const authed = await evaluate(`
            (() => {
                Session.set({token:'test-token', user_type:'STUDENT', real_name:'测试学生', role_label:'学生'});
                renderTopLogin();
                return !!document.querySelector('.top-auth-user')
                    && !document.querySelector('.login-toggle')
                    && (document.querySelector('.top-auth-user a') || {}).getAttribute('href') === 'student.html';
            })()
        `);
        console.log("右上角登录菜单:", menuPASS && opened ? "PASS" : "FAIL");
        console.log("服务体系登录按钮数量:", serviceButtons);
        console.log("已登录顶部状态:", authed ? "PASS" : "FAIL");
        if (!menuPASS || !opened || serviceButtons !== 0 || !authed) process.exitCode = 1;
        ws.close();
    } finally {
        edge.kill();
        await sleep(300);
        const tempRoot = path.resolve(os.tmpdir());
        const resolved = path.resolve(profileDir);
        if (resolved.startsWith(tempRoot + path.sep)) {
            fs.rmSync(resolved, { recursive: true, force: true });
        }
    }
}

main().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
});

