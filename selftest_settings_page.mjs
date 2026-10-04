/**
 * selftest_settings_page.mjs
 *
 * 插件 Pages「settings」纯函数与静态资源的自检脚本。
 * 只使用 Node 标准库，运行方式：
 *   "C:\Users\haoxu\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe" selftest_settings_page.mjs
 *
 * 说明：Node 会把没有 package.json 的 .js 当 CJS，因此先把 pages/settings/settings.js
 * 复制到系统临时目录下的 settings-under-test.mjs，再动态 import，用完删除。
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
// 默认测工作区里的插件目录；设了 TEMP_REVIEW_PLUGIN_DIR 就测那个目录（用于校验打包产物）
const PLUGIN_DIR = process.env.TEMP_REVIEW_PLUGIN_DIR
  ? path.resolve(process.env.TEMP_REVIEW_PLUGIN_DIR)
  : path.join(ROOT, "astrbot_plugin_temp_review_group");
const PAGE_DIR = path.join(PLUGIN_DIR, "pages", "settings");
const SCHEMA_PATH = path.join(PLUGIN_DIR, "_conf_schema.json");
const SETTINGS_SRC = path.join(PAGE_DIR, "settings.js");

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

const deepCopy = (value) => JSON.parse(JSON.stringify(value));

/* ------------------------------------------------------------------ *
 * 载入被测模块
 * ------------------------------------------------------------------ */

const schema = JSON.parse(fs.readFileSync(SCHEMA_PATH, "utf8"));
const schemaKeys = Object.keys(schema);
info(`_conf_schema.json 共 ${schemaKeys.length} 个配置项：${schemaKeys.join(", ")}`);

const tmpFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "dsh-settings-selftest-")), "settings-under-test.mjs");
fs.copyFileSync(SETTINGS_SRC, tmpFile);

let S;
try {
  S = await import(pathToFileURL(tmpFile).href);
} finally {
  try {
    fs.rmSync(path.dirname(tmpFile), { recursive: true, force: true });
  } catch {
    /* 临时文件清理失败不影响测试结论 */
  }
}

/* ------------------------------------------------------------------ *
 * 1. 分组覆盖：不漏不重
 * ------------------------------------------------------------------ */

check("1. groupFields 字段集合与 schema key 集合完全一致（不漏不重）", () => {
  const groups = S.groupFields(schema);
  assert.ok(Array.isArray(groups) && groups.length > 0, "groupFields 应返回非空数组");
  const collected = [];
  for (const group of groups) {
    assert.ok(typeof group.id === "string" && group.id, "分组缺少 id");
    assert.ok(typeof group.title === "string" && group.title, "分组缺少 title");
    assert.ok(Array.isArray(group.fields), "分组缺少 fields 数组");
    for (const field of group.fields) collected.push(field.key);
  }
  assert.equal(
    new Set(collected).size,
    collected.length,
    `存在重复字段：${collected.join(", ")}`,
  );
  assert.deepStrictEqual(
    [...collected].sort(),
    [...schemaKeys].sort(),
    `分组字段与 schema 不一致：缺少 ${schemaKeys.filter((k) => !collected.includes(k)).join(", ") || "无"}；` +
      `多余 ${collected.filter((k) => !schemaKeys.includes(k)).join(", ") || "无"}`,
  );
  // 分组内部保持 schema 顺序（跨分组的全局顺序会因“其他”兜底分组而不同）
  const order = new Map(schemaKeys.map((key, index) => [key, index]));
  for (const group of groups) {
    const indexes = group.fields.map((field) => order.get(field.key));
    for (let i = 1; i < indexes.length; i += 1) {
      assert.ok(
        indexes[i] > indexes[i - 1],
        `分组 ${group.id} 内字段未按 schema 顺序：${group.fields.map((f) => f.key).join(", ")}`,
      );
    }
  }
});

/* ------------------------------------------------------------------ *
 * 2. 每个 key 都能被 validateValue 处理
 * ------------------------------------------------------------------ */

function legalValue(node) {
  switch (node.type) {
    case "bool":
      return true;
    case "int":
    case "float":
      return node.default !== undefined ? node.default : node.slider ? node.slider.min : 0;
    case "string":
      return Array.isArray(node.options) && node.options.length ? node.options[0] : node.default ?? "";
    case "text":
      return node.default ?? "示例文本";
    case "list":
      return Array.isArray(node.default) && node.default.length ? deepCopy(node.default) : ["123456"];
    case "template_list":
      return deepCopy(node.default ?? []);
    default:
      return node.default ?? "";
  }
}

