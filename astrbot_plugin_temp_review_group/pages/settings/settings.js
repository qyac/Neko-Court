/**
 * settings.js —— 插件 Pages「settings」的纯函数模块。
 *
 * 约束：本文件不得引用浏览器 DOM、window、document、网络请求或浏览器存储 API，
 * 只做数据变换与校验，浏览器（app.js）与 Node 测试（selftest_settings_page.mjs）共用。
 */

/** 配置项分组（顺序即渲染顺序）。未在分组中出现的 schema key 会自动进入「其他」。 */
export const FIELD_GROUPS = [
  {
    id: "basic",
    title: "基础设置",
    keys: ["review_groups", "admin_ids", "exempt_user_ids"],
  },
  {
    id: "review",
    title: "审核流程",
    keys: [
      "questions",
      "common_answers",
      "match_mode",
      "fuzzy_threshold",
      "review_mode",
      "review_llm_provider",
      "llm_review_prompt",
      "llm_review_timeout_seconds",
      "max_attempts",
      "kick_on_fail",
      "reject_add_request",
      "auto_enroll_on_speak",
      "question_message",
      "retry_message",
      "success_message",
      "kick_message",
    ],
  },
  {
    id: "code",
    title: "验证码",
    keys: [
      "code_length",
      "code_charset",
      "code_reset_time",
      "code_send_mode",
      "private_code_message",
      "code_fallback_to_group",
      "private_send_channel",
    ],
  },
  {
    id: "cleanup",
    title: "每日清理",
    keys: [
      "cleanup_time",
      "cleanup_keep_approved",
      "cleanup_notice",
      "kick_interval_seconds",
      "keep_group_admins",
    ],
  },
  {
    id: "other",
    title: "其他",
    keys: ["timezone", "group_admin_can_query"],
  },
];

/** 兜底分组 id。 */
export const OTHER_GROUP_ID = "other";

/** 允许为空的字段：为空时只提示警告，不阻止保存（后端会接受）。 */
export const WARN_ONLY_KEYS = ["review_groups"];

const WARN_ONLY = new Set(WARN_ONLY_KEYS);

const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

const isPlainObject = (value) =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/* ------------------------------------------------------------------ *
 * 基础工具
 * ------------------------------------------------------------------ */

/** 深拷贝（只处理 JSON 可表达的数据）。 */
export function deepClone(value) {
  if (value === undefined) return undefined;
  if (value === null || typeof value !== "object") return value;
  if (Array.isArray(value)) return value.map((item) => deepClone(item));
  const out = {};
  for (const key of Object.keys(value)) out[key] = deepClone(value[key]);
  return out;
}

/** 深比较（忽略对象键顺序）。 */
export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b) return false;
  if (a === null || b === null) return false;
  if (typeof a !== "object") {
    // NaN 之类：按不等处理，避免误判为“未修改”
    return Number.isNaN(a) && Number.isNaN(b);
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) if (!deepEqual(a[i], b[i])) return false;
    return true;
  }
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  for (const key of ka) {
    if (!hasOwn(b, key)) return false;
    if (!deepEqual(a[key], b[key])) return false;
  }
  return true;
}

function toNumber(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const text = value.trim();
    if (!text) return null;
    const num = Number(text);
    return Number.isFinite(num) ? num : null;
  }
  return null;
}

function toBool(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") {
    const text = value.trim().toLowerCase();
    if (["true", "1", "yes", "on", "是"].includes(text)) return true;
    if (["false", "0", "no", "off", "否", ""].includes(text)) return false;
  }
  return null;
}

function toListArray(value) {
  if (Array.isArray(value)) {
    const out = [];
    for (const item of value) {
      if (item === undefined || item === null) continue;
      out.push(typeof item === "string" ? item : String(item));
    }
    return out;
  }
  if (typeof value === "string") return parseListInput(value);
  return null;
}

/** 某个字段类型的空值。 */
function typeDefault(node) {
  const type = node ? node.type : undefined;
  switch (type) {
    case "bool":
      return false;
    case "int":
    case "float": {
      const slider = isPlainObject(node.slider) ? node.slider : null;
      return slider && typeof slider.min === "number" ? slider.min : 0;
    }
    case "list":
    case "template_list":
      return [];
    default:
      return "";
  }
}

function nodeDefault(node) {
  return hasOwn(node, "default") ? node.default : typeDefault(node);
}

function normalizeIdList(value) {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item));
}

