/**
 * review-site/static/form.js
 *
 * 审核站纯函数工具集：UID 解析、表单校验、状态映射、时间格式化、分页边界与 CSV 转义。
 * ESM 模块，可在浏览器与 Node 中直接 import。内部只做字符串/数字运算，
 * 不读取任何宿主环境的全局对象，也不发起任何请求，因此可被 Node 单元测试直接调用。
 */

/** 状态 → 中文标签。 */
export const STATUS_LABELS = Object.freeze({
  pending: "待核验",
  approved: "已通过",
  rejected: "未通过",
  manual: "待人工",
  blocked: "已拉黑",
});

/** 已知状态的稳定顺序（渲染统计/筛选时用）。 */
export const STATUS_ORDER = Object.freeze([
  "pending",
  "approved",
  "rejected",
  "manual",
  "blocked",
]);

/** 备注最大长度。 */
export const NOTE_MAX = 200;
/** QQ 号长度范围。 */
export const QQ_MIN_LEN = 5;
export const QQ_MAX_LEN = 12;
/** B站 UID 长度范围（1 位数字视为不合法）。 */
export const UID_MIN_LEN = 2;        // 纯数字：至少 2 位，避免把「我有 3 个号」当成 UID
export const UID_MIN_LEN_EXPLICIT = 1; // 明确写了 UID: / 主页链接：1 位也认（B站存在 mid=2 这种老账号）
export const UID_MAX_LEN = 15;       // B站 mid 是 64 位整数，11~15 位接口都接受，上限给足不挡真人
/** 后端固定 page size。 */
export const DEFAULT_PAGE_SIZE = 50;

/** 判定为终态的状态：轮询遇到这些状态就停止。 */
const TERMINAL_STATUSES = Object.freeze(["approved", "rejected", "manual"]);

/** “没有值”的占位符。 */
export const EMPTY_TEXT = "\u2014";

/* ------------------------------------------------------------------ *
 * 内部工具
 * ------------------------------------------------------------------ */

function pad2(value) {
  return String(value).padStart(2, "0");
}

function isBlank(value) {
  return value === null || value === undefined || value === "" || value === false;
}

/**
 * 把秒/毫秒时间戳统一成毫秒；空值、非数字、<=0 视为“无时间”。
 * 约定：后端下发 Unix 秒（created_at/decided_at/last_sync_at），
 * 但若调用方误传毫秒（>= 1e12）也按毫秒处理。
 */
function toMillis(value) {
  if (isBlank(value)) return null;
  const n = typeof value === "number" ? value : Number(String(value).trim());
  if (!Number.isFinite(n) || n <= 0) return null;
  return n >= 1e12 ? n : n * 1000;
}

/** 把秒/毫秒时间戳统一成秒；无效返回 null。 */
function toSeconds(value) {
  const ms = toMillis(value);
  return ms === null ? null : ms / 1000;
}

/** 只保留数字字符。 */
function digitsOnly(value) {
  return String(value === null || value === undefined ? "" : value).replace(/\D+/g, "");
}

/** 校验 UID 数字串长度（1 位视为不合法），合法则原样返回，否则 null。 */
function normalizeUidDigits(raw, minLen = UID_MIN_LEN) {
  const s = digitsOnly(raw);
  if (!s) return null;
  if (s.length < minLen || s.length > UID_MAX_LEN) return null;
  if (/^0+$/.test(s)) return null;
  return s;
}

/* ------------------------------------------------------------------ *
 * UID / 表单
 * ------------------------------------------------------------------ */

/**
 * 从纯数字或 B站主页链接里抽出 UID 字符串。
 * 支持：`12345678`、`UID:12345678`、`uid=998877`、
 *       `https://space.bilibili.com/12345678`、`space.bilibili.com/12345678?from=x`。
 * 不合法（空、含非数字、1 位、超过 15 位）一律返回 null。
 * 说明：B站 mid 是 64 位整数，实测 11~15 位接口都接受，所以上限放宽到 15 位；
 * 填错的 UID 会由站点核验返回"不存在"，不会被误放行。
 */
export function extractUid(text) {
  if (text === null || text === undefined) return null;
  const raw = String(text).trim();
  if (!raw) return null;

  // 纯数字直接判定
  if (/^\d+$/.test(raw)) return normalizeUidDigits(raw, UID_MIN_LEN);

  const lower = raw.toLowerCase();
  let match = lower.match(/space\.bilibili\.com\/(\d+)/);
  if (!match) match = lower.match(/space\.bilibili\.com[^\d]{0,8}?(\d{2,})/);
  if (!match) match = lower.match(/\buid\s*[=:：]\s*(\d+)/);
  if (!match) match = lower.match(/[?&]uid=(\d+)/);
  if (!match) return null;
  return normalizeUidDigits(match[1], UID_MIN_LEN_EXPLICIT);
}