check("2. schema 中每个 key 都能被 validateValue 处理且不抛异常", () => {
  const seenTypes = new Set();
  for (const key of schemaKeys) {
    const node = schema[key];
    const value = legalValue(node);
    let result;
    assert.doesNotThrow(() => {
      result = S.validateValue(node, value);
    }, `key=${key} 调用 validateValue 抛异常`);
    assert.equal(result, null, `key=${key}（type=${node.type}）的合法值被判为非法：${result}`);
    seenTypes.add(node.type);
  }
  for (const type of ["bool", "int", "float", "string", "text", "list", "template_list"]) {
    assert.ok(seenTypes.has(type), `schema 未覆盖类型 ${type}，测试样本不完整`);
  }
});

/* ------------------------------------------------------------------ *
 * 3. defaultsFromSchema 深相等
 * ------------------------------------------------------------------ */

check("3. defaultsFromSchema 每个值与 schema.default 深相等", () => {
  const defaults = S.defaultsFromSchema(schema);
  assert.deepStrictEqual(Object.keys(defaults).sort(), [...schemaKeys].sort(), "默认值 key 集合不一致");
  let compared = 0;
  for (const key of schemaKeys) {
    assert.ok(
      Object.prototype.hasOwnProperty.call(schema[key], "default"),
      `key=${key} 在 schema 中没有 default`,
    );
    assert.deepStrictEqual(defaults[key], schema[key].default, `key=${key} 的默认值不相等`);
    if (defaults[key] && typeof defaults[key] === "object") {
      assert.notEqual(defaults[key], schema[key].default, `key=${key} 的默认值应为深拷贝`);
    }
    compared += 1;
  }
  assert.equal(compared, schemaKeys.length, "未逐个比较全部默认值");
});

/* ------------------------------------------------------------------ *
 * 4. 拒绝非法值
 * ------------------------------------------------------------------ */

check("4. validateValue 拒绝非法 match_mode / 超范围 max_attempts / 非法 kick_interval_seconds", () => {
  const badMode = S.validateValue(schema.match_mode, "levenshtein");
  assert.ok(typeof badMode === "string" && badMode.length > 0, "非法 match_mode 未被拒绝");

  const badFuzzy = S.validateValue(schema.fuzzy_threshold, 1.5);
  assert.ok(typeof badFuzzy === "string" && badFuzzy.length > 0, "越界 fuzzy_threshold 未被拒绝");
  assert.equal(S.validateValue(schema.fuzzy_threshold, 0.8), null, "合法 fuzzy_threshold 被拒绝");

  const badAttempts = S.validateValue(schema.max_attempts, 100);
  assert.ok(typeof badAttempts === "string" && badAttempts.length > 0, "max_attempts=100 未被拒绝");

  const badIntervalHigh = S.validateValue(schema.kick_interval_seconds, 99);
  assert.ok(
    typeof badIntervalHigh === "string" && badIntervalHigh.length > 0,
    "kick_interval_seconds=99 未被拒绝",
  );
  const badIntervalLow = S.validateValue(schema.kick_interval_seconds, -3);
  assert.ok(
    typeof badIntervalLow === "string" && badIntervalLow.length > 0,
    "kick_interval_seconds=-3 未被拒绝",
  );
  const badLength = S.validateValue(schema.code_length, 2);
  assert.ok(typeof badLength === "string" && badLength.length > 0, "code_length=2 未被拒绝");

  // 合法值仍然通过
  assert.equal(S.validateValue(schema.match_mode, "exact"), null, "合法 match_mode 被拒绝");
  assert.equal(S.validateValue(schema.max_attempts, 10), null, "边界值 max_attempts=10 被拒绝");
  assert.equal(S.validateValue(schema.kick_interval_seconds, 0.5), null, "合法 kick_interval_seconds 被拒绝");
});

/* ------------------------------------------------------------------ *
 * 5. diffValues
 * ------------------------------------------------------------------ */

