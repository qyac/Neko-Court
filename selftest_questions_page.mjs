/**
 * selftest_questions_page.mjs
 *
 * 插件 Pages「questions」（题库管理）纯函数与静态资源的自检脚本。
 * 只使用 Node 标准库，运行方式：
 *   "C:\Users\haoxu\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe" selftest_questions_page.mjs
 *
 * 说明：Node 会把没有 package.json 的 .js 当 CJS，因此先把 pages/questions/bank.js
 * 复制到系统临时目录下的 bank-under-test.mjs，再动态 import，用完删除。
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
const PAGE_DIR = path.join(PLUGIN_DIR, "pages", "questions");
const BANK_SRC = path.join(PAGE_DIR, "bank.js");
const HTML_PATH = path.join(PAGE_DIR, "index.html");
const APP_PATH = path.join(PAGE_DIR, "app.js");
const CSS_PATH = path.join(PAGE_DIR, "style.css");
const I18N_ZH = path.join(PLUGIN_DIR, ".astrbot-plugin", "i18n", "zh-CN.json");
const I18N_EN = path.join(PLUGIN_DIR, ".astrbot-plugin", "i18n", "en-US.json");

const MATCH_MODE_OPTIONS = ["inherit", "contains", "exact", "regex", "fuzzy"];

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

/* ------------------------------------------------------------------ *
 * 载入被测模块
 * ------------------------------------------------------------------ */

const tmpFile = path.join(
  fs.mkdtempSync(path.join(os.tmpdir(), "dsh-questions-selftest-")),
  "bank-under-test.mjs",
);
fs.copyFileSync(BANK_SRC, tmpFile);

let B;
try {
  B = await import(pathToFileURL(tmpFile).href);
} finally {
  try {
    fs.rmSync(path.dirname(tmpFile), { recursive: true, force: true });
  } catch {
    /* 临时文件清理失败不影响测试结论 */
  }
}

info(`被测模块：${path.relative(ROOT, BANK_SRC)}`);

/** 造一份契约形状的题库数据。 */
function makeBank() {
  return {
    questions: [
      {
        __template_key: "question_item",
        enabled: true,
        question: "甲题",
        hint: "提示甲",
        answers: ["甲", "第一"],
        match_mode: "inherit",
        picks: 3,
        passes: 1,
        usable: true,
      },
      {
        __template_key: "question_item",
        enabled: false,
        question: "乙题",
        hint: "",
        answers: [],
        match_mode: "exact",
        picks: 0,
        passes: 0,
        usable: false,
      },
    ],
    common_answers: ["邀请码1234"],
    match_mode: "contains",
    fuzzy_threshold: 0.8,
    match_mode_options: MATCH_MODE_OPTIONS.slice(),
    review_mode: "rule",
    llm_available: true,
    llm_provider: "fake-chat",
    parse_limit: 30,
    parse_max_chars: 8000,
    meta: { name: "astrbot_plugin_temp_review_group", display_name: "临时审核群管理", version: "v1.1.0" },
  };
}

/* ------------------------------------------------------------------ *
 * 1. 脏数据兜底
 * ------------------------------------------------------------------ */