/**
 * 校验申请表单。
 * @returns {{ok: boolean, errors: {qq?: string, uid?: string, note?: string}}}
 */
export function validateForm({ qq, uid, note } = {}) {
  const errors = {};

  const qqText = isBlank(qq) ? "" : String(qq).trim();
  if (!qqText) {
    errors.qq = "请填写 QQ 号";
  } else if (!/^\d+$/.test(qqText)) {
    errors.qq = "QQ 号只能是数字";
  } else if (qqText.length < QQ_MIN_LEN || qqText.length > QQ_MAX_LEN) {
    errors.qq = `QQ 号需为 ${QQ_MIN_LEN}~${QQ_MAX_LEN} 位数字`;
  }

  const uidText = isBlank(uid) ? "" : String(uid).trim();
  if (!uidText) {
    errors.uid = "请填写 B站 UID 或主页链接";
  } else if (extractUid(uidText) === null) {
    errors.uid = `UID 需为 ${UID_MIN_LEN}~${UID_MAX_LEN} 位数字，或 B站主页链接`;
  }

  const noteText = isBlank(note) ? "" : String(note).trim();
  if (noteText.length > NOTE_MAX) {
    errors.note = `备注最多 ${NOTE_MAX} 个字`;
  }

  return { ok: Object.keys(errors).length === 0, errors };
}

/**
 * 规范化后的申请请求体：去空格，UID 只留数字，note 缺省为空串。
 *
 * 网页答题：只有在页面上真的拿到并显示了题目（question 非空）时才带上
 * `question` / `answer` 两个键；不需要答题时请求体保持原样（后端会按需忽略）。
 */
export function buildApplyPayload({ qq, uid, note, question, answer } = {}) {
  const qqText = isBlank(qq) ? "" : String(qq).trim();
  const uidText = isBlank(uid) ? "" : String(uid).trim();
  const extracted = extractUid(uidText);
  const payload = {
    qq: qqText,
    uid: extracted === null ? digitsOnly(uidText) : extracted,
    note: isBlank(note) ? "" : String(note).trim(),
  };

  const asked = isBlank(question) ? "" : String(question).trim();
  if (asked) {
    payload.question = asked;
    payload.answer = isBlank(answer) ? "" : String(answer).trim();
  }
  return payload;
}

/** 管理后台决策/删除请求体。 */
export function buildDecisionPayload(csrf, id, action, note) {
  const numericId = Number(id);
  return {
    csrf: isBlank(csrf) ? "" : String(csrf),
    id: Number.isFinite(numericId) && String(id).trim() !== "" ? numericId : id,
    action: isBlank(action) ? "" : String(action),
    note: isBlank(note) ? "" : String(note).trim(),
  };
}

/* ------------------------------------------------------------------ *
 * 状态 / 时间
 * ------------------------------------------------------------------ */

/** 状态 → 中文标签；未知状态返回“未知”。 */
export function statusLabel(status) {
  const key = isBlank(status) ? "" : String(status);
  return Object.prototype.hasOwnProperty.call(STATUS_LABELS, key)
    ? STATUS_LABELS[key]
    : "未知";
}

/** 状态 → CSS 类名后缀；未知状态返回 `unknown`。 */
export function statusClass(status) {
  const key = isBlank(status) ? "" : String(status);
  return Object.prototype.hasOwnProperty.call(STATUS_LABELS, key) ? key : "unknown";
}

/** 是否终态（approved/rejected/manual 为 true；pending/blocked 为 false）。 */
export function isTerminal(status) {
  const key = isBlank(status) ? "" : String(status);
  return TERMINAL_STATUSES.indexOf(key) !== -1;
}

/** 本地时间字符串 `YYYY-MM-DD HH:MM`；空/非法返回 `—`。 */
export function formatTime(ts) {
  const ms = toMillis(ts);
  if (ms === null) return EMPTY_TEXT;
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return EMPTY_TEXT;
  return (
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}` +
    ` ${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  );
}

/**
 * 相对时间：刚刚 / N 分钟前 / N 小时前 / N 天前。
 * @param {number} ts 目标时间戳（秒或毫秒）
 * @param {number} [now] 参考时间戳，缺省为当前时间
 */
