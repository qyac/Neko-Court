/**
 * selftest_review_site_ui.mjs
 *
 * 临时审核群 · 独立审核网站前端的自检脚本（纯函数 + 静态契约）。
 * 只使用 Node 标准库，运行方式：
 *   "C:\Users\haoxu\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe" selftest_review_site_ui.mjs
 *
 * 说明：Node 会把没有 package.json 的 .js 当 CJS，因此先把 review_web/static/form.js
 * 复制到系统临时目录下的 form-under-test.mjs，再动态 import，用完删除。
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const SITE_DIR = path.join(ROOT, "astrbot_plugin_temp_review_group", "review_web");
const TEMPLATE_DIR = path.join(SITE_DIR, "templates");
const STATIC_DIR = path.join(SITE_DIR, "static");

const APPLY_HTML = path.join(TEMPLATE_DIR, "apply.html");
const LOGIN_HTML = path.join(TEMPLATE_DIR, "login.html");
const ADMIN_HTML = path.join(TEMPLATE_DIR, "admin.html");
const STYLE_CSS = path.join(STATIC_DIR, "style.css");
const FORM_JS = path.join(STATIC_DIR, "form.js");
const APP_JS = path.join(STATIC_DIR, "app.js");

let passed = 0;
let failed = 0;

function check(name, fn) {
  try {
    const result = fn();
    if (result === false) throw new Error("断言返回 false");
    passed += 1;
    console.log(`PASS  ${name}`);
  } catch (err) {
    failed += 1;
    const message = err && err.message ? err.message : String(err);
    console.log(`FAIL  ${name}`);
    console.log(`      ${message.split("\n").join("\n      ")}`);
  }
}

function info(message) {
  console.log(`INFO  ${message}`);
}

function readText(file) {
  return fs.readFileSync(file, "utf8");
}

/* ------------------------------------------------------------------ *
 * 载入被测模块
 * ------------------------------------------------------------------ */

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "dsh-review-site-ui-"));
const tmpFile = path.join(tmpDir, "form-under-test.mjs");
fs.copyFileSync(FORM_JS, tmpFile);

let F;
try {
  F = await import(pathToFileURL(tmpFile).href);
} finally {
  try {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* 临时文件清理失败不影响测试结论 */
  }
}

info(`被测模块：${path.relative(ROOT, FORM_JS)}`);

/* ------------------------------------------------------------------ *
 * 1. extractUid
 * ------------------------------------------------------------------ */

check("extractUid: 纯数字 UID", () => {
  assert.equal(F.extractUid("12345678"), "12345678");
});

check("extractUid: 带空格与 UID: 前缀", () => {
  assert.equal(F.extractUid(" UID:12345678 "), "12345678");
});

check("extractUid: space.bilibili.com 主页链接", () => {
  assert.equal(F.extractUid("https://space.bilibili.com/12345678"), "12345678");
});

check("extractUid: uid= 查询串", () => {
  assert.equal(F.extractUid("uid=998877"), "998877");
});

check("extractUid: 普通文本里的单个数字 → null", () => {
  assert.equal(F.extractUid("我有 3 个号"), null);
});

check("extractUid: 空串 → null", () => {
  assert.equal(F.extractUid(""), null);
  assert.equal(F.extractUid("   "), null);
});

check("extractUid: 11 位数字 → null", () => {
  assert.equal(F.extractUid("12345678901"), null);
});

check("extractUid: 1 位数字 → null", () => {
  assert.equal(F.extractUid("7"), null);
});

check("extractUid: 链接带查询参数 / 结尾斜杠 / 大小写", () => {
  assert.equal(F.extractUid("https://space.bilibili.com/12345678?from=search"), "12345678");
  assert.equal(F.extractUid("space.bilibili.com/998877/"), "998877");
  assert.equal(F.extractUid("HTTPS://SPACE.BILIBILI.COM/1234567"), "1234567");
});

check("extractUid: 非数字文本 / null / undefined → null", () => {
  assert.equal(F.extractUid("abc"), null);
  assert.equal(F.extractUid("UID:"), null);
  assert.equal(F.extractUid(null), null);
  assert.equal(F.extractUid(undefined), null);
});