check("1. normalizeQuestion / normalizeBank 对脏数据兜底且不抛异常", () => {
  const required = [
    "MATCH_MODE_LABELS",
    "normalizeQuestion",
    "normalizeBank",
    "newQuestion",
    "validateQuestion",
    "validateBank",
    "diffBank",
    "parseListInput",
    "draftToQuestion",
    "statsText",
    "matchModeLabel",
    "usableCount",
    "charCount",
  ];
  for (const name of required) {
    assert.ok(name in B, `bank.js 缺少导出：${name}`);
  }
  for (const name of required.filter((item) => item !== "MATCH_MODE_LABELS")) {
    assert.equal(typeof B[name], "function", `${name} 应为函数`);
  }
  assert.equal(B.MATCH_MODE_LABELS.inherit, "跟随全局");
  assert.equal(B.MATCH_MODE_LABELS.contains, "包含关键词");
  assert.equal(B.MATCH_MODE_LABELS.exact, "完全相等");
  assert.equal(B.MATCH_MODE_LABELS.regex, "正则");
  assert.equal(B.MATCH_MODE_LABELS.fuzzy, "模糊相似");

  // 缺字段 / 非对象
  assert.doesNotThrow(() => B.normalizeQuestion(null));
  assert.doesNotThrow(() => B.normalizeQuestion("脏数据"));
  assert.doesNotThrow(() => B.normalizeQuestion([]));
  const blank = B.normalizeQuestion(undefined);
  assert.equal(blank.__template_key, "question_item");
  assert.equal(blank.enabled, true);
  assert.equal(blank.question, "");
  assert.equal(blank.hint, "");
  assert.deepStrictEqual(blank.answers, []);
  assert.equal(blank.match_mode, "inherit");
  assert.equal(blank.picks, 0);
  assert.equal(blank.passes, 0);
  assert.equal(blank.usable, false);
  assert.deepStrictEqual(Object.keys(blank).sort(), Object.keys(B.newQuestion()).sort());

  // answers 是字符串 / 数组里混脏值
  const fromString = B.normalizeQuestion({
    answers: "A、B; A",
    question: "1 + 1 = ?",
    match_mode: "乱写",
    enabled: "false",
  });
  assert.deepStrictEqual(fromString.answers, ["A", "B"]);
  assert.equal(fromString.match_mode, "inherit", "非法 match_mode 应回退 inherit");
  assert.equal(fromString.enabled, false, "字符串 false 应被识别");
  const messy = B.normalizeQuestion({
    answers: [1, null, "x", "", "x", {}],
    question: 123,
    enabled: 0,
    usable: true,
    picks: "3",
    passes: -1,
  });
  assert.deepStrictEqual(messy.answers, ["1", "x"]);
  assert.equal(messy.question, "123");
  assert.equal(messy.enabled, false);
  assert.equal(messy.usable, true);
  assert.equal(messy.picks, 3);
  assert.equal(messy.passes, -1);

  // normalizeBank：非对象、questions 不是数组、字段类型全错
  assert.doesNotThrow(() => B.normalizeBank("脏数据"));
  const empty = B.normalizeBank(undefined);
  assert.deepStrictEqual(empty.questions, []);
  assert.deepStrictEqual(empty.commonAnswers, []);
  assert.equal(empty.matchMode, "contains");
  assert.equal(empty.fuzzyThreshold, 0.8);
  assert.equal(empty.reviewMode, "rule");
  assert.equal(empty.llmAvailable, false);
  assert.equal(empty.llmProvider, "");
  assert.equal(empty.parseLimit, 30);
  assert.equal(empty.parseMaxChars, 8000);
  assert.deepStrictEqual(empty.meta, {});
  assert.ok(empty.matchModeOptions.includes("inherit"), "matchModeOptions 应有兜底值");

  const dirty = B.normalizeBank({
    questions: "不是数组",
    common_answers: "c1，c2，c1",
    match_mode: "乱写",
    fuzzy_threshold: "abc",
    match_mode_options: [],
    parse_limit: "abc",
    parse_max_chars: "abc",
  });
  assert.deepStrictEqual(dirty.questions, []);
  assert.deepStrictEqual(dirty.commonAnswers, ["c1", "c2"]);
  assert.equal(dirty.matchMode, "contains");
  assert.equal(dirty.fuzzyThreshold, 0.8);
  assert.equal(dirty.parseLimit, 30);
  assert.equal(dirty.parseMaxChars, 8000);
  assert.ok(dirty.matchModeOptions.length >= 5);

  // 后端给了自定义匹配方式时保留它（不要把配置里的值改掉）
  const custom = B.normalizeBank({
    match_mode_options: ["inherit", "custom"],
    questions: [{ question: "q", match_mode: "custom" }],
  });
  assert.equal(custom.questions[0].match_mode, "custom");

  const one = B.normalizeBank(makeBank());
  assert.equal(one.questions.length, 2);
  assert.equal(one.questions[0].usable, true);
  assert.equal(one.questions[1].enabled, false);
  assert.equal(one.llmAvailable, true);
  assert.equal(one.llmProvider, "fake-chat");
  assert.equal(one.matchMode, "contains");
  assert.equal(one.parseMaxChars, 8000);
  assert.deepStrictEqual(one.commonAnswers, ["邀请码1234"]);
  assert.equal(one.meta.version, "v1.1.0");
  assert.deepStrictEqual(B.newQuestion(), {
    __template_key: "question_item",
    enabled: true,
    question: "",
    hint: "",
    answers: [],
    match_mode: "inherit",
    picks: 0,
    passes: 0,
    usable: false,
  });
});

/* ------------------------------------------------------------------ *
 * 2. validateQuestion
 * ------------------------------------------------------------------ */

