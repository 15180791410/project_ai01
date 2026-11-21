/* 企业工作台视觉回归：使用本机无头 Edge 登录真实角色并截图。
   目的不是比对像素，而是确认图表挂载、无运行时错误、页面确实可滚动。 */
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9226;
const ROOT = path.resolve(__dirname, "..");
const PROFILE = fs.mkdtempSync(path.join(os.tmpdir(), "yuejiao-visual-"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitTarget() {
    for (let i = 0; i < 50; i++) {
        try {
            const targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
            const page = targets.find((item) => item.type === "page");
            if (page) return page;
        } catch (error) {}
        await sleep(200);
    }
    throw new Error("Edge 调试端口未就绪");
}

async function main() {
    const edge = spawn(EDGE, [
        "--headless=new", "--disable-gpu", `--remote-debugging-port=${PORT}`,
        `--user-data-dir=${PROFILE}`, "http://127.0.0.1:8000/admin.html",
        "--window-size=1600,1100",
    ], { windowsHide: true, stdio: "ignore" });
    const runtimeErrors = [];
    try {
        const target = await waitTarget();
        const ws = new WebSocket(target.webSocketDebuggerUrl);
        const pending = new Map();
        let sequence = 0;
        ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            if (message.method === "Runtime.exceptionThrown") {
                runtimeErrors.push(message.params.exceptionDetails.text || "Runtime exception");
            }
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
        const screenshot = async (name) => {
            const result = await send("Page.captureScreenshot", {
                format: "png", captureBeyondViewport: false,
            });
            fs.writeFileSync(path.join(ROOT, "docs", name), Buffer.from(result.data, "base64"));
        };
        await send("Runtime.enable");
        await send("Page.enable");
        // 无头 Edge 首次启动可能先停在 about:blank，等页面导航到项目地址
        // 后再执行登录，避免相对 /api 路径解析失败。
        await sleep(900);

        await evaluate(`
            fetch('/api/auth/login', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: 'admin', password: 'Admin@2026'})
            }).then(r => r.json()).then(d => {
                Session.set(d.data); user = d.data; enterWorkspace(d.data);
            })
        `);
        await sleep(1800);
        const adminCharts = await evaluate(`({
            charts: document.querySelectorAll('.chart-panel').length,
            marks: document.querySelectorAll('.donut-ring,.yj-bar-row,.funnel-stage,.tree-branch').length,
            overviewVisible: document.getElementById('page-overview').classList.contains('active')
        })`);
        await screenshot("visual_admin_dashboard.png");
        await evaluate(`document.querySelector('#adminPriorityChart [data-yj-index="0"]').click()`);
        await sleep(350);
        const adminPreview = await evaluate(`({
            opened: !!document.querySelector('.preview-box'),
            records: document.querySelectorAll('.preview-record').length
        })`);
        await screenshot("visual_admin_preview.png");
        await evaluate(`document.querySelector('.preview-box .preview-close').click()`);
        await evaluate(`document.querySelector('#adminAppChart [data-yj-index="7"]').click()`);
        await sleep(300);
        const applicationPreview = await evaluate(`({
            title: document.querySelector('.preview-head h3')?.textContent || '',
            records: document.querySelectorAll('.preview-record').length
        })`);
        await evaluate(`document.querySelector('.preview-box .preview-close').click()`);
        await evaluate(`document.querySelector('#adminAlertChart [data-yj-index="0"]').click()`);
        await sleep(300);
        const alertPreview = await evaluate(`({
            title: document.querySelector('.preview-head h3')?.textContent || '',
            records: document.querySelectorAll('.preview-record').length
        })`);
        await evaluate(`document.querySelector('.preview-box .preview-close').click()`);
        await evaluate(`goAdminPage('students')`);
        await sleep(900);
        const lifecyclePanel = await evaluate(`({
            cards: document.querySelectorAll('#countryGroups .info-item').length,
            graduationPanel: document.getElementById('graduationReviewPanel')?.style.display !== 'none',
            lifecycleButtons: document.querySelectorAll('#countryGroups button[onclick^="viewLifecycle"]').length
        })`);
        await screenshot("visual_admin_students_lifecycle.png");
        await evaluate(`goAdminPage('application')`);
        await sleep(1100);
        await evaluate(`
            (() => {
                const row = Array.from(document.querySelectorAll('#appBody tr'))
                    .find(item => item.textContent.includes('林子涵'));
                (row || document.querySelector('#appBody tr'))
                    .querySelector('button[onclick^="openApplicationArchive"]').click();
            })()
        `);
        await sleep(900);
        await evaluate(`
            Array.from(document.querySelectorAll('#applicationArchiveMask .archive-tab'))
                .find(item => item.textContent.includes('材料准备')).click()
        `);
        await sleep(500);
        const adminArchive = await evaluate(`({
            opened: !!document.getElementById('applicationArchiveMask'),
            tabs: document.querySelectorAll('#applicationArchiveMask .archive-tab').length,
            fields: document.querySelectorAll('#applicationArchiveMask .archive-field').length,
            materialPanel: !!document.querySelector('#applicationArchiveMask .archive-materials'),
            materials: document.querySelectorAll('#applicationArchiveMask .archive-material').length
        })`);
        await screenshot("visual_admin_application_archive.png");
        await evaluate(`closeApplicationArchive()`);
        await evaluate(`goAdminPage('leads')`);
        await sleep(900);
        const leadWorkload = await evaluate(`({
            bars: document.querySelectorAll('#leadOwnerLoadChart .yj-bar-row').length,
            active: document.getElementById('page-leads').classList.contains('active')
        })`);
        await screenshot("visual_admin_leads.png");

        await evaluate(`
            fetch('/api/auth/login', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: 'teacher01', password: 'Teacher@2026'})
            }).then(r => r.json()).then(d => {
                Session.set(d.data); user = d.data; enterWorkspace(d.data);
            })
        `);
        await sleep(1500);
        const homeroomCharts = await evaluate(`({
            charts: document.querySelectorAll('.chart-panel').length,
            marks: document.querySelectorAll('.donut-ring,.yj-bar-row,.funnel-stage,.tree-branch').length,
            role: document.getElementById('empChip').textContent
        })`);
        await screenshot("visual_homeroom_dashboard.png");

        await send("Page.navigate", { url: "http://127.0.0.1:8000/student.html" });
        await sleep(900);
        await evaluate(`
            fetch('/api/auth/login', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: 'student01', password: 'Demo@2026'})
            }).then(r => r.json()).then(d => {
                Session.set(d.data); user = d.data; enterWorkspace(d.data);
            })
        `);
        await sleep(1800);
        const studentCharts = await evaluate(`({
            charts: document.querySelectorAll('.chart-panel').length,
            marks: document.querySelectorAll('.donut-ring,.yj-bar-row,.funnel-stage,.line-dot,.tree-branch').length
        })`);
        await screenshot("visual_student_dashboard.png");
        await evaluate(`document.querySelector('#studentScoreChart [data-yj-index="0"]').click()`);
        await sleep(300);
        const studentPreview = await evaluate(`({
            opened: !!document.querySelector('.preview-box'),
            records: document.querySelectorAll('.preview-record').length
        })`);
        await screenshot("visual_student_preview.png");
        await evaluate(`document.querySelector('.preview-box .preview-close').click(); goPage('application');`);
        await sleep(1200);
        await evaluate(`document.querySelector('#applicationList .tl-open').click()`);
        await sleep(900);
        await evaluate(`
            Array.from(document.querySelectorAll('#applicationArchiveMask .archive-tab'))
                .find(item => item.textContent.includes('材料准备')).click()
        `);
        await sleep(500);
        const studentArchive = await evaluate(`({
            opened: !!document.getElementById('applicationArchiveMask'),
            tabs: document.querySelectorAll('#applicationArchiveMask .archive-tab').length,
            fields: document.querySelectorAll('#applicationArchiveMask .archive-field').length,
            materials: document.querySelectorAll('#applicationArchiveMask .archive-material').length
        })`);
        await screenshot("visual_student_application_archive.png");
        await evaluate(`closeApplicationArchive(); switchPage('emotion'); loadEmotionRecords();`);
        await sleep(700);
        const emotionChart = await evaluate(`({
            dots: document.querySelectorAll('#emotionTrendChart .line-dot').length,
            rows: document.querySelectorAll('#emotionRecordBody tr').length
        })`);
        await screenshot("visual_student_emotion.png");
        await evaluate(`
            fetch('/api/auth/login', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({username: 'demo_student01', password: 'Demo@2026'})
            }).then(r => r.json()).then(d => {
                Session.set(d.data); user = d.data; enterWorkspace(d.data);
            })
        `);
        await sleep(900);
        const consultLock = await evaluate(`
            (() => {
                const tile = document.querySelector('.quick-tile[data-mod="emotion"]');
                goPage('emotion');
                return {
                    stage: stageInfo ? stageInfo.stage : '',
                    lockedTile: !!(tile && tile.classList.contains('locked')),
                    lockedPage: document.getElementById('page-locked').classList.contains('active')
                };
            })()
        `);
        await screenshot("visual_student_consult_locked.png");

        await send("Page.navigate", { url: "http://127.0.0.1:8000/index.html" });
        await sleep(1200);
        const homeTree = await evaluate(`({
            treeBranches: document.querySelectorAll('#homeServiceTree .tree-branch').length,
            hero: !!document.querySelector('.hero-wrap')
        })`);
        await screenshot("visual_home.png");

        await send("Page.navigate", { url: "http://127.0.0.1:8000/service.html" });
        await sleep(900);
        const serviceEntry = await evaluate(`({
            hero: !!document.querySelector('.service-hero'),
            crmFab: !!document.getElementById('crmLeadFab'),
            visitorCenterRemoved: !document.getElementById('page-visitor') &&
                !document.getElementById('navVisitor'),
            assistant: !!document.getElementById('ywBall')
        })`);
        await evaluate(`document.getElementById('crmLeadFab').click()`);
        await sleep(250);
        const crmLeadForm = await evaluate(`({
            opened: !!document.querySelector('.modal-box'),
            title: document.querySelector('.modal-box h3')?.textContent || '',
            fields: document.querySelectorAll('.modal-box [data-key]').length
        })`);
        await screenshot("visual_service_crm_entry.png");
        await evaluate(`document.querySelector('.modal-box #fbCancel')?.click()`);
        await screenshot("visual_service.png");

        console.log("admin", JSON.stringify(adminCharts));
        console.log("adminPreview", JSON.stringify(adminPreview));
        console.log("applicationPreview", JSON.stringify(applicationPreview));
        console.log("alertPreview", JSON.stringify(alertPreview));
        console.log("lifecyclePanel", JSON.stringify(lifecyclePanel));
        console.log("adminArchive", JSON.stringify(adminArchive));
        console.log("leadWorkload", JSON.stringify(leadWorkload));
        console.log("homeroom", JSON.stringify(homeroomCharts));
        console.log("student", JSON.stringify(studentCharts));
        console.log("studentPreview", JSON.stringify(studentPreview));
        console.log("studentArchive", JSON.stringify(studentArchive));
        console.log("emotionChart", JSON.stringify(emotionChart));
        console.log("consultLock", JSON.stringify(consultLock));
        console.log("home", JSON.stringify(homeTree));
        console.log("serviceEntry", JSON.stringify(serviceEntry));
        console.log("crmLeadForm", JSON.stringify(crmLeadForm));
        console.log("runtimeErrors", runtimeErrors.length);
        if (adminCharts.marks < 8 || !adminPreview.opened || adminPreview.records < 1 ||
            !applicationPreview.records ||
            !alertPreview.records ||
            lifecyclePanel.cards < 1 || !lifecyclePanel.graduationPanel ||
            lifecyclePanel.lifecycleButtons < 1 ||
            !adminArchive.opened || adminArchive.tabs !== 8 || !adminArchive.materials ||
            leadWorkload.bars < 1 || homeroomCharts.marks < 4 || studentCharts.marks < 4 ||
            !studentPreview.opened || studentPreview.records < 1 ||
            !studentArchive.opened || studentArchive.tabs !== 8 ||
            !studentArchive.fields || !studentArchive.materials ||
            emotionChart.dots < 3 || emotionChart.rows < 3 ||
            consultLock.stage !== "CONSULT" || !consultLock.lockedTile || !consultLock.lockedPage ||
            homeTree.treeBranches !== 3 ||
            !serviceEntry.hero || !serviceEntry.crmFab ||
            !serviceEntry.visitorCenterRemoved || !serviceEntry.assistant ||
            !crmLeadForm.opened || crmLeadForm.title !== "留资给顾问" ||
            crmLeadForm.fields < 4 ||
            runtimeErrors.length) {
            process.exitCode = 1;
        }
        ws.close();
    } finally {
        edge.kill();
        await sleep(300);
        const tempRoot = path.resolve(os.tmpdir());
        const resolved = path.resolve(PROFILE);
        if (resolved.startsWith(tempRoot + path.sep)) {
            fs.rmSync(resolved, { recursive: true, force: true });
        }
    }
}

main().catch((error) => {
    console.error(error.stack || error.message);
    process.exitCode = 1;
});
