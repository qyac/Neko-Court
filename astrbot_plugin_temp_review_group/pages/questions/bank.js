/**
 * bank.js —— 插件 Pages「questions」的纯函数模块。
 *
 * 约束：本文件不得引用 DOM、window、document、网络请求或浏览器存储 API，
 * 只做数据变换与校验，浏览器（app.js）与 Node 测试（selftest_questions_page.mjs）共用。
 */

/** 题库条目的模板 key（后端 _conf_schema.json 中 templates.question_item）。 */
export const QUESTION_TEMPLATE_KEY = "question_item";

/** 单题匹配方式的可选值（与后端 MATCH_MODE_CHOICES 一致）。 */
export const DEFAULT_MATCH_MODE_OPTIONS = ["inherit", "contains", "exact", "regex", "fuzzy"];

/** 匹配方式的中文标签。 */
export const MATCH_MODE_LABELS = {
  inherit: "跟随全局",
  contains: "包含关键词",
  exact: "完全相等",
  regex: "正则",
  fuzzy: "模糊相似",
};

/** 后端缺省值（拿不到时用于兜底展示）。 */
export const DEFAULT_GLOBAL_MATCH_MODE = "contains";
export const DEFAULT_FUZZY_THRESHOLD = 0.8;
export const DEFAULT_REVIEW_MODE = "rule";
export const DEFAULT_PARSE_LIMIT = 30;
export const DEFAULT_PARSE_MAX_CHARS = 8000;

const isPlainObject = (value) =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** 任意脏值 → 字符串（对象/数组等无法表达的一律给空串）。 */
function toText(value) {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (typeof value === "boolean") return String(value);
  return "";
}

/** 任意脏值 → 布尔值；无法判断时用 fallback。 */
function toBool(value, fallback = false) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (value === 1) return true;
    if (value === 0) return false;
    return fallback;
  }
  if (typeof value === "string") {
    const text = value.trim().toLowerCase();
    if (["true", "1", "yes", "on", "是", "开启", "启用"].includes(text)) return true;
    if (["false", "0", "no", "off", "否", "关闭", "停用", ""].includes(text)) return false;
  }
  return fallback;
}

/** 任意脏值 → 整数；无法判断时用 fallback。 */
function toInt(value, fallback = 0) {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value === "string" && value.trim()) {
    const num = Number(value.trim());
    if (Number.isFinite(num)) return Math.trunc(num);
  }
  return fallback;
}

/** 任意脏值 → 有限数字；无法判断时用 fallback。 */
function toNumber(value, fallback = 0) {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const num = Number(value.trim());
    if (Number.isFinite(num)) return num;
  }
  return fallback;
}

/** 归一化匹配方式候选列表（去空、去重、保序）；为空时回落到默认 5 项。 */
function normalizeOptions(options) {
  if (!Array.isArray(options)) return DEFAULT_MATCH_MODE_OPTIONS.slice();
  const out = [];
  for (const item of options) {
    const text = toText(item).trim().toLowerCase();
    if (!text || out.includes(text)) continue;
    out.push(text);
  }
  return out.length ? out : DEFAULT_MATCH_MODE_OPTIONS.slice();
}

/** 字符串列表：数组或分隔符文本 → 去空、去重、保序的字符串数组。 */
function toStringList(value) {
  if (Array.isArray(value)) {
    const out = [];
    for (const item of value) {
      const text = toText(item).trim();
      if (!text || out.includes(text)) continue;
      out.push(text);
    }
    return out;
  }
  if (typeof value === "string") return parseListInput(value);
  return [];
}

/* ------------------------------------------------------------------ *
 * 题库条目
 * ------------------------------------------------------------------ */

/**
 * 脏数据兜底：任何输入都能得到一条结构完整的题目。
 * @param {unknown} raw 后端返回或用户编辑中的条目
 * @param {string[]} [options] 允许的匹配方式；缺省用内置 5 项
 */