check("2. validateQuestion：空题干 / 非法匹配方式拒绝，空 answers 允许", () => {
  assert.ok(typeof B.validateQuestion(null) === "string", "非对象应被拒绝");
  assert.ok(
    typeof B.validateQuestion({ question: "   ", answers: [], match_mode: "inherit" }) === "string",
    "空题干应被拒绝",
  );
  assert.ok(
    typeof B.validateQuestion({ question: "甲", answers: [], match_mode: "levenshtein" }) === "string",
    "非法 match_mode 应被拒绝",
  );
  assert.ok(
    typeof B.validateQuestion({ question: "甲", answers: ["甲", " "], match_mode: "inherit" }) === "string",
    "答案库里的空白项应被拒绝",
  );
  assert.ok(
    typeof B.validateQuestion({ question: "甲", answers: {}, match_mode: "inherit" }) === "string",
    "非列表答案应被拒绝",
  );
  assert.equal(
    B.validateQuestion({ question: "甲", answers: [], match_mode: "inherit" }),
    null,
    "空答案库应允许（依赖通用答案库）",
  );
  assert.equal(B.validateQuestion({ question: "甲", answers: "甲、第一", match_mode: "inherit" }), null);
  assert.equal(B.validateQuestion({ question: "甲", answers: ["甲"], match_mode: "exact" }), null);
  assert.ok(
    typeof B.validateQuestion({ question: "甲", answers: [], match_mode: "fuzzy" }, ["inherit", "contains"]) ===
      "string",
    "应按传入的 options 判定匹配方式",
  );
});

/* ------------------------------------------------------------------ *
 * 3. validateBank
 * ------------------------------------------------------------------ */

check("3. validateBank：0 条可用题目进 warnings 而不是 errors", () => {
  const onlyDisabled = B.validateBank({
    questions: [
      {
        __template_key: "question_item",
        enabled: false,
        question: "甲题",
        hint: "",
        answers: ["甲"],
        match_mode: "inherit",
      },
    ],
    common_answers: [],
    match_mode_options: MATCH_MODE_OPTIONS.slice(),
  });
  assert.deepStrictEqual(onlyDisabled.errors, [], "停用题目本身不是错误");
  assert.ok(onlyDisabled.warnings.length >= 1, "0 条可用题目应进 warnings");

  const emptyBank = B.validateBank({ questions: [], common_answers: [] });
  assert.deepStrictEqual(emptyBank.errors, []);
  assert.ok(emptyBank.warnings.length >= 1);

  const broken = B.validateBank({
    questions: [{ question: "  ", answers: [], match_mode: "乱写" }],
    common_answers: [],
  });
  assert.equal(broken.errors.length, 1, "空题干应进 errors");
  assert.ok(broken.warnings.length >= 1, "同时没有可用题目应进 warnings");

  const good = B.validateBank({
    questions: [
      {
        __template_key: "question_item",
        enabled: true,
        question: "甲题",
        hint: "",
        answers: ["甲"],
        match_mode: "inherit",
      },
    ],
    common_answers: [],
    match_mode_options: MATCH_MODE_OPTIONS.slice(),
  });
  assert.deepStrictEqual(good.errors, []);
  assert.deepStrictEqual(good.warnings, [], "有可用题目时不应有 warning");

  // 通用答案库能让“没有该题答案”的题目变成可用
  const viaCommon = B.validateBank({
    questions: [{ question: "口令题", enabled: true, answers: [], match_mode: "inherit" }],
    common_answers: ["邀请码1234"],
  });
  assert.deepStrictEqual(viaCommon.errors, []);
  assert.deepStrictEqual(viaCommon.warnings, []);
});

/* ------------------------------------------------------------------ *
 * 4. diffBank
 * ------------------------------------------------------------------ */