/* ------------------------------------------------------------------ *
 * 2. validateForm
 * ------------------------------------------------------------------ */

check("validateForm: 合法输入通过", () => {
  const out = F.validateForm({ qq: "12345", uid: "12345678", note: "" });
  assert.equal(out.ok, true);
  assert.deepEqual(out.errors, {});
});

check("validateForm: 合法输入（UID 为主页链接 + 备注）", () => {
  const out = F.validateForm({
    qq: "123456789012",
    uid: "https://space.bilibili.com/12345678",
    note: "群友推荐",
  });
  assert.equal(out.ok, true);
  assert.deepEqual(out.errors, {});
});

check("validateForm: QQ 非数字报错", () => {
  const out = F.validateForm({ qq: "12a45", uid: "12345678" });
  assert.equal(out.ok, false);
  assert.match(out.errors.qq, /数字/);
});

check("validateForm: QQ 过短 / 过长报错", () => {
  assert.equal(F.validateForm({ qq: "1234", uid: "12345678" }).ok, false);
  assert.equal(F.validateForm({ qq: "1234567890123", uid: "12345678" }).ok, false);
});

check("validateForm: QQ 缺失报错", () => {
  const out = F.validateForm({ qq: "", uid: "12345678" });
  assert.equal(out.ok, false);
  assert.ok(out.errors.qq);
});

check("validateForm: UID 非法报错", () => {
  const out = F.validateForm({ qq: "12345", uid: "我的B站号" });
  assert.equal(out.ok, false);
  assert.ok(out.errors.uid);
  assert.equal(F.validateForm({ qq: "12345", uid: "1" }).ok, false);
});

check("validateForm: UID 缺失报错", () => {
  const out = F.validateForm({ qq: "12345", uid: "" });
  assert.equal(out.ok, false);
  assert.ok(out.errors.uid);
});

check("validateForm: 备注超长（>200）报错", () => {
  const out = F.validateForm({ qq: "12345", uid: "12345678", note: "啊".repeat(201) });
  assert.equal(out.ok, false);
  assert.ok(out.errors.note);
});

check("validateForm: 备注正好 200 字通过", () => {
  const out = F.validateForm({ qq: "12345", uid: "12345678", note: "啊".repeat(200) });
  assert.equal(out.ok, true);
});

check("validateForm: 空对象不抛异常", () => {
  const out = F.validateForm();
  assert.equal(out.ok, false);
  assert.ok(out.errors.qq && out.errors.uid);
});

/* ------------------------------------------------------------------ *
 * 3. 状态枚举
 * ------------------------------------------------------------------ */

check("isTerminal: 全枚举", () => {
  assert.equal(F.isTerminal("approved"), true);
  assert.equal(F.isTerminal("rejected"), true);
  assert.equal(F.isTerminal("manual"), true);
  assert.equal(F.isTerminal("pending"), false);
  assert.equal(F.isTerminal("blocked"), false);
  assert.equal(F.isTerminal("whatever"), false);
});

check("statusLabel: 全枚举", () => {
  assert.equal(F.statusLabel("pending"), "待核验");
  assert.equal(F.statusLabel("approved"), "已通过");
  assert.equal(F.statusLabel("rejected"), "未通过");
  assert.equal(F.statusLabel("manual"), "待人工");
  assert.equal(F.statusLabel("blocked"), "已拉黑");
  assert.equal(F.statusLabel("nope"), "未知");
});

check("statusClass: 全枚举", () => {
  assert.equal(F.statusClass("pending"), "pending");
  assert.equal(F.statusClass("approved"), "approved");
  assert.equal(F.statusClass("rejected"), "rejected");
  assert.equal(F.statusClass("manual"), "manual");
  assert.equal(F.statusClass("blocked"), "blocked");
  assert.equal(F.statusClass("nope"), "unknown");
  assert.equal(F.statusClass(undefined), "unknown");
});

/* ------------------------------------------------------------------ *
 * 4. formatTime / relativeTime
 * ------------------------------------------------------------------ */

