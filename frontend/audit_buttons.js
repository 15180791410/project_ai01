// 静态检查：页面 onclick 调用的函数是否真的存在。
const fs = require("fs");

const files = [
    "index.html",
    "student.html",
    "admin.html",
    "service.html",
];
const common = fs.readFileSync("static/common.js", "utf8");
const globalDefs = new Set(
    [...common.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[1])
);

let failed = false;
for (const file of files) {
    const source = fs.readFileSync(file, "utf8");
    const defs = new Set(
        [...source.matchAll(/function\s+([A-Za-z_$][\w$]*)\s*\(/g)].map((m) => m[1])
    );
    const calls = [...source.matchAll(/onclick=["']\s*([A-Za-z_$][\w$]*)\s*\(/g)]
        .map((m) => m[1]);
    const missing = [...new Set(
        calls.filter((name) => !defs.has(name) && !globalDefs.has(name))
    )];
    if (missing.length) failed = true;
    console.log(file, "按钮处理函数", new Set(calls).size, "缺失", missing.join(",") || "无");
}

process.exit(failed ? 1 : 0);
