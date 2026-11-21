// 使用无头 Edge 验证学生端“留学方案”导航和“带入具体方案申请”流程。
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9224;
const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), "yuejiao-student-"));
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
        `--user-data-dir=${profileDir}`, "http://127.0.0.1:8000/student.html",
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
        await sleep(700);
        await evaluate(`
            fetch('/api/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: 'demo_student01', password: 'Demo@2026'})
            }).then(r => r.json()).then(d => {
                if (d.code !== 200) throw new Error(d.msg || '学生登录失败');
                Session.set(d.data);
                enterWorkspace(d.data);
                return true;
            })
        `);
        await sleep(500);
        const navPASS = await evaluate(`
            (() => {
                const item = document.querySelector('.nav-item[data-page="programs"]');
                if (!item) return false;
                item.click();
                return document.getElementById('page-programs').classList.contains('active');
            })()
        `);
        await sleep(500);
        const germanyCount = await evaluate(`
            (() => {
                document.getElementById('programCountryFilter').value = '德国';
                renderStudentPrograms();
                return document.querySelectorAll('#studentProgramList .panel').length;
            })()
        `);
        const applyPASS = await evaluate(`
            (() => {
                const plan = window.studentPrograms.find(function (item) {
                    return item.country === '德国' && item.project_name.indexOf('医疗健康') >= 0;
                });
                if (!plan) return false;
                return applySpecificProgram(plan.id).then(function () {
                    return document.getElementById('page-application').classList.contains('active')
                        && document.getElementById('applyProgram').value === plan.project_name;
                });
            })()
        `);
        console.log("学生留学方案导航:", navPASS ? "PASS" : "FAIL");
        console.log("德国具体方案数量:", germanyCount);
        console.log("具体方案带入申请:", applyPASS ? "PASS" : "FAIL");
        if (!navPASS || germanyCount < 6 || !applyPASS) process.exitCode = 1;
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