export function relativeTime(ts, now) {
  const target = toSeconds(ts);
  if (target === null) return EMPTY_TEXT;
  const reference = now === undefined || now === null || now === ""
    ? Date.now() / 1000
    : toSeconds(now);
  const base = reference === null ? Date.now() / 1000 : reference;

  let diff = Math.floor(base - target);
  if (!Number.isFinite(diff)) return EMPTY_TEXT;
  if (diff < 0) diff = 0;

  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/* ------------------------------------------------------------------ *
 * 统计 / 分页 / CSV
 * ------------------------------------------------------------------ */

/**
 * 汇总条目：总数、按状态计数（已知状态以 0 补全）、最新创建时间。
 * @param {Array<{status?: string, created_at?: number}>} items
 */
export function summarize(items) {
  const list = Array.isArray(items) ? items : [];
  const byStatus = {};
  for (const key of STATUS_ORDER) byStatus[key] = 0;

  let newestTs = 0;
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const key = isBlank(item.status) ? "unknown" : String(item.status);
    byStatus[key] = (Number.isFinite(byStatus[key]) ? byStatus[key] : 0) + 1;

    const created = Number(item.created_at);
    if (Number.isFinite(created) && created > newestTs) newestTs = created;
  }

  return { total: list.length, byStatus, newestTs };
}

/** 归一化 offset：非数字/负数 → 0，并取整。 */
function normalizeOffset(offset) {
  const n = Number(offset);
  if (!Number.isFinite(n) || n < 0) return 0;
  return Math.floor(n);
}

/** 归一化 pageSize：非正数 → 1。 */
function normalizePageSize(pageSize) {
  const n = Number(pageSize);
  if (!Number.isFinite(n) || n < 1) return 1;
  return Math.floor(n);
}

/**
 * 下一页的安全 offset：不会越过最后一页；若已在最后一页则保持不变；
 * offset 超过 total 时回到最后一页起点。
 */
export function nextOffset(offset, total, pageSize = DEFAULT_PAGE_SIZE) {
  const size = normalizePageSize(pageSize);
  const current = normalizeOffset(offset);
  const count = Number(total);
  if (!Number.isFinite(count) || count <= 0) return 0;

  const next = current + size;
  if (next >= count) {
    const last = Math.floor((count - 1) / size) * size;
    return last < current ? last : current;
  }
  return next;
}

/** 上一页的安全 offset：不会小于 0。 */
export function prevOffset(offset, pageSize = DEFAULT_PAGE_SIZE) {
  const size = normalizePageSize(pageSize);
  const current = normalizeOffset(offset);
  const prev = current - size;
  return prev < 0 ? 0 : prev;
}

/** CSV 单元格最小转义：含 `"` `,` 换行时加引号并把 `"` 写成 `""`。 */
export function csvCell(value) {
  if (value === null || value === undefined) return "";
  const text = typeof value === "string" ? value : String(value);
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

/* ------------------------------------------------------------------ *
 * 网页答题：申请页的题目模型
 * ------------------------------------------------------------------ */

/** 答案最长长度（与后端 answer[:200] 对齐，输入框用它做 maxlength）。 */
export const ANSWER_MAX = 200;
/** 题干最长长度（与后端 300 字上限对齐）。 */
export const QUESTION_MAX = 300;
/** 题库默认匹配方式（站点未配置时的兜底）。 */
export const DEFAULT_MATCH_MODE = "contains";
/** 模糊阈值默认值与区间（与后端 matching.clamp_threshold 一致）。 */
export const DEFAULT_FUZZY_THRESHOLD = 0.8;
export const MIN_FUZZY_THRESHOLD = 0.5;
export const MAX_FUZZY_THRESHOLD = 1;

/** 匹配方式 → 中文标签。 */
export const MATCH_MODE_LABELS = Object.freeze({
  inherit: "跟随全局",
  contains: "包含关键词",
  exact: "完全相等",
  regex: "正则",
  fuzzy: "模糊相似",
});

/** 出题来源 → 中文标签。 */
export const QUESTION_SOURCE_LABELS = Object.freeze({
  site: "站点题库",
  plugin: "插件题库",
});

/** 匹配方式下拉的兜底选项（后端会下发 match_mode_options）。 */
export const MATCH_MODE_OPTIONS = Object.freeze([
  "inherit",
  "contains",
  "exact",
  "regex",
  "fuzzy",
]);

/** 宽松布尔：字符串 "true"/"1"/"yes" 也算真；无法判断时用 fallback。 */
function boolValue(value, fallback = false) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const key = value.trim().toLowerCase();
    if (key === "true" || key === "1" || key === "yes" || key === "on") return true;
    if (key === "false" || key === "0" || key === "no" || key === "off" || key === "") return false;
  }
  return fallback;
}

/** 非负整数计数：脏数据/负数 → fallback。 */
function countValue(value, fallback = 0) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return fallback;
  return Math.floor(n);
}