/* ------------------------------------------------------------------ *
 * schema → 字段 / 分组 / 默认值
 * ------------------------------------------------------------------ */

/** 按 schema 顺序返回 [{ key, node }]。 */
export function fieldsFromSchema(schema) {
  if (!isPlainObject(schema)) return [];
  return Object.keys(schema).map((key) => ({
    key,
    node: isPlainObject(schema[key]) ? schema[key] : {},
  }));
}

/** 按 FIELD_GROUPS 分组；schema 中存在但未列出的 key 归入「其他」，不漏不重。 */
export function groupFields(schema) {
  const fields = fieldsFromSchema(schema);
  const byKey = new Map(fields.map((field) => [field.key, field]));
  const used = new Set();

  const groups = FIELD_GROUPS.map((group) => {
    const list = [];
    for (const key of group.keys) {
      if (used.has(key)) continue;
      const field = byKey.get(key);
      if (!field) continue;
      used.add(key);
      list.push(field);
    }
    return { id: group.id, title: group.title, fields: list };
  });

  let other = groups.find((group) => group.id === OTHER_GROUP_ID);
  if (!other) {
    other = { id: OTHER_GROUP_ID, title: "其他", fields: [] };
    groups.push(other);
  }
  for (const field of fields) {
    if (!used.has(field.key)) {
      used.add(field.key);
      other.fields.push(field);
    }
  }
  return groups;
}