export function normalizeQuestion(raw, options = DEFAULT_MATCH_MODE_OPTIONS) {
  const source = isPlainObject(raw) ? raw : {};
  const allowed = normalizeOptions(options);
  const mode = toText(source.match_mode).trim().toLowerCase();
  return {
    __template_key: toText(source.__template_key).trim() || QUESTION_TEMPLATE_KEY,
    enabled: toBool(source.enabled, true),
    question: toText(source.question),
    hint: toText(source.hint),
    answers: toStringList(source.answers),
    match_mode: allowed.includes(mode) ? mode : "inherit",
    picks: toInt(source.picks, 0),
    passes: toInt(source.passes, 0),
    usable: toBool(source.usable, false),
  };
}

/**
 * 脏数据兜底：GET "questions" / POST "questions" 的 data → 前端模型。
 * 只返回契约里列出的字段（saved / warnings 由 app.js 直接从原始响应里取）。
 */
export function normalizeBank(data) {
  const source = isPlainObject(data) ? data : {};
  const matchModeOptions = normalizeOptions(source.match_mode_options);
  const questions = Array.isArray(source.questions)
    ? source.questions.map((item) => normalizeQuestion(item, matchModeOptions))
    : [];

  const globalMode = toText(source.match_mode).trim().toLowerCase();
  const matchMode =
    globalMode && globalMode !== "inherit" && matchModeOptions.includes(globalMode)
      ? globalMode
      : DEFAULT_GLOBAL_MATCH_MODE;

  const threshold = toNumber(source.fuzzy_threshold, DEFAULT_FUZZY_THRESHOLD);

  return {
    questions,
    commonAnswers: toStringList(source.common_answers),
    matchMode,
    fuzzyThreshold: Math.min(1, Math.max(0, threshold)),
    matchModeOptions,
    reviewMode: toText(source.review_mode).trim() || DEFAULT_REVIEW_MODE,
    llmAvailable: toBool(source.llm_available, false),
    llmProvider: toText(source.llm_provider).trim(),
    parseLimit: Math.max(1, toInt(source.parse_limit, DEFAULT_PARSE_LIMIT)),
    parseMaxChars: Math.max(1, toInt(source.parse_max_chars, DEFAULT_PARSE_MAX_CHARS)),
    meta: isPlainObject(source.meta) ? Object.assign({}, source.meta) : {},
  };
}

/** 新增题目的初始值（新增条目一律带 __template_key）。 */
export function newQuestion() {
  return {
    __template_key: QUESTION_TEMPLATE_KEY,
    enabled: true,
    question: "",
    hint: "",
    answers: [],
    match_mode: "inherit",
    picks: 0,
    passes: 0,
    usable: false,
  };
}

/**
 * 校验单条题目。
 * @returns {string|null} null 表示通过，否则是中文错误文案
 */
export function validateQuestion(q, options = DEFAULT_MATCH_MODE_OPTIONS) {
  if (!isPlainObject(q)) return "题目格式不正确（应为对象）";
  if (!toText(q.question).trim()) return "题干不能为空";
  const allowed = normalizeOptions(options);
  const mode = toText(q.match_mode).trim().toLowerCase();
  if (!allowed.includes(mode)) {
    return `匹配方式 ${mode || "（空）"} 不在可选范围内：${allowed.join(" / ")}`;
  }
  const answers = q.answers;
  if (answers === undefined || answers === null) return null;
  if (typeof answers === "string") return null; // 文本形式交给 parseListInput 兜底
  if (!Array.isArray(answers)) return "答案库格式不正确（应为列表）";
  for (const item of answers) {
    if (!toText(item).trim()) return "答案库里存在空白项";
  }
  return null;
}

/**
 * 客户端可用性判定，口径与后端 _question_usable 一致：
 * 启用 + 有题干 +（该题有答案 或 配了通用答案库）。
 */
function isUsableNow(question, hasCommonAnswers) {
  const hasQuestion = toText(question.question).trim() !== "";
  const hasAnswers = Array.isArray(question.answers) && question.answers.length > 0;
  return Boolean(question.enabled) && hasQuestion && (hasAnswers || hasCommonAnswers);
}

/**
 * 校验整个题库。
 * 逐题错误进 errors（阻止保存）；「0 条可用题目」只进 warnings（后端也允许保存）。
 * @returns {{errors: string[], warnings: string[]}}
 */