/** 文本字段：null/undefined → 空串，其余 trim。 */
function textValue(value) {
  return isBlank(value) ? "" : String(value).trim();
}

/**
 * 申请页题目模型：任何脏数据都能得到结构完整的对象。
 * @returns {{enabled: boolean, question: string, hint: string, total: number, source: string}}
 */
export function normalizeQuestion(data) {
  const source = data && typeof data === "object" ? data : {};
  const rawSource = textValue(source.source).toLowerCase();
  return {
    // 后端明确返回 enabled:true 才算需要答题（缺失/脏数据一律按“不答题”处理）
    enabled: boolValue(source.enabled, false),
    question: textValue(source.question),
    hint: textValue(source.hint),
    total: countValue(source.total, 0),
    source: rawSource === "plugin" ? "plugin" : "site",
  };
}

/** 是否真的可以答题：enabled 为真且题干非空。 */
export function hasQuestion(data) {
  const info = normalizeQuestion(data);
  return info.enabled === true && info.question !== "";
}

/** 需要答题时答案不能为空；返回 null 表示通过，否则返回就地错误文案。 */
export function validateAnswerInput(questionEnabled, answer) {
  if (questionEnabled !== true) return null;
  const text = textValue(answer);
  if (!text) return "请先回答审核问题再提交";
  return null;
}

/**
 * 题目渲染顺序：站点题在前、插件题在后，各自保持原有顺序（稳定排序）。
 * @param {Array<{source?: string}>} items
 */
export function sortQuestions(items) {
  const list = Array.isArray(items) ? items : [];
  const site = [];
  const plugin = [];
  for (const item of list) {
    const source = item && typeof item === "object" ? textValue(item.source).toLowerCase() : "";
    if (source === "plugin") plugin.push(item);
    else site.push(item);
  }
  return site.concat(plugin);
}

/**
 * 管理后台顶部状态行的数据（脏数据兜底）。
 * @returns {{siteCount: number, pluginCount: number, activeCount: number, source: string, askEnabled: boolean}}
 */
export function questionStats(payload) {
  const data = payload && typeof payload === "object" ? payload : {};
  const site = Array.isArray(data.site) ? data.site : [];
  const plugin = Array.isArray(data.plugin) ? data.plugin : [];
  const rawSource = textValue(data.active_source).toLowerCase();
  return {
    siteCount: site.length,
    pluginCount: plugin.length,
    activeCount: countValue(data.active_count, 0),
    source: rawSource === "plugin" ? "plugin" : rawSource === "site" ? "site" : "",
    // 与后端默认值一致：字段缺失时按“开启答题”处理
    askEnabled: boolValue(data.ask_questions, true),
  };
}

/** 匹配方式 → 中文标签；未知模式原样返回（空值返回空串）。 */
export function matchModeLabel(mode) {
  const raw = textValue(mode);
  const key = raw.toLowerCase();
  return Object.prototype.hasOwnProperty.call(MATCH_MODE_LABELS, key)
    ? MATCH_MODE_LABELS[key]
    : raw;
}

/** 出题来源 → 中文标签；未知来源原样返回。 */
export function questionSourceLabel(source) {
  const key = textValue(source).toLowerCase();
  return Object.prototype.hasOwnProperty.call(QUESTION_SOURCE_LABELS, key)
    ? QUESTION_SOURCE_LABELS[key]
    : key;
}

/**
 * 列表输入解析：换行、逗号（中英文）、分号（中英文）、顿号分隔；
 * 去空、去重、保持顺序。数组输入同样会去空去重。
 */
export function parseListInput(text) {
  if (text === undefined || text === null) return [];
  const raw = Array.isArray(text) ? text : String(text).split(/[\n\r,，;；、]+/);
  const out = [];
  const seen = new Set();
  for (const item of raw) {
    const value = textValue(item);
    if (!value || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

/** 答案列表 → chips 编辑框回填文本（逗号分隔）。 */
export function chipsToText(list) {
  return parseListInput(list).join(", ");
}

/** 模糊阈值规范到 0.5~1.0；空值/脏数据用默认值 0.8。 */
export function normalizeThreshold(value, fallback = DEFAULT_FUZZY_THRESHOLD) {
  const raw = isBlank(value) ? NaN : Number(value);
  const fallbackNumber = Number(fallback);
  const base = Number.isFinite(raw)
    ? raw
    : Number.isFinite(fallbackNumber)
      ? fallbackNumber
      : DEFAULT_FUZZY_THRESHOLD;
  return Math.min(MAX_FUZZY_THRESHOLD, Math.max(MIN_FUZZY_THRESHOLD, base));
}