/** { key: 深拷贝的 default }（缺省 default 时按类型给安全值）。 */
export function defaultsFromSchema(schema) {
  const out = {};
  for (const { key, node } of fieldsFromSchema(schema)) {
    out[key] = deepClone(nodeDefault(node));
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * template_list 辅助
 * ------------------------------------------------------------------ */

function templatesOf(node) {
  return node && isPlainObject(node.templates) ? node.templates : {};
}

function pickTemplateKey(node, preferred) {
  const templates = templatesOf(node);
  const keys = Object.keys(templates);
  if (preferred && keys.includes(preferred)) return preferred;
  if (preferred && keys.length === 0) return preferred;
  return keys[0] || preferred || "default";
}

function templateOf(node, templateKey) {
  const templates = templatesOf(node);
  const key = pickTemplateKey(node, templateKey);
  const tpl = templates[key];
  return isPlainObject(tpl) ? tpl : null;
}

function templateItemKeys(node, templateKey) {
  const tpl = templateOf(node, templateKey);
  if (!tpl || !isPlainObject(tpl.items)) return [];
  return Object.keys(tpl.items).map((key) => [key, isPlainObject(tpl.items[key]) ? tpl.items[key] : {}]);
}

function normalizeByNode(node, raw, fallback) {
  const type = node ? node.type : undefined;
  switch (type) {
    case "bool": {
      const bool = toBool(raw);
      if (bool !== null) return bool;
      const fb = toBool(fallback);
      return fb === null ? false : fb;
    }
    case "int":
    case "float": {
      const num = toNumber(raw);
      if (num !== null) return num;
      const fb = toNumber(fallback);
      return fb === null ? 0 : fb;
    }
    case "list": {
      const list = toListArray(raw);
      if (list !== null) return list;
      const fb = toListArray(fallback);
      return fb === null ? [] : fb;
    }
    case "template_list": {
      const list = toTemplateArray(node, raw);
      if (list !== null) return list;
      const fb = toTemplateArray(node, fallback);
      return fb === null ? [] : fb;
    }
    case "string":
    case "text":
    default: {
      if (typeof raw === "string") return raw;
      if (raw === undefined || raw === null) return typeof fallback === "string" ? fallback : "";
      if (typeof raw === "object") return typeof fallback === "string" ? fallback : "";
      return String(raw);
    }
  }
}

function toTemplateArray(node, value) {
  if (!Array.isArray(value)) return null;
  return value.map((item) => normalizeTemplateItem(node, item));
}

function normalizeTemplateItem(node, item) {
  const source = isPlainObject(item) ? item : {};
  const templateKey = pickTemplateKey(node, source.__template_key);
  const out = {};
  if (source.__template_key) out.__template_key = String(source.__template_key);
  else if (templateKey) out.__template_key = templateKey;

  const itemKeys = templateItemKeys(node, templateKey);
  if (!itemKeys.length) {
    for (const key of Object.keys(source)) {
      if (key === "__template_key") continue;
      out[key] = deepClone(source[key]);
    }
    return out;
  }
  for (const [key, subNode] of itemKeys) {
    const raw = source[key];
    const fallback = nodeDefault(subNode);
    out[key] = raw === undefined || raw === null ? deepClone(fallback) : normalizeByNode(subNode, raw, fallback);
  }
  return out;
}

/** 新建一条模板条目：带 __template_key 与各子字段默认值。 */
export function newTemplateItem(node, templateKey) {
  const key = pickTemplateKey(node, templateKey);
  const item = { __template_key: key };
  for (const [subKey, subNode] of templateItemKeys(node, key)) {
    item[subKey] = deepClone(nodeDefault(subNode));
  }
  return item;
}

/* ------------------------------------------------------------------ *
 * 草稿 / diff
 * ------------------------------------------------------------------ */

/** schema.secret === true 与后端 secret_fields 的并集。 */
export function secretKeySet(schema, secretFields) {
  const set = new Set();
  for (const key of normalizeIdList(secretFields)) set.add(key);
  for (const { key, node } of fieldsFromSchema(schema)) {
    if (node.secret === true) set.add(key);
  }
  return set;
}

function isBlankSecret(value) {
  if (value === undefined || value === null) return true;
  if (typeof value === "string") return value.trim() === "";
  if (Array.isArray(value)) return value.length === 0;
  return false;
}

/** 表单草稿：secret 字段置空字符串，缺失值回落到 default，list/template_list 深拷贝。 */
export function toDraft(schema, values, secretFields) {
  const secrets = secretKeySet(schema, secretFields);
  const source = isPlainObject(values) ? values : {};
  const draft = {};
  for (const { key, node } of fieldsFromSchema(schema)) {
    if (secrets.has(key)) {
      draft[key] = "";
      continue;
    }
    const raw = hasOwn(source, key) ? source[key] : undefined;
    const fallback = nodeDefault(node);
    draft[key] =
      raw === undefined || raw === null ? deepClone(fallback) : normalizeByNode(node, raw, fallback);
  }
  return draft;
}

/**
 * 只返回「真正变化且通过校验」的 key。
 * - secret 字段：空字符串（未填写）不算变化，填了内容算变化；
 * - 校验失败（WARN_ONLY_KEYS 除外）的字段不会进入 payload。
 */
export function diffValues(schema, draft, original, secretFields) {
  const secrets = secretKeySet(schema, secretFields);
  const current = isPlainObject(draft) ? draft : {};
  const base = isPlainObject(original) ? original : {};
  const out = {};

  for (const { key, node } of fieldsFromSchema(schema)) {
    if (!hasOwn(current, key)) continue;
    const value = current[key];

    if (secrets.has(key)) {
      if (isBlankSecret(value)) continue;
      out[key] = typeof value === "string" ? value : String(value);
      continue;
    }

    if (deepEqual(value, base[key])) continue;
    if (validateValue(node, value) !== null && !WARN_ONLY.has(key)) continue;
    out[key] = deepClone(value);
  }
  return out;
}

/** 与校验无关的「原始变化」key（用于未保存提示与保存按钮可用性）。 */
export function changedKeys(schema, draft, original, secretFields) {
  const secrets = secretKeySet(schema, secretFields);
  const current = isPlainObject(draft) ? draft : {};
  const base = isPlainObject(original) ? original : {};
  const out = [];
  for (const { key } of fieldsFromSchema(schema)) {
    if (!hasOwn(current, key)) continue;
    if (secrets.has(key)) {
      if (!isBlankSecret(current[key])) out.push(key);
      continue;
    }
    if (!deepEqual(current[key], base[key])) out.push(key);
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * 校验
 * ------------------------------------------------------------------ */

function checkSliderRange(node, num) {
  const slider = isPlainObject(node.slider) ? node.slider : null;
  if (!slider) return null;
  const { min, max } = slider;
  if (typeof min === "number" && num < min) return `不能小于 ${min}`;
  if (typeof max === "number" && num > max) return `不能大于 ${max}`;
  return null;
}

function validateTemplateItem(node, item, index) {
  const label = `第 ${index + 1} 条`;
  if (!isPlainObject(item)) return `${label}格式不正确`;
  const templateKey = pickTemplateKey(node, item.__template_key);
  const itemKeys = templateItemKeys(node, templateKey);
  if (!itemKeys.length) return null;
  for (const [key, subNode] of itemKeys) {
    const value = item[key];
    const subName = subNode.description ? `「${subNode.description}」` : "";
    if (value === undefined) {
      if (hasOwn(subNode, "default")) continue;
      return `${label}${subName}缺少内容`;
    }
    const err = validateValue(subNode, value);
    if (err) return `${label}${subName}${err}`;
    // 模板条目内的文本字段默认不允许留空（例如空问题没有意义），
    // 但 schema 里标了 allow_empty 的可选字段（例如该题的额外提示）允许为空。
    if (
      subNode.allow_empty !== true &&
      (subNode.type === "text" || subNode.type === "string") &&
      typeof value === "string" &&
      !value.trim()
    ) {
      return `${label}${subName}不能为空`;
    }
  }
  return null;
}

/** 返回 null（合法）或中文错误文案。 */
export function validateValue(node, value) {
  const current = isPlainObject(node) ? node : {};
  const type = current.type;

  switch (type) {
    case "bool":
      return typeof value === "boolean" ? null : "请选择开启或关闭";
    case "int": {
      const num = toNumber(value);
      if (num === null) return "请输入整数";
      if (!Number.isInteger(num)) return "请输入整数";
      return checkSliderRange(current, num);
    }
    case "float": {
      const num = toNumber(value);
      if (num === null) return "请输入数字";
      return checkSliderRange(current, num);
    }
    case "string": {
      if (typeof value !== "string") return "请输入文本";
      const options = Array.isArray(current.options) ? current.options : [];
      if (options.length && !options.includes(value)) {
        return `只能选择：${options.join(" / ")}`;
      }
      return null;
    }
    case "text":
      return typeof value === "string" ? null : "请输入文本";
    case "list": {
      if (!Array.isArray(value)) return "应为列表";
      for (const item of value) {
        if (typeof item !== "string" || !item.trim()) return "列表项不能为空";
      }
      // 空列表是合法配置（schema 默认值就是 []，后端也只对 review_groups 给 warning）
      return null;
    }
    case "template_list": {
      if (!Array.isArray(value)) return "应为模板列表";
      // 空列表同样合法：清空题库后后端会给出 warning，而不是拒绝保存
      for (let i = 0; i < value.length; i += 1) {
        const err = validateTemplateItem(current, value[i], i);
        if (err) return err;
      }
      return null;
    }
    default:
      return null;
  }
}

/** 该 key 的校验失败是否只是警告（不阻止保存）。 */
export function isWarnOnlyKey(key) {
  return WARN_ONLY.has(key);
}

/* ------------------------------------------------------------------ *
 * 输入解析 / 状态 chips
 * ------------------------------------------------------------------ */

/** 逗号（中英文）、分号（中英文）、顿号、换行分隔；去空、去重、保持顺序。 */
export function parseListInput(text) {
  if (text === undefined || text === null) return [];
  const raw = Array.isArray(text) ? text.map((item) => String(item)) : String(text).split(/[\n\r,，;；、]+/);
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

function textOrDash(value) {
  if (value === undefined || value === null || value === "") return "—";
  return String(value);
}

function countOrDash(value) {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  if (value === undefined || value === null || value === "") return "—";
  return String(value);
}

/** 运行时状态 chips（只读展示）。 */
export function statusChips(status) {
  const data = isPlainObject(status) ? status : {};
  const chips = [];
  const groups = Array.isArray(data.review_groups) ? data.review_groups : [];

  chips.push({ label: "当前验证码", value: textOrDash(data.code), tone: "code" });
  chips.push({
    label: "验证码失效",
    value: textOrDash(data.code_expire_text || data.code_expire_at),
  });
  chips.push({ label: "待审核", value: countOrDash(data.pending) });
  chips.push({ label: "已通过", value: countOrDash(data.approved) });
  chips.push({
    label: "审核群",
    value: groups.length ? `${groups.length} 个` : "—",
    hint: groups.length ? groups.join("、") : "",
  });
  chips.push({ label: "服务器时间", value: textOrDash(data.server_time) });

  if (data.cleanup_time) chips.push({ label: "清理时间", value: String(data.cleanup_time) });
  if (data.code_reset_time) chips.push({ label: "重置时间", value: String(data.code_reset_time) });
  if (data.timezone) chips.push({ label: "时区", value: String(data.timezone) });
  else chips.push({ label: "时区", value: "系统本地" });
  if (Array.isArray(data.platforms) && data.platforms.length) {
    chips.push({ label: "平台", value: data.platforms.join("、") });
  }
  if (data.has_questions === false) {
    chips.push({ label: "题库", value: "未配置", tone: "warn" });
  }
  return chips;
}
