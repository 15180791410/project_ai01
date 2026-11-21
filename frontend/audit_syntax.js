// 前端脚本静态语法检查。
//
// 项目没有构建工具，浏览器直接执行 HTML 中的内联脚本。因此这里用 Node 的
// Function 构造器做语法解析：只解析、不执行页面代码，也不会访问网络或数据库。
const fs = require("fs");

const htmlFiles = ["index.html", "student.html", "admin.html", "service.html"];
let failed = false;

function check(label, source) {
    try {
        // new Function 会编译函数体；若括号、引号或语句不完整会立即抛错。
        new Function(source);
        console.log("[PASS]", label);
    } catch (error) {
        failed = true;
        console.error("[FAIL]", label, error.message);
    }
}

// common.js 是被所有页面引用的公共脚本，单独检查一次。
check("static/common.js", fs.readFileSync("static/common.js", "utf8"));

for (const file of htmlFiles) {
    const html = fs.readFileSync(file, "utf8");
    // 只取没有 src 属性的内联 <script>，外部脚本由 Node 直接检查。
    const pattern = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
    let index = 0;
    for (const match of html.matchAll(pattern)) {
        index += 1;
        check(file + " inline#" + index, match[1]);
    }
}

process.exit(failed ? 1 : 0);