check("5. diffValues 只返回变化的 key（含 secret 字段语义）", () => {
  const values = {};
  for (const key of schemaKeys) values[key] = deepCopy(schema[key].default);

  const draft = S.toDraft(schema, values, []);
  assert.deepStrictEqual(S.diffValues(schema, draft, values, []), {}, "未变化时应返回空对象");

  draft.match_mode = "exact";
  const single = S.diffValues(schema, draft, values, []);
  assert.deepStrictEqual(Object.keys(single), ["match_mode"], "应只返回 match_mode");

  draft.max_attempts = 5;
  const multi = S.diffValues(schema, draft, values, []);
  assert.deepStrictEqual(
    Object.keys(multi).sort(),
    ["match_mode", "max_attempts"],
    "应只返回两个变化的 key",
  );
  assert.equal(multi.max_attempts, 5, "变化值应被带出");

  // 校验失败的变化不应进入 payload
  draft.max_attempts = 100;
  const invalid = S.diffValues(schema, draft, values, []);
  assert.ok(!("max_attempts" in invalid), "校验失败的变化不应进入 payload");

  // secret 字段
  const secretSchema = {
    api_token: { type: "string", description: "令牌", secret: true, default: "" },
    plain: { type: "string", description: "普通", default: "" },
  };
  const secretValues = { api_token: "", plain: "" };
  const secretDraft = S.toDraft(secretSchema, secretValues, ["api_token"]);
  assert.equal(secretDraft.api_token, "", "secret 草稿应为空字符串");
  assert.deepStrictEqual(
    S.diffValues(secretSchema, secretDraft, secretValues, ["api_token"]),
    {},
    "secret 字段为空时不应算变化",
  );

  secretDraft.api_token = "  ";
  assert.deepStrictEqual(
    S.diffValues(secretSchema, secretDraft, secretValues, ["api_token"]),
    {},
    "secret 字段为空白时不应算变化",
  );

  secretDraft.api_token = "s3cr3t";
  const secretDiff = S.diffValues(secretSchema, secretDraft, secretValues, ["api_token"]);
  assert.deepStrictEqual(Object.keys(secretDiff), ["api_token"], "secret 字段填了内容应算变化");
  assert.equal(secretDiff.api_token, "s3cr3t", "secret 值应被带出");

  // changedKeys 只看原始变化，不受校验影响
  assert.deepStrictEqual(
    S.changedKeys(schema, draft, values, []).sort(),
    ["match_mode", "max_attempts"],
    "changedKeys 应返回原始变化",
  );
});

/* ------------------------------------------------------------------ *
 * 6. parseListInput
 * ------------------------------------------------------------------ */

check("6. parseListInput 去重、去空并保持顺序", () => {
  assert.deepStrictEqual(
    S.parseListInput("123\n456, 123，789;"),
    ["123", "456", "789"],
    "多分隔符解析结果不正确",
  );
  assert.deepStrictEqual(S.parseListInput("  a ,, a ；b、c\r\nd "), ["a", "b", "c", "d"], "空白与重复处理不正确");
  assert.deepStrictEqual(S.parseListInput(""), [], "空字符串应返回空数组");
  assert.deepStrictEqual(S.parseListInput(null), [], "null 应返回空数组");
});

/* ------------------------------------------------------------------ *
 * 7. newTemplateItem
 * ------------------------------------------------------------------ */

check("7. newTemplateItem 生成带 __template_key 与 answers: [] 的条目", () => {
  const item = S.newTemplateItem(schema.questions, "question_item");
  assert.equal(item.__template_key, "question_item", "缺少 __template_key");
  assert.deepStrictEqual(item.answers, [], "answers 应为空数组");
  assert.equal(item.question, "", "question 应回落到默认空字符串");
  assert.notEqual(item.answers, schema.questions.templates.question_item.items.answers.default, "应为深拷贝");
  assert.ok(
    /第 1 条「问题内容」/.test(String(S.validateValue(schema.questions, [item]))),
    `空问题应被拒绝，实际：${S.validateValue(schema.questions, [item])}`,
  );
  const filled = S.newTemplateItem(schema.questions, "question_item");
  filled.question = "1 + 1 = ?";
  filled.answers = ["2"];
  assert.equal(S.validateValue(schema.questions, [filled]), null, "填写完整的条目应通过校验");
});

/* ------------------------------------------------------------------ *
 * 8. toDraft 对 secret 字段
 * ------------------------------------------------------------------ */