export function validateBank(bank) {
  const data = normalizeBank(bank);
  const errors = [];
  data.questions.forEach((question, index) => {
    const error = validateQuestion(question, data.matchModeOptions);
    if (error) errors.push(`第 ${index + 1} 题：${error}`);
  });

  // 不依赖后端回传的 usable 标记：用户刚改过、或手工构造的题库也能算准
  const hasCommon = data.commonAnswers.length > 0;
  const usable = data.questions.filter(
    (question) => question.usable === true || isUsableNow(question, hasCommon),
  ).length;

  const warnings = [];
  if (usable === 0) {
    warnings.push(
      data.questions.length
        ? "没有任何可用题目：题目需要「已启用」、填好题干，并至少有一个该题答案或通用答案，否则新人入群不会收到问题。"
        : "题库是空的：新人入群不会收到审核问题，可以在下方新增题目，或让大模型自动填入。",
    );
  }
  return { errors, warnings };
}

/** 供 diffBank 使用的规范形态：只保留可编辑字段（忽略 picks/passes/usable）。 */
function canonicalBank(bank) {
  const data = normalizeBank(bank);
  return JSON.stringify({
    questions: data.questions.map((question) => ({
      enabled: question.enabled,
      question: question.question,
      hint: question.hint,
      answers: question.answers,
      match_mode: question.match_mode,
    })),
    commonAnswers: data.commonAnswers,
  });
}

/** 与基准值相比是否有实质改动（含顺序变化、增删、字段变化）。 */
export function diffBank(current, baseline) {
  return canonicalBank(current) !== canonicalBank(baseline);
}

/* ------------------------------------------------------------------ *
 * 输入解析 / 展示
 * ------------------------------------------------------------------ */

/** 换行、逗号（中英文）、分号（中英文）、顿号分隔；去空、去重、保持顺序。 */
export function parseListInput(text) {
  if (text === undefined || text === null) return [];
  const raw = Array.isArray(text) ? text.map((item) => toText(item)) : String(text).split(/[\n\r,，;；、]+/);
  const out = [];
  const seen = new Set();
  for (const item of raw) {
    const value = String(item).trim();
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

/** 大模型解析出的草稿 → 题库条目（answers 支持字符串或数组，非法 match_mode 回退 inherit）。 */
export function draftToQuestion(draft) {
  const source = isPlainObject(draft) ? draft : {};
  const mode = toText(source.match_mode).trim().toLowerCase();
  const question = newQuestion();
  question.enabled = toBool(source.enabled, true);
  question.question = toText(source.question).trim();
  question.hint = toText(source.hint).trim();
  question.answers = toStringList(source.answers);
  question.match_mode = DEFAULT_MATCH_MODE_OPTIONS.includes(mode) ? mode : "inherit";
  return question;
}

/** 每题统计文案，例如「抽中 3 / 通过 1」。 */
export function statsText(q) {
  const source = isPlainObject(q) ? q : {};
  return `抽中 ${toInt(source.picks, 0)} / 通过 ${toInt(source.passes, 0)}`;
}

/** 匹配方式展示文案；inherit 显示为「跟随全局(contains)」。 */
export function matchModeLabel(mode, globalMode) {
  const key = toText(mode).trim().toLowerCase();
  if (key === "inherit") {
    const global = toText(globalMode).trim();
    return global ? `跟随全局(${global})` : "跟随全局";
  }
  return MATCH_MODE_LABELS[key] || key || "未知";
}

/** usable === true 的题目条数。 */
export function usableCount(bank) {
  const list = Array.isArray(bank)
    ? bank
    : isPlainObject(bank) && Array.isArray(bank.questions)
      ? bank.questions
      : [];
  let count = 0;
  for (const item of list) {
    if (toBool(item && item.usable, false)) count += 1;
  }
  return count;
}

/** 字符数（按码点计数，与后端 Python len(text) 对齐）。 */
export function charCount(text) {
  if (text === undefined || text === null) return 0;
  const value = typeof text === "string" ? text : toText(text);
  return Array.from(value).length;
}