check("4. diffBank 只对实质变化返回 true（忽略 picks/passes/usable）", () => {
  const base = makeBank();
  const copy = () => JSON.parse(JSON.stringify(base));

  assert.equal(B.diffBank(copy(), base), false, "完全相同时应返回 false");

  const changedAnswers = copy();
  changedAnswers.questions[0].answers = ["甲"];
  assert.equal(B.diffBank(changedAnswers, base), true, "改答案应返回 true");

  const changedQuestion = copy();
  changedQuestion.questions[0].question = "丙题";
  assert.equal(B.diffBank(changedQuestion, base), true, "改题干应返回 true");

  const changedHint = copy();
  changedHint.questions[0].hint = "提示乙";
  assert.equal(B.diffBank(changedHint, base), true, "改提示应返回 true");

  const changedMode = copy();
  changedMode.questions[0].match_mode = "fuzzy";
  assert.equal(B.diffBank(changedMode, base), true, "改匹配方式应返回 true");

  const toggled = copy();
  toggled.questions[0].enabled = false;
  assert.equal(B.diffBank(toggled, base), true, "启用/停用应返回 true");

  const added = copy();
  added.questions.push({
    __template_key: "question_item",
    enabled: true,
    question: "丙题",
    hint: "",
    answers: [],
    match_mode: "inherit",
  });
  assert.equal(B.diffBank(added, base), true, "新增应返回 true");

  const removed = copy();
  removed.questions.pop();
  assert.equal(B.diffBank(removed, base), true, "删除应返回 true");

  const reordered = copy();
  reordered.questions.reverse();
  assert.equal(B.diffBank(reordered, base), true, "顺序变化应返回 true");

  const changedCommon = copy();
  changedCommon.common_answers = ["邀请码1234", "新口令"];
  assert.equal(B.diffBank(changedCommon, base), true, "改通用答案库应返回 true");

  const onlyStats = copy();
  onlyStats.questions[0].picks = 99;
  onlyStats.questions[0].passes = 42;
  onlyStats.questions[0].usable = false;
  assert.equal(B.diffBank(onlyStats, base), false, "只改 picks/passes/usable 应返回 false");

  const statsOnSecond = copy();
  statsOnSecond.questions[1].picks = 7;
  assert.equal(B.diffBank(statsOnSecond, base), false);

  // 空白差异不影响结论（两端同样归一化）
  const trailing = copy();
  trailing.questions[0].answers = ["甲", "第一", " "];
  assert.equal(B.diffBank(trailing, base), false, "归一化后无变化应返回 false");
});

/* ------------------------------------------------------------------ *
 * 5. parseListInput
 * ------------------------------------------------------------------ */

check("5. parseListInput 按分隔符拆分、去空、去重、保持顺序", () => {
  assert.deepStrictEqual(B.parseListInput("123\n456, 123，789;"), ["123", "456", "789"]);
  assert.deepStrictEqual(B.parseListInput("  a ,, a ；b、c\r\nd "), ["a", "b", "c", "d"]);
  assert.deepStrictEqual(B.parseListInput(""), []);
  assert.deepStrictEqual(B.parseListInput(null), []);
  assert.deepStrictEqual(B.parseListInput(undefined), []);
  assert.deepStrictEqual(B.parseListInput(["a", " b ", "a"]), ["a", "b"]);
});

/* ------------------------------------------------------------------ *
 * 6. draftToQuestion
 * ------------------------------------------------------------------ */

check("6. draftToQuestion：answers 支持字符串，非法 match_mode 回退 inherit", () => {
  const fromText = B.draftToQuestion({ question: "x", answers: "A、B", match_mode: "乱写" });
  assert.deepStrictEqual(fromText.answers, ["A", "B"]);
  assert.equal(fromText.match_mode, "inherit");
  assert.equal(fromText.__template_key, "question_item");
  assert.equal(fromText.question, "x");
  assert.equal(fromText.hint, "");
  assert.equal(fromText.enabled, true);
  assert.equal(fromText.usable, false);
  assert.deepStrictEqual(Object.keys(fromText).sort(), Object.keys(B.newQuestion()).sort());

  const fromArray = B.draftToQuestion({ question: " y ", answers: ["A", "A", "B"], match_mode: "exact", hint: " h " });
  assert.deepStrictEqual(fromArray.answers, ["A", "B"]);
  assert.equal(fromArray.question, "y");
  assert.equal(fromArray.hint, "h");
  assert.equal(fromArray.match_mode, "exact");

  const dirty = B.draftToQuestion(undefined);
  assert.equal(dirty.question, "");
  assert.deepStrictEqual(dirty.answers, []);
  assert.equal(dirty.match_mode, "inherit");
});

/* ------------------------------------------------------------------ *
 * 7. statsText / matchModeLabel / usableCount / charCount
 * ------------------------------------------------------------------ */