check("formatTime: 秒级时间戳格式化为本地 YYYY-MM-DD HH:MM", () => {
  const ts = 1699999999;
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  const expected =
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  assert.equal(F.formatTime(ts), expected);
  assert.match(F.formatTime(ts), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
});

check("formatTime: 毫秒级时间戳与秒级一致", () => {
  assert.equal(F.formatTime(1699999999000), F.formatTime(1699999999));
});

check("formatTime: 空/非法 → —", () => {
  assert.equal(F.formatTime(null), "—");
  assert.equal(F.formatTime(undefined), "—");
  assert.equal(F.formatTime(""), "—");
  assert.equal(F.formatTime("abc"), "—");
  assert.equal(F.formatTime(NaN), "—");
  assert.equal(F.formatTime(0), "—");
  assert.equal(F.formatTime(-1), "—");
});

check("relativeTime: 59 秒 → 刚刚", () => {
  const ts = 1700000000;
  assert.equal(F.relativeTime(ts, ts + 59), "刚刚");
  assert.equal(F.relativeTime(ts, ts), "刚刚");
});

check("relativeTime: 60 秒 → 1 分钟前", () => {
  const ts = 1700000000;
  assert.equal(F.relativeTime(ts, ts + 60), "1 分钟前");
  assert.equal(F.relativeTime(ts, ts + 61), "1 分钟前");
});

check("relativeTime: 59 分 → 59 分钟前，59 分 59 秒仍是分钟", () => {
  const ts = 1700000000;
  assert.equal(F.relativeTime(ts, ts + 59 * 60), "59 分钟前");
  assert.equal(F.relativeTime(ts, ts + 3599), "59 分钟前");
});

check("relativeTime: 1 小时 / 23 小时 / 24 小时边界", () => {
  const ts = 1700000000;
  assert.equal(F.relativeTime(ts, ts + 3600), "1 小时前");
  assert.equal(F.relativeTime(ts, ts + 86399), "23 小时前");
  assert.equal(F.relativeTime(ts, ts + 86400), "1 天前");
});

check("relativeTime: 3 天 → 3 天前", () => {
  const ts = 1700000000;
  assert.equal(F.relativeTime(ts, ts + 3 * 86400), "3 天前");
});

check("relativeTime: 未来时间按 0 处理 / 空值 → —", () => {
  const ts = 1700000000;
  assert.equal(F.relativeTime(ts, ts - 500), "刚刚");
  assert.equal(F.relativeTime(0), "—");
  assert.equal(F.relativeTime(null), "—");
});

/* ------------------------------------------------------------------ *
 * 5. buildApplyPayload
 * ------------------------------------------------------------------ */

check("buildApplyPayload: 去空格、UID 取数字、note 缺省为空串", () => {
  assert.deepEqual(F.buildApplyPayload({ qq: " 12345 ", uid: "https://space.bilibili.com/12345678" }), {
    qq: "12345",
    uid: "12345678",
    note: "",
  });
});

check("buildApplyPayload: uid= 链接与备注去空格", () => {
  assert.deepEqual(F.buildApplyPayload({ qq: "12345", uid: " uid=998877 ", note: " 你好 " }), {
    qq: "12345",
    uid: "998877",
    note: "你好",
  });
});

check("buildApplyPayload: 非法 UID 退化为纯数字串", () => {
  assert.deepEqual(F.buildApplyPayload({ qq: "12345", uid: "abc" }), {
    qq: "12345",
    uid: "",
    note: "",
  });
  // `uid=12ab34` 里能识别出 uid 前缀后的前段数字，属于可接受的宽松行为
  assert.equal(F.buildApplyPayload({ qq: "12345", uid: "uid=12ab34" }).uid, "12");
  // 完全无法识别时只保留数字字符
  assert.equal(F.buildApplyPayload({ qq: "12345", uid: "abc123" }).uid, "123");
});

check("buildApplyPayload: 空参数不抛异常", () => {
  assert.deepEqual(F.buildApplyPayload(), { qq: "", uid: "", note: "" });
});

/* ------------------------------------------------------------------ *
 * 6. buildDecisionPayload
 * ------------------------------------------------------------------ */

check("buildDecisionPayload: 带 csrf", () => {
  assert.deepEqual(F.buildDecisionPayload("tok-123", 12, "approve", ""), {
    csrf: "tok-123",
    id: 12,
    action: "approve",
    note: "",
  });
});

check("buildDecisionPayload: 数字字符串 id 归一化、note 去空格", () => {
  assert.deepEqual(F.buildDecisionPayload("t", "12", "block", " 违规 "), {
    csrf: "t",
    id: 12,
    action: "block",
    note: "违规",
  });
});

check("buildDecisionPayload: csrf 缺失为空串", () => {
  assert.equal(F.buildDecisionPayload(null, 1, "delete").csrf, "");
  assert.equal(F.buildDecisionPayload(undefined, 1, "delete").note, "");
});

/* ------------------------------------------------------------------ *
 * 7. summarize
 * ------------------------------------------------------------------ */

check("summarize: 空数组", () => {
  const out = F.summarize([]);
  assert.equal(out.total, 0);
  assert.equal(out.newestTs, 0);
  assert.equal(out.byStatus.pending, 0);
  assert.equal(out.byStatus.approved, 0);
  assert.equal(out.byStatus.rejected, 0);
  assert.equal(out.byStatus.manual, 0);
  assert.equal(out.byStatus.blocked, 0);
});

check("summarize: 非数组输入按空处理", () => {
  assert.equal(F.summarize(null).total, 0);
  assert.equal(F.summarize(undefined).total, 0);
});

check("summarize: 计数正确 + newestTs", () => {
  const items = [
    { id: 1, status: "approved", created_at: 1700000000 },
    { id: 2, status: "approved", created_at: 1700000500 },
    { id: 3, status: "pending", created_at: 1699999999 },
    { id: 4, status: "rejected", created_at: 0 },
    { id: 5, status: "manual" },
    { id: 6, status: "blocked", created_at: 1700000999 },
    { id: 7 },
  ];
  const out = F.summarize(items);
  assert.equal(out.total, 7);
  assert.equal(out.byStatus.approved, 2);
  assert.equal(out.byStatus.pending, 1);
  assert.equal(out.byStatus.rejected, 1);
  assert.equal(out.byStatus.manual, 1);
  assert.equal(out.byStatus.blocked, 1);
  assert.equal(out.byStatus.unknown, 1);
  assert.equal(out.newestTs, 1700000999);
});

/* ------------------------------------------------------------------ *
 * 8. nextOffset / prevOffset
 * ------------------------------------------------------------------ */

check("nextOffset: 常规推进", () => {
  assert.equal(F.nextOffset(0, 120, 50), 50);
  assert.equal(F.nextOffset(50, 120, 50), 100);
});

check("nextOffset: 最后一页不越界", () => {
  assert.equal(F.nextOffset(100, 120, 50), 100);
  assert.equal(F.nextOffset(5, 10, 5), 5);
});

check("nextOffset: total 为空/0", () => {
  assert.equal(F.nextOffset(0, 0, 50), 0);
  assert.equal(F.nextOffset(50, 0, 50), 0);
  assert.equal(F.nextOffset(0, null, 50), 0);
});

check("nextOffset: 负数与超过 total 的 offset", () => {
  assert.equal(F.nextOffset(-5, 10, 5), 5);
  assert.equal(F.nextOffset(100, 10, 50), 0);
  assert.equal(F.nextOffset("abc", 10, 5), 5);
});

check("prevOffset: 边界", () => {
  assert.equal(F.prevOffset(0, 50), 0);
  assert.equal(F.prevOffset(50, 50), 0);
  assert.equal(F.prevOffset(60, 50), 10);
  assert.equal(F.prevOffset(-3, 50), 0);
  assert.equal(F.prevOffset(100, 50), 50);
});

check("分页默认 pageSize 与后端一致（50）", () => {
  assert.equal(F.DEFAULT_PAGE_SIZE, 50);
  assert.equal(F.nextOffset(0, 120), 50);
  assert.equal(F.prevOffset(50), 0);
});

/* ------------------------------------------------------------------ *
 * 9. csvCell
 * ------------------------------------------------------------------ */

check("csvCell: 普通值原样返回", () => {
  assert.equal(F.csvCell("abc"), "abc");
  assert.equal(F.csvCell(123), "123");
  assert.equal(F.csvCell(""), "");
});

check("csvCell: 逗号加引号", () => {
  assert.equal(F.csvCell("a,b"), '"a,b"');
});

check("csvCell: 引号转义", () => {
  assert.equal(F.csvCell('a"b'), '"a""b"');
  assert.equal(F.csvCell('"'), '""""');
});

check("csvCell: 换行加引号", () => {
  assert.equal(F.csvCell("a\nb"), '"a\nb"');
  assert.equal(F.csvCell("a\r\nb"), '"a\r\nb"');
});

check("csvCell: 空值", () => {
  assert.equal(F.csvCell(null), "");
  assert.equal(F.csvCell(undefined), "");
});

/* ------------------------------------------------------------------ *
 * 10. 静态契约
 * ------------------------------------------------------------------ */

const ALLOWED_PLACEHOLDERS = {
  "apply.html": ["site_name", "site_subtitle", "group_line", "rules_line", "notice_html", "footer_html"],
  "login.html": ["site_name", "error_html", "footer_html"],
  "admin.html": [
    "site_name",
    "plugin_status_html",
    "stats_html",
    "csrf",
    "logout_url",
    "footer_html",
    "initial_json",
  ],
};

const TEMPLATE_FILES = {
  "apply.html": APPLY_HTML,
  "login.html": LOGIN_HTML,
  "admin.html": ADMIN_HTML,
};

check("静态检查: 三个模板与静态资源都存在", () => {
  for (const file of [APPLY_HTML, LOGIN_HTML, ADMIN_HTML, STYLE_CSS, FORM_JS, APP_JS]) {
    assert.ok(fs.existsSync(file), `缺少文件：${path.relative(ROOT, file)}`);
  }
});

check("静态检查: apply.html 关键点", () => {
  const html = readText(APPLY_HTML);
  for (const needle of [
    "./static/style.css",
    "./static/app.js",
    'type="module"',
    'name="viewport"',
    "$site_name",
    "$site_subtitle",
    "$group_line",
    "$rules_line",
    "$notice_html",
    "$footer_html",
    'role="alert"',
    "<label for=",
    'id="apply-form"',
    'id="qq"',
    'id="uid"',
  ]) {
    assert.ok(html.includes(needle), `apply.html 缺少：${needle}`);
  }
});

check("静态检查: login.html 关键点", () => {
  const html = readText(LOGIN_HTML);
  for (const needle of [
    "./static/style.css",
    "./static/app.js",
    'type="module"',
    'name="viewport"',
    "$site_name",
    "$error_html",
    "$footer_html",
    'type="password"',
    "<label for=",
    // 后端路由：登录页在 /admin，登录接口是 ./api/admin/login
    'action="./api/admin/login"',
    'name="password"',
    'href="./"',
  ]) {
    assert.ok(html.includes(needle), `login.html 缺少：${needle}`);
  }
  for (const bad of ['href="./apply"', 'href="./login"', 'action="./login"']) {
    assert.ok(!html.includes(bad), `login.html 不应包含无效路由：${bad}`);
  }
});

check("静态检查: admin.html 关键点（$csrf / $initial_json / 数据块）", () => {
  const html = readText(ADMIN_HTML);
  for (const needle of [
    "./static/style.css",
    "./static/app.js",
    'type="module"',
    'name="viewport"',
    "$site_name",
    "$plugin_status_html",
    "$stats_html",
    "$csrf",
    "$logout_url",
    "$footer_html",
    "$initial_json",
    'id="initial-data"',
    'type="application/json"',
    "<caption",
    'scope="col"',
    "aria-",
    'id="applications-table"',
    "./api/admin/export.csv",
    'download',
    // 后端只有 / 与 /admin 两个页面路由
    'href="./"',
    'value="pass"',
  ]) {
    assert.ok(html.includes(needle), `admin.html 缺少：${needle}`);
  }
  for (const bad of ['href="./apply"', "value=\"approve\"", 'action="./login"']) {
    assert.ok(!html.includes(bad), `admin.html 不应包含无效契约：${bad}`);
  }
});

check("静态检查: 模板只使用约定内的占位符（无未替换的其它 $xxx）", () => {
  for (const [name, file] of Object.entries(TEMPLATE_FILES)) {
    const html = readText(file);
    const pattern = /\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g;
    const found = new Set();
    let match;
    while ((match = pattern.exec(html)) !== null) found.add(match[1] || match[2]);
    const allowed = ALLOWED_PLACEHOLDERS[name];
    for (const key of found) {
      assert.ok(allowed.includes(key), `${name} 使用了未约定的占位符：$${key}`);
    }
    const residual = html.replace(pattern, "");
    assert.ok(!residual.includes("$"), `${name} 存在无法识别的 $ 字符`);
  }
});

check("静态检查: 模板不含外链 / 相对上级目录 / bridge-sdk", () => {
  const banned = ["bridge-sdk", "http://", "https://cdn", "../"];
  for (const [name, file] of Object.entries(TEMPLATE_FILES)) {
    const html = readText(file);
    for (const needle of banned) {
      assert.ok(!html.includes(needle), `${name} 不应包含：${needle}`);
    }
    // 只允许外链 ./static/app.js；application/json 数据块不算可执行内联脚本
    const tags = html.match(/<script\b[^>]*>/gi) || [];
    for (const tag of tags) {
      const hasSrc = /\bsrc=/i.test(tag);
      const isData = /type\s*=\s*["']application\/json["']/i.test(tag);
      assert.ok(hasSrc || isData, `${name} 存在非法的内联脚本标签：${tag}`);
      if (hasSrc) {
        assert.ok(/src\s*=\s*["']\.\/static\/app\.js["']/.test(tag), `${name} 脚本只能外链 ./static/app.js`);
      }
    }
  }
});

check("静态检查: 模板为 string.Template 兼容（无 $ 后紧跟非标识符）", () => {
  for (const [name, file] of Object.entries(TEMPLATE_FILES)) {
    const html = readText(file);
    const bad = html.match(/\$[^A-Za-z{]/);
    assert.equal(bad, null, `${name} 存在非法的 $ 用法：${bad && bad[0]}`);
  }
});

check("静态检查: style.css 使用 :root 变量与深色模式", () => {
  const css = readText(STYLE_CSS);
  assert.ok(css.includes(":root"), "缺少 :root 变量区");
  assert.ok(css.includes("--"), "缺少 CSS 变量");
  assert.ok(css.includes("@media (prefers-color-scheme: dark)"), "缺少深色模式覆盖");
  assert.ok(/--tap:\s*44px/.test(css), "触控目标基准应为 44px");
});

check("静态检查: form.js 为纯函数模块（无 DOM / 网络 / 存储）", () => {
  const js = readText(FORM_JS);
  for (const needle of ["document.", "window.", "fetch(", "localStorage", "XMLHttpRequest", "sessionStorage"]) {
    assert.ok(!js.includes(needle), `form.js 不应包含：${needle}`);
  }
});

check("静态检查: form.js 导出全部约定函数", () => {
  const required = [
    "extractUid",
    "validateForm",
    "statusLabel",
    "statusClass",
    "formatTime",
    "relativeTime",
    "isTerminal",
    "buildApplyPayload",
    "buildDecisionPayload",
    "summarize",
    "nextOffset",
    "prevOffset",
    "csvCell",
  ];
  for (const name of required) {
    assert.equal(typeof F[name], "function", `form.js 未导出函数：${name}`);
  }
});

check("静态检查: app.js 使用约定的接口路径与无 DOM 安全读取", () => {
  const js = readText(APP_JS);
  for (const needle of [
    "./api/apply",
    "./api/admin/applications",
    "./api/admin/decision",
    "./api/admin/settings",
    "./api/admin/delete",
    "./api/admin/login",
    "JSON.parse",
    "parse",
    "aria",
    "addEventListener",
    "counts",
    "blocked_list",
  ]) {
    assert.ok(js.includes(needle), `app.js 缺少：${needle}`);
  }
});

check("静态检查: app.js 读取后端真实字段（items/total/counts/settings/plugin）", () => {
  const js = readText(APP_JS);
  for (const needle of ["initial.counts", "initial.items", "initial.settings", "body.counts", "body.plugin", "body.stats"]) {
    assert.ok(js.includes(needle), `app.js 缺少字段读取：${needle}`);
  }
  // 已通过但没有验证码时不应渲染空白验证码
  assert.ok(js.includes("plugin_connected"), "app.js 应处理 plugin_connected");
  assert.ok(js.includes("还没有拿到当日验证码") , "app.js 应提示插件未同步验证码");
});

check("静态检查: app.js 只 import ./form.js", () => {
  const js = readText(APP_JS);
  const specifiers = [...js.matchAll(/from\s+["']([^"']+)["']/g)].map((m) => m[1]);
  assert.ok(specifiers.length >= 1, "app.js 应至少 import ./form.js");
  for (const spec of specifiers) {
    assert.equal(spec, "./form.js", `app.js 出现了非 ./form.js 的 import：${spec}`);
  }
});

check("静态检查: app.js 覆盖申请页交互要点（复制兜底 / 轮询 / 超时提示）", () => {
  const js = readText(APP_JS);
  for (const needle of [
    "navigator.clipboard",
    "execCommand",
    "setInterval",
    "const POLL_INTERVAL_MS = 2000",
    "const POLL_TIMEOUT_MS = 60000",
    "仍在核验，可刷新页面查看",
    "核验中…",
    "重新填写",
  ]) {
    assert.ok(js.includes(needle), `app.js 缺少：${needle}`);
  }
});

check("静态检查: 管理后台包含写操作的二次确认与提示", () => {
  const js = readText(APP_JS);
  for (const needle of ["window.confirm", "删除记录不等于拉黑", "登录已过期", "toast"]) {
    assert.ok(js.includes(needle), `app.js 缺少：${needle}`);
  }
});

/* ------------------------------------------------------------------ *
 * 11. 渲染模拟（近似 Python string.Template）
 * ------------------------------------------------------------------ */

/** 用给定变量近似 string.Template 的 $name / ${name} 替换。 */
function renderTemplate(html, values) {
  return html.replace(
    /\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)/g,
    (match, braced, plain) => {
      const key = braced || plain;
      if (!Object.prototype.hasOwnProperty.call(values, key)) {
        throw new Error(`模板需要未提供的变量：$${key}`);
      }
      return values[key];
    },
  );
}

const SAMPLE_VALUES = {
  "apply.html": {
    site_name: "临时审核群",
    site_subtitle: "B站入群核验",
    group_line: "QQ 群 123456789",
    rules_line: "等级 ≥ 2，粉丝 ≥ 0",
    notice_html: '<div class="alert alert-warn">测试公告</div>',
    footer_html: "<p>页脚</p>",
  },
  "login.html": {
    site_name: "临时审核群",
    error_html: '<div class="alert alert-error">密码错误</div>',
    footer_html: "<p>页脚</p>",
  },
  "admin.html": {
    site_name: "临时审核群",
    plugin_status_html: "<p>已连接</p>",
    stats_html: "<p>总数 10</p>",
    csrf: "csrf-token-123",
    logout_url: "./api/admin/logout",
    footer_html: "<p>页脚</p>",
    initial_json: JSON.stringify({
      items: [
        {
          id: 12,
          ticket: "t-1",
          qq: "12345",
          uid: "12345678",
          uid_name: "小明",
          level: 4,
          fans: 520,
          status: "approved",
          reason: "",
          note: "",
          code: "ABC123",
          source: "auto",
          ip: "1.2.3.4",
          delivered: 0,
          created_at: 1699999999,
          decided_at: 1700000000,
        },
      ],
      total: 1,
      counts: { total: 10, pending: 2, approved: 6, rejected: 1, manual: 0, blocked: 0, blocked_list: 1 },
      csrf: "csrf-token-123",
      settings: { site_name: "临时审核群", bili_name_keywords: ["猫"], bili_unique: true, plugin_token: "" },
    }),
  },
};

check("渲染模拟: 三个模板都能被完整替换（渲染后不残留 $ 占位符）", () => {
  for (const [name, file] of Object.entries(TEMPLATE_FILES)) {
    const rendered = renderTemplate(readText(file), SAMPLE_VALUES[name]);
    assert.ok(!rendered.includes("$"), `${name} 渲染后仍残留 $ 字符`);
    assert.ok(rendered.includes("临时审核群"), `${name} 渲染后缺少站点名`);
    assert.ok(rendered.startsWith("<!DOCTYPE html>"), `${name} 渲染后缺少 DOCTYPE`);
    assert.ok(rendered.trimEnd().endsWith("</html>"), `${name} 渲染后结构不完整`);
  }
});

check("渲染模拟: HTML 片段原样插入（不转义、不加引号）", () => {
  const apply = renderTemplate(readText(APPLY_HTML), SAMPLE_VALUES["apply.html"]);
  assert.ok(apply.includes('<div class="alert alert-warn">测试公告</div>'));
  const login = renderTemplate(readText(LOGIN_HTML), SAMPLE_VALUES["login.html"]);
  assert.ok(login.includes('<div class="alert alert-error">密码错误</div>'));
  const admin = renderTemplate(readText(ADMIN_HTML), SAMPLE_VALUES["admin.html"]);
  assert.ok(admin.includes("<p>已连接</p>"));
  assert.ok(admin.includes('content="csrf-token-123"'));
  assert.ok(admin.includes('href="./api/admin/logout"'));
});

check("渲染模拟: admin 首屏数据块可直接 JSON.parse", () => {
  const admin = renderTemplate(readText(ADMIN_HTML), SAMPLE_VALUES["admin.html"]);
  const match = admin.match(
    /<script type="application\/json" id="initial-data">([\s\S]*?)<\/script>/,
  );
  assert.ok(match, "admin.html 未找到 initial-data 数据块");
  const parsed = JSON.parse(match[1]);
  assert.equal(parsed.items.length, 1);
  assert.equal(parsed.items[0].uid_name, "小明");
  assert.equal(parsed.total, 1);
  assert.equal(parsed.counts.approved, 6);
  assert.equal(parsed.settings.bili_unique, true);
  assert.equal(parsed.csrf, "csrf-token-123");
  assert.equal(parsed.settings.plugin_token, "");
});

check("契约检查: 模板占位符与 render.py 提供的变量一一对应", () => {
  const source = readText(path.join(SITE_DIR, "render.py"));
  const pairs = [
    ["apply.html", "render_apply"],
    ["login.html", "render_login"],
    ["admin.html", "render_admin"],
  ];
  for (const [template, fn] of pairs) {
    const start = source.indexOf(`def ${fn}(`);
    assert.ok(start >= 0, `render.py 缺少函数：${fn}`);
    const rest = source.slice(start + 1);
    const nextDef = rest.indexOf("\ndef ");
    const body = nextDef >= 0 ? rest.slice(0, nextDef) : rest;
    for (const key of ALLOWED_PLACEHOLDERS[template]) {
      assert.ok(
        body.includes(`"${key}"`),
        `render.py 的 ${fn} 没有提供 $${key}（${template} 需要）`,
      );
    }
  }
});

/* ------------------------------------------------------------------ *
 * 12. app.js 语法冒烟
 * ------------------------------------------------------------------ */

check("语法检查: app.js 去掉 import 后可作为函数体编译（无语法错误）", () => {
  const js = readText(APP_JS);
  const body = js.replace(/^\s*import\s+[\s\S]*?from\s+["']\.\/form\.js["'];?/m, "");
  assert.ok(body !== js, "app.js 未找到 ./form.js 的 import 语句");
  // eslint-disable-next-line no-new-func
  new Function(body);
});

check("语法检查: app.js 使用 ESM（顶层 import/export 之外不依赖 require）", () => {
  const js = readText(APP_JS);
  const code = js.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.ok(/^\s*import\s/m.test(js), "app.js 应为 ESM（缺少 import）");
  assert.ok(!code.includes("require("), "app.js 不应使用 require");
  assert.ok(!code.includes(".innerHTML"), "app.js 不应用 innerHTML 写入数据");
});

/* ------------------------------------------------------------------ *
 * 汇总
 * ------------------------------------------------------------------ */

const total = passed + failed;
console.log("");
console.log(`${passed}/${total} 通过`);
if (failed > 0) {
  process.exitCode = 1;
}