check("8. toDraft 对 secret 字段返回空字符串", () => {
  const synthetic = {
    token: { type: "string", description: "令牌", secret: true, default: "default-token" },
    note: { type: "text", description: "备注", default: "hi" },
  };
  const draft1 = S.toDraft(synthetic, { token: "real-token", note: "hey" }, ["token"]);
  assert.equal(draft1.token, "", "schema.secret 字段应置空");
  assert.equal(draft1.note, "hey", "非 secret 字段应保留原值");

  const draft2 = S.toDraft(synthetic, { token: "real-token", note: "hey" }, ["note"]);
  assert.equal(draft2.note, "", "secret_fields 中的字段应置空");

  const values = {};
  for (const key of schemaKeys) values[key] = deepCopy(schema[key].default);
  values.match_mode = "regex";
  const draft3 = S.toDraft(schema, values, ["match_mode"]);
  assert.equal(draft3.match_mode, "", "真实 schema 中列入 secret_fields 的 key 也应置空");
  assert.equal(draft3.code_length, schema.code_length.default, "其他字段应正常回落/保留");
  assert.deepStrictEqual(
    S.toDraft(schema, values, []).questions,
    values.questions,
    "template_list 应深拷贝",
  );
  assert.notEqual(S.toDraft(schema, values, []).questions, values.questions, "template_list 不应共享引用");
});

/* ------------------------------------------------------------------ *
 * 9. 静态资源检查
 * ------------------------------------------------------------------ */