check("7. statsText / matchModeLabel / usableCount / charCount", () => {
  assert.equal(B.statsText({ picks: 3, passes: 1 }), "抽中 3 / 通过 1");
  assert.equal(B.statsText({}), "抽中 0 / 通过 0");
  assert.equal(B.statsText(null), "抽中 0 / 通过 0");
  assert.equal(B.statsText({ picks: "5", passes: 2 }), "抽中 5 / 通过 2");

  assert.equal(B.matchModeLabel("inherit", "contains"), "跟随全局(contains)");
  assert.equal(B.matchModeLabel("inherit", "fuzzy"), "跟随全局(fuzzy)");
  assert.equal(B.matchModeLabel("exact", "contains"), "完全相等");
  assert.equal(B.matchModeLabel("fuzzy", ""), "模糊相似");
  assert.equal(B.matchModeLabel("regex"), "正则");
  assert.equal(B.matchModeLabel("inherit", ""), "跟随全局");

  assert.equal(B.usableCount(makeBank()), 1);
  assert.equal(B.usableCount({ questions: [{ usable: true }, { usable: false }, {}] }), 1);
  assert.equal(B.usableCount({ questions: [] }), 0);
  assert.equal(B.usableCount(null), 0);
  assert.equal(B.usableCount([{ usable: true }, { usable: true }]), 2);

  assert.equal(B.charCount("abc"), 3);
  assert.equal(B.charCount(""), 0);
  assert.equal(B.charCount(null), 0);
  assert.equal(B.charCount(undefined), 0);
  assert.equal(B.charCount("😀"), 1, "按码点计数，与后端 len(text) 对齐");
  assert.equal(B.charCount("甲乙\n丙"), 4);
});

/* ------------------------------------------------------------------ *
 * 8. 静态资源与 bridge 调用
 * ------------------------------------------------------------------ */

check("8. 页面静态资源与 bridge 调用检查", () => {
  for (const file of [HTML_PATH, APP_PATH, CSS_PATH, BANK_SRC]) {
    assert.ok(fs.existsSync(file), `缺少文件：${file}`);
  }

  const html = fs.readFileSync(HTML_PATH, "utf8");
  assert.ok(html.includes("./app.js"), "index.html 应通过相对路径引用 ./app.js");
  assert.ok(html.includes("./style.css"), "index.html 应通过相对路径引用 ./style.css");
  assert.ok(html.includes('type="module"'), "index.html 应以 type=module 加载外部脚本");
  assert.ok(!html.includes("bridge-sdk"), "index.html 不应手动引入 bridge-sdk（AstrBot 会自动注入）");
  assert.ok(!html.includes("asset_token"), "index.html 不应包含 asset_token");
  assert.ok(!/<script(?![^>]*\bsrc=)[^>]*>/i.test(html), "index.html 不应包含内联脚本");
  assert.ok(!/href="\.\.\//.test(html) && !/src="\.\.\//.test(html), "index.html 不应使用 .. 相对路径");
  assert.ok(!/https?:\/\//.test(html), "index.html 不应引用 CDN / 外链资源");
  assert.ok(html.includes("aria-describedby") || html.includes("aria-live"), "index.html 应包含无障碍属性");

  const app = fs.readFileSync(APP_PATH, "utf8");
  assert.ok(app.includes("AstrBotPluginPage"), "app.js 应使用 window.AstrBotPluginPage");
  assert.ok(app.includes("function unwrap("), "app.js 应包含信封格式归一化 unwrap()");
  assert.ok(app.includes('apiGet("questions")'), 'app.js 应调用 apiGet("questions")');
  assert.ok(app.includes('apiPost("questions"'), 'app.js 应调用 apiPost("questions"');
  assert.ok(app.includes('apiPost("questions/parse"'), 'app.js 应调用 apiPost("questions/parse"');
  assert.ok(app.includes("bridge.ready("), "app.js 应等待 bridge.ready()");
  assert.ok(app.includes("getContext("), "app.js 应读取 bridge.getContext()");
  assert.ok(app.includes("onContext("), "app.js 应订阅 bridge.onContext()");
  assert.ok(app.includes('bridge.t("pages.questions.title"'), "app.js 应用 bridge.t 取标题");
  assert.ok(
    /unwrap\(\s*await\s+bridge\.apiGet/.test(app),
    "app.js 应对 apiGet 结果调用 unwrap()",
  );
  assert.ok(
    /unwrap\(\s*await\s+bridge\.apiPost/.test(app),
    "app.js 应对 apiPost 结果调用 unwrap()",
  );
  assert.ok(app.includes("响应格式异常"), "app.js 应对缺失 questions 数组给出「响应格式异常」");
  assert.ok(app.includes("warnings"), "app.js 应处理后端返回的 warnings");
  assert.ok(app.includes('showMessage("warning"'), "app.js 应用警告样式展示 warnings");
  assert.ok(app.includes("没有可用的对话模型"), "app.js 应说明模型不可用的原因");
  assert.ok(app.includes("errorRaw(") && app.includes("setParseRaw("), "app.js 应支持展示模型原始输出");
  assert.ok(app.includes("别忘了点"), "app.js 加入草稿后应提示还未保存");
  assert.ok(/String\(err\?\.message \?\? err \?\? /.test(app), "错误文案应用防御式取法");

  const misc = fs.readFileSync(BANK_SRC, "utf8");
  assert.ok(!/\bdocument\./.test(misc), "bank.js 不应使用 document");
  assert.ok(!/\bwindow\./.test(misc), "bank.js 不应使用 window");
  assert.ok(!/localStorage|sessionStorage/.test(misc), "bank.js 不应使用存储 API");
  assert.ok(!/\bfetch\s*\(/.test(misc), "bank.js 不应使用 fetch");

  const css = fs.readFileSync(CSS_PATH, "utf8");
  assert.ok(css.includes("data-theme"), 'style.css 应包含 [data-theme="dark"] 覆盖');
  assert.ok(css.includes(".chips-editor"), "style.css 应定义 chips 编辑器样式");
  assert.ok(css.includes("@media"), "style.css 应包含窄屏适配");
});

/* ------------------------------------------------------------------ *
 * 9. i18n
 * ------------------------------------------------------------------ */

check("9. i18n 追加 questions 且保留 settings", () => {
  const zh = JSON.parse(fs.readFileSync(I18N_ZH, "utf8"));
  assert.equal(zh.pages.questions.title, "题库管理", "zh-CN 缺少 pages.questions.title");
  assert.equal(
    zh.pages.questions.description,
    "展开式问题列表，可写答案，也能让大模型从文本自动填入",
    "zh-CN 的 questions.description 与要求不一致",
  );
  assert.ok(zh.pages.settings && zh.pages.settings.title, "zh-CN 的 pages.settings.title 不能被删掉");
  assert.equal(zh.pages.settings.title, "审核群设置");

  const en = JSON.parse(fs.readFileSync(I18N_EN, "utf8"));
  assert.ok(en.pages.questions && en.pages.questions.title, "en-US 缺少 pages.questions.title");
  assert.ok(en.pages.questions.description, "en-US 缺少 pages.questions.description");
  assert.ok(en.pages.settings && en.pages.settings.title, "en-US 的 pages.settings.title 不能被删掉");
});

/* ------------------------------------------------------------------ *
 * 10. 可展开列表的无障碍与模板字段
 * ------------------------------------------------------------------ */

check("10. app.js 使用 aria-expanded 与 __template_key", () => {
  const app = fs.readFileSync(APP_PATH, "utf8");
  assert.ok(app.includes("aria-expanded"), "可展开的问题列表需要 aria-expanded");
  assert.ok(app.includes("aria-controls"), "展开按钮应通过 aria-controls 关联内容区");
  assert.ok(app.includes("__template_key"), "保存时必须带上 __template_key");
  assert.ok(app.includes("confirm("), "删除题目应二次确认");

  const html = fs.readFileSync(HTML_PATH, "utf8");
  assert.ok(/<label[^>]*for=/.test(html), "index.html 应有 label for 关联控件");
  assert.ok(html.includes('id="btn-save"') && html.includes('id="btn-reload"'), "缺少工具条按钮");
  assert.ok(html.includes('id="parse-text"'), "缺少大模型解析输入框");
  assert.ok(html.includes("全部展开") && html.includes("全部折叠"), "工具条应提供全部展开 / 全部折叠");
  assert.ok(html.includes("保存题库"), "工具条应提供保存题库");
  assert.ok(html.includes("新增题目"), "工具条应提供新增题目");
  assert.ok(html.includes("aria-expanded"), "原始输出折叠区应有 aria-expanded");
  assert.ok(html.includes("通用答案库"), "页面应有通用答案库区块");
  assert.ok(html.includes("大模型自动填入"), "页面应有大模型自动填入区块");
  assert.ok(html.includes("查看模型原始输出"), "页面应提供模型原始输出折叠区");
  assert.ok(html.includes('id="parse-raw"') && html.includes('id="parse-drafts"'), "缺少解析结果容器");
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