check("9. 页面静态资源与 bridge 调用检查", () => {
  const htmlPath = path.join(PAGE_DIR, "index.html");
  const appPath = path.join(PAGE_DIR, "app.js");
  const cssPath = path.join(PAGE_DIR, "style.css");

  for (const file of [htmlPath, appPath, cssPath, SETTINGS_SRC]) {
    assert.ok(fs.existsSync(file), `缺少文件：${file}`);
  }

  const html = fs.readFileSync(htmlPath, "utf8");
  assert.ok(html.includes("./app.js"), "index.html 应通过相对路径引用 ./app.js");
  assert.ok(html.includes("./style.css"), "index.html 应通过相对路径引用 ./style.css");
  assert.ok(html.includes('type="module"'), "index.html 应以 type=module 加载外部脚本");
  assert.ok(
    !html.includes("bridge-sdk"),
    "index.html 不应手动引入 bridge-sdk（AstrBot 会自动注入）",
  );
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>/i.test(html), "index.html 不应包含内联脚本");
  assert.ok(!html.includes("asset_token"), "index.html 不应包含 asset_token");
  assert.ok(!/href="\.\.\//.test(html) && !/src="\.\.\//.test(html), "index.html 不应使用 .. 相对路径");

  const app = fs.readFileSync(appPath, "utf8");
  assert.ok(app.includes("AstrBotPluginPage"), "app.js 应使用 window.AstrBotPluginPage");
  assert.ok(app.includes('apiGet("settings")'), 'app.js 应调用 apiGet("settings")');
  assert.ok(app.includes('apiPost("settings"'), 'app.js 应调用 apiPost("settings"');
  assert.ok(app.includes("bridge.ready("), "app.js 应等待 bridge.ready()");
  assert.ok(app.includes("function unwrap("), "app.js 应包含信封格式归一化 unwrap()");
  assert.ok(
    /unwrap\(\s*await\s+bridge\.apiGet/.test(app),
    "app.js 应对 apiGet 结果调用 unwrap()",
  );
  assert.ok(
    /unwrap\(\s*await\s+bridge\.apiPost/.test(app),
    "app.js 应对 apiPost 结果调用 unwrap()",
  );
  assert.ok(app.includes("响应格式异常"), "app.js 应对 schema 缺失给出“响应格式异常”提示");
  assert.ok(app.includes("warnings"), "app.js 应处理后端返回的 warnings");
  assert.ok(app.includes('showMessage("warning"'), "app.js 应用警告样式展示 warnings");

  const css = fs.readFileSync(cssPath, "utf8");
  assert.ok(css.includes("data-theme"), 'style.css 应包含 [data-theme="dark"] 覆盖');

  const settingsSrc = fs.readFileSync(SETTINGS_SRC, "utf8");
  assert.ok(!/\bdocument\./.test(settingsSrc), "settings.js 不应使用 document");
  assert.ok(!/\bwindow\./.test(settingsSrc), "settings.js 不应使用 window");
  assert.ok(!/\bfetch\s*\(/.test(settingsSrc), "settings.js 不应使用 fetch");
  assert.ok(!/localStorage|sessionStorage/.test(settingsSrc), "settings.js 不应使用存储 API");
});

/* ------------------------------------------------------------------ *
 * 10. statusChips
 * ------------------------------------------------------------------ */

check("10. statusChips 返回非空数组且包含验证码值", () => {
  const sample = {
    code: "ABC123",
    code_created_at: "09-18 00:00",
    code_expire_at: "2026-09-19 00:00",
    code_expire_text: "09-19 00:00",
    pending: 2,
    approved: 1,
    review_groups: ["123456"],
    cleanup_time: "03:30",
    code_reset_time: "00:00",
    timezone: "",
    server_time: "2026-09-18 12:00:00",
    data_file: "C:\\state.json",
    has_questions: true,
    platforms: ["qq-main"],
  };
  const chips = S.statusChips(sample);
  assert.ok(Array.isArray(chips) && chips.length > 0, "statusChips 应返回非空数组");
  for (const chip of chips) {
    assert.equal(typeof chip.label, "string", "chip.label 应为字符串");
    assert.ok(chip.value !== undefined && chip.value !== null, "chip.value 不应为空");
  }
  assert.ok(
    chips.some((chip) => String(chip.value).includes("ABC123")),
    "chips 应包含验证码值 ABC123",
  );
  assert.ok(
    chips.some(
      (chip) =>
        String(chip.value).includes("123456") ||
        String(chip.hint ?? "").includes("123456"),
    ),
    "chips 应包含审核群号",
  );
  const empty = S.statusChips(null);
  assert.ok(Array.isArray(empty) && empty.length > 0, "status 缺省时也应返回非空 chips");
});

/* ------------------------------------------------------------------ *
 * 11. 空列表 / 空题库：必须可保存（与 schema 默认值和后端行为一致）
 * ------------------------------------------------------------------ */

check("11. 空列表与空题库被视为合法配置，并会进入保存 payload", () => {
  // schema 的默认值就是 []，清空这些项必须能存下去，否则后端永远收不到“清空”操作
  for (const key of ["review_groups", "admin_ids", "exempt_user_ids"]) {
    assert.equal(S.validateValue(schema[key], []), null, `${key} 为空时应视为合法`);
  }
  assert.equal(S.validateValue(schema.questions, []), null, "清空题库应视为合法（后端会给 warning）");

  const values = {};
  for (const key of schemaKeys) values[key] = deepCopy(schema[key].default);
  values.review_groups = ["123456"];
  values.admin_ids = ["555"];
  values.questions = [{ __template_key: "question_item", question: "1+1=?", answers: ["2"] }];

  const draft = S.toDraft(schema, values, []);
  draft.review_groups = [];
  draft.admin_ids = [];
  draft.questions = [];

  const payload = S.diffValues(schema, draft, values, []);
  assert.deepStrictEqual(
    Object.keys(payload).sort(),
    ["admin_ids", "questions", "review_groups"],
    "清空操作必须进入 payload",
  );
  assert.deepStrictEqual(payload.admin_ids, [], "空列表应原样提交");

  // 但“问题内容留空”仍然要拦下来（空题目没有意义）
  const blank = S.validateValue(schema.questions, [
    { __template_key: "question_item", question: "   ", answers: [] },
  ]);
  assert.ok(typeof blank === "string" && blank.length > 0, "空问题内容应被拒绝");

  // 列表项为空字符串仍要拦下来
  const blankItem = S.validateValue(schema.admin_ids, ["555", "  "]);
  assert.ok(typeof blankItem === "string" && blankItem.length > 0, "空列表项应被拒绝");
});

/* ------------------------------------------------------------------ *
 * 12. 题库条目：必填字段与可选字段的边界
 * ------------------------------------------------------------------ */

check("12. 题库条目允许可选字段留空、但不允许空题干", () => {
  const item = S.newTemplateItem(schema.questions, "question_item");
  item.question = "题目";
  item.answers = ["答案"];
  assert.equal(item.enabled, true, "新条目默认启用");
  assert.equal(item.hint, "", "新条目 hint 默认为空");
  assert.equal(item.match_mode, "inherit", "新条目匹配方式默认 inherit");
  assert.equal(S.validateValue(schema.questions, [item]), null, "空 hint 的条目应通过校验");

  const blankQuestion = Object.assign({}, item, { question: "   " });
  assert.ok(
    typeof S.validateValue(schema.questions, [blankQuestion]) === "string",
    "空题干仍应被拒绝",
  );

  const disabledNoAnswers = Object.assign({}, item, { enabled: false, answers: [] });
  assert.equal(
    S.validateValue(schema.questions, [disabledNoAnswers]),
    null,
    "停用的题目允许没有答案",
  );

  const badSubMode = Object.assign({}, item, { match_mode: "levenshtein" });
  assert.ok(
    typeof S.validateValue(schema.questions, [badSubMode]) === "string",
    "条目内非法的匹配方式应被拒绝",
  );
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
