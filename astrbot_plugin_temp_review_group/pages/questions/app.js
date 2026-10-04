/**
 * app.js —— 插件 Pages「questions」（题库管理）的 DOM 与 bridge 交互层。
 *
 * 页面在受限 iframe 中运行，因此只使用 window.AstrBotPluginPage bridge，
 * 不依赖 localStorage / cookie / 父页面，也不引任何第三方库。
 */

import {
  DEFAULT_MATCH_MODE_OPTIONS,
  DEFAULT_PARSE_MAX_CHARS,
  MATCH_MODE_LABELS,
  QUESTION_TEMPLATE_KEY,
  charCount,
  diffBank,
  draftToQuestion,
  matchModeLabel,
  newQuestion,
  normalizeBank,
  normalizeQuestion,
  parseListInput,
  statsText,
  usableCount,
  validateBank,
  validateQuestion,
} from "./bank.js";

const FALLBACK_TITLE = "题库管理";
const FALLBACK_DESC = "展开式问题列表，可写答案，也能让大模型从文本自动填入";
const FALLBACK_ERROR = "请求失败";

/* ------------------------------------------------------------------ *
 * 状态
 * ------------------------------------------------------------------ */

const state = {
  bridge: null,
  ctx: {},
  /** normalizeBank 的结果：全局匹配方式、模型可用性、上限等元信息 */
  bank: null,
  /** 保存基线（用于判断未保存改动） */
  baseline: { questions: [], commonAnswers: [] },
  /** 当前编辑中的题库 */
  draft: { questions: [], commonAnswers: [] },
  /** 展开中的题目 uid 集合 */
  expanded: new Set(),
  /** uid → { q, node, refs } */
  rowRefs: new Map(),
  /** chips 编辑器的 flush 回调（题库区 / 解析草稿区各一组） */
  flushers: [],
  draftFlushers: [],
  uidSeq: 0,
  loading: false,
  saving: false,
  parsing: false,
  dirty: false,
  fatal: false,
  messageTimer: null,
  parse: { drafts: [], provider: "", raw: "", note: "" },
};

const els = {};

/* ------------------------------------------------------------------ *
 * 小工具
 * ------------------------------------------------------------------ */

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = String(value);
    else if (key === "text") node.textContent = String(value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "value") node.value = String(value);
    else if (key === "checked") node.checked = Boolean(value);
    else if (key === "hidden") node.hidden = Boolean(value);
    else if (key === "disabled") node.disabled = Boolean(value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child === undefined || child === null || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * 统一归一化 bridge 返回值。
 * AstrBot 后端返回信封格式 { status: "ok", data: {...} }，SDK 通常已解包成内层 data，
 * 但不同版本行为存在差异，这里再兜底一次，两种形状都能处理。
 */
function unwrap(result) {
  if (result && typeof result === "object" && result.status === "ok" && "data" in result) {
    return result.data;
  }
  return result;
}

/** 防御式错误文案：多行原样保留（消息条 / 状态区都是 pre-wrap）。 */
function errText(err, fallback = FALLBACK_ERROR) {
  let text = "";
  try {
    text = String(err?.message ?? err ?? fallback);
  } catch {
    text = "";
  }
  if (!text.trim() || text === "[object Object]") {
    try {
      const body = err?.payload ?? err?.data;
      const json = body === undefined ? "" : JSON.stringify(body);
      if (json && json !== "{}") text = json;
    } catch {
      text = "";
    }
  }
  return text.trim() ? text : fallback;
}

/** 错误对象里可能夹带模型原始输出（GitHub 风格响应）。 */
function errorRaw(err) {
  const candidates = [
    err?.data?.raw,
    err?.payload?.data?.raw,
    err?.payload?.raw,
    err?.response?.data?.raw,
    err?.raw,
  ];
  for (const item of candidates) {
    if (typeof item === "string" && item.trim()) return item;
  }
  return "";
}

/** 删除类操作的二次确认；iframe 里弹不出模态框时不至于完全点不动。 */
function confirmAction(message) {
  try {
    if (typeof window !== "undefined" && typeof window.confirm === "function") {
      return Boolean(window.confirm(message));
    }
  } catch {
    /* 受限 iframe 可能禁止模态框 */
  }
  return true;
}

/* ------------------------------------------------------------------ *
 * 主题 / i18n
 * ------------------------------------------------------------------ */

function applyContext(ctx) {
  if (isPlainObject(ctx)) state.ctx = Object.assign({}, state.ctx, ctx);
  const dark = Boolean(state.ctx.isDark || state.ctx.theme === "dark");
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function applyI18n() {
  const bridge = state.bridge;
  let title = FALLBACK_TITLE;
  let description = FALLBACK_DESC;
  if (bridge && typeof bridge.t === "function") {
    try {
      const t1 = bridge.t("pages.questions.title", FALLBACK_TITLE);
      const t2 = bridge.t("pages.questions.description", FALLBACK_DESC);
      if (typeof t1 === "string" && t1.trim()) title = t1;
      if (typeof t2 === "string" && t2.trim()) description = t2;
    } catch {
      /* 使用默认文案 */
    }
  }
  els.title.textContent = title;
  els.desc.textContent = description;
  document.title = title;
}

/* ------------------------------------------------------------------ *
 * 消息条 / 解析状态区
 * ------------------------------------------------------------------ */

function showMessage(kind, text, options = {}) {
  const bar = els.messageBar;
  if (!bar) return;
  if (state.messageTimer) {
    clearTimeout(state.messageTimer);
    state.messageTimer = null;
  }
  bar.className = `message-bar message-${kind}`;
  bar.textContent = String(text ?? "");
  bar.hidden = false;
  if (options.autoHide) {
    state.messageTimer = setTimeout(() => {
      bar.hidden = true;
      state.messageTimer = null;
    }, 6000);
  }
}

function clearMessage() {
  if (!els.messageBar) return;
  if (state.messageTimer) {
    clearTimeout(state.messageTimer);
    state.messageTimer = null;
  }
  els.messageBar.hidden = true;
  els.messageBar.textContent = "";
}

function setParseStatus(kind, text) {
  const node = els.parseStatus;
  if (!node) return;
  if (!text) {
    node.hidden = true;
    node.textContent = "";
    node.className = "parse-status";
    return;
  }
  node.hidden = false;
  node.className = `parse-status ${kind === "error" ? "is-error" : "is-info"}`;
  node.textContent = String(text);
}

/* ------------------------------------------------------------------ *
 * flush 注册表
 * ------------------------------------------------------------------ */

function registerFlusher(fn, group = "bank") {
  if (group === "draft") state.draftFlushers.push(fn);
  else state.flushers.push(fn);
}

function flushGroup(list) {
  for (const fn of list.slice()) {
    try {
      fn();
    } catch {
      /* 单个控件 flush 失败不影响其他控件 */
    }
  }
}

function flushAll() {
  // 注意：不清空注册表 —— 控件仍在 DOM 上，重建 DOM 时才会重置。
  flushGroup(state.flushers);
  flushGroup(state.draftFlushers);
}

/* ------------------------------------------------------------------ *
 * chips 编辑器
 * ------------------------------------------------------------------ */

function buildChipsEditor(options) {
  const { inputId, describedBy, placeholder, get, set, onChange, group = "bank" } = options;
  const list = el("div", { class: "chips", role: "list" });
  const input = el("input", {
    type: "text",
    id: inputId,
    class: "input chips-input",
    autocomplete: "off",
    spellcheck: "false",
    placeholder: placeholder || "输入后回车 / 逗号 / 顿号添加",
  });
  if (describedBy) input.setAttribute("aria-describedby", describedBy);
  const wrap = el("div", { class: "chips-editor" }, [list, input]);
  wrap.addEventListener("click", (event) => {
    if (event.target === wrap) input.focus();
  });

  const read = () => {
    const value = get();
    return Array.isArray(value) ? value.slice() : [];
  };

  const renderChips = () => {
    list.textContent = "";
    const items = read();
    items.forEach((item, index) => {
      const remove = el("button", {
        type: "button",
        class: "chip-remove",
        text: "×",
        "aria-label": `删除 ${item}`,
      });
      remove.addEventListener("click", () => {
        const next = read();
        next.splice(index, 1);
        set(next);
        renderChips();
        onChange();
      });
      list.appendChild(
        el("span", { class: "chip chip-editable", role: "listitem" }, [
          el("span", { class: "chip-text", text: item }),
          remove,
        ]),
      );
    });
    if (!items.length) list.appendChild(el("span", { class: "chips-empty", text: "暂无内容" }));
  };

  const commit = (text) => {
    const parsed = parseListInput(text);
    if (!parsed.length) return false;
    const next = read();
    const seen = new Set(next);
    let added = false;
    for (const item of parsed) {
      if (seen.has(item)) continue;
      seen.add(item);
      next.push(item);
      added = true;
    }
    if (!added) return false;
    set(next);
    renderChips();
    onChange();
    return true;
  };

  input.addEventListener("keydown", (event) => {
    const key = event.key;
    if (key === "Enter" || key === "," || key === "，" || key === ";" || key === "；" || key === "、") {
      if (key !== "Enter" || input.value.trim()) event.preventDefault();
      if (input.value.trim()) {
        commit(input.value);
        input.value = "";
      }
    } else if (key === "Backspace" && !input.value) {
      const next = read();
      if (!next.length) return;
      next.pop();
      set(next);
      renderChips();
      onChange();
    }
  });

  input.addEventListener("blur", () => {
    if (input.value.trim()) {
      commit(input.value);
      input.value = "";
    }
  });

  input.addEventListener("paste", (event) => {
    const text = event.clipboardData ? event.clipboardData.getData("text") : "";
    if (!text || !/[\n\r,，;；、]/.test(text)) return;
    event.preventDefault();
    commit(`${input.value}\n${text}`);
    input.value = "";
  });

  const flush = () => {
    if (input.value.trim()) {
      commit(input.value);
      input.value = "";
    }
  };
  registerFlusher(flush, group);

  renderChips();
  return wrap;
}

/** 题库数据辅助
 * ------------------------------------------------------------------ */

function cloneQuestion(q) {
  const next = newQuestion();
  next.__template_key = q && q.__template_key ? String(q.__template_key) : QUESTION_TEMPLATE_KEY;
  next.enabled = Boolean(q && q.enabled);
  next.question = String((q && q.question) ?? "");
  next.hint = String((q && q.hint) ?? "");
  next.answers = Array.isArray(q && q.answers) ? q.answers.slice() : [];
  next.match_mode = q && q.match_mode ? String(q.match_mode) : "inherit";
  next.picks = Number(q && q.picks) || 0;
  next.passes = Number(q && q.passes) || 0;
  next.usable = Boolean(q && q.usable === true);
  return next;
}

function ensureUid(q) {
  if (!q || typeof q !== "object") return "";
  if (typeof q.__uid !== "string" || !q.__uid) {
    state.uidSeq += 1;
    q.__uid = `q${state.uidSeq}`;
  }
  return q.__uid;
}

function assignUids(list, previous) {
  const prev = Array.isArray(previous) ? previous : [];
  list.forEach((q, index) => {
    const old = prev[index];
    if (old && typeof old.__uid === "string" && old.__uid) q.__uid = old.__uid;
    else ensureUid(q);
  });
}

/** 可用性口径与后端 _question_usable 一致：启用 + 有题干 +（有该题答案 或 有通用答案）。 */
function recomputeUsable(draft) {
  const hasCommon = Array.isArray(draft.commonAnswers) && draft.commonAnswers.length > 0;
  for (const q of draft.questions) {
    const hasQuestion = String(q.question || "").trim() !== "";
    const hasAnswers = Array.isArray(q.answers) && q.answers.length > 0;
    q.usable = Boolean(q.enabled) && hasQuestion && (hasAnswers || hasCommon);
  }
}

function bankShape(draft, options) {
  return {
    questions: draft.questions,
    common_answers: draft.commonAnswers,
    match_mode_options: Array.isArray(options) && options.length
      ? options
      : state.bank
        ? state.bank.matchModeOptions
        : DEFAULT_MATCH_MODE_OPTIONS,
    match_mode: state.bank ? state.bank.matchMode : "contains",
  };
}

function currentOptions() {
  if (state.bank && Array.isArray(state.bank.matchModeOptions) && state.bank.matchModeOptions.length) {
    return state.bank.matchModeOptions.slice();
  }
  return DEFAULT_MATCH_MODE_OPTIONS.slice();
}

function payloadQuestions() {
  return state.draft.questions.map((q) => ({
    __template_key: q.__template_key || QUESTION_TEMPLATE_KEY,
    enabled: Boolean(q.enabled),
    question: String(q.question ?? ""),
    hint: String(q.hint ?? ""),
    answers: Array.isArray(q.answers) ? q.answers.slice() : [],
    match_mode: q.match_mode || "inherit",
    picks: Number(q.picks) || 0,
    passes: Number(q.passes) || 0,
    usable: q.usable === true,
  }));
}

/* ------------------------------------------------------------------ *
 * 顶栏 / 状态 chips
 * ------------------------------------------------------------------ */

function renderHeader() {
  const meta = state.bank && isPlainObject(state.bank.meta) ? state.bank.meta : {};
  const version = meta.version ? String(meta.version) : "";
  els.version.textContent = version;
  els.version.hidden = !version;
  els.version.title = meta.display_name ? String(meta.display_name) : "";
}

function reviewModeLabel(mode) {
  const key = String(mode || "").trim().toLowerCase();
  if (key === "rule") return "规则";
  if (key === "llm") return "大模型";
  if (key === "rule-fallback") return "规则（模型不可用回退）";
  return key || "—";
}

function renderStatus() {
  const bar = els.statusBar;
  if (!bar) return;
  bar.textContent = "";
  const total = state.draft.questions.length;
  const usable = usableCount(state.draft);
  const provider = state.bank && state.bank.llmProvider ? state.bank.llmProvider : "";
  const reviewMode = state.bank ? state.bank.reviewMode : "";

  const chips = [
    { label: "题目总数", value: String(total), tone: "" },
    {
      label: "可用题目",
      value: String(usable),
      tone: usable ? "ok" : "warn",
      hint: usable ? "会被抽中的题目条数" : "可用题目为 0 时新人入群不会收到问题",
    },
    { label: "通用答案", value: String(state.draft.commonAnswers.length), tone: "" },
    {
      label: "审核判定",
      value: reviewMode || "—",
      tone: reviewMode && reviewMode !== "rule" ? "warn" : "",
      hint: reviewModeLabel(reviewMode),
    },
    {
      label: "解析模型",
      value: provider || "未配置",
      tone: provider ? "" : "warn",
      hint: provider ? "用于「让大模型解析」的模型" : "没有可用的对话模型，自动填入不可用",
    },
  ];

  for (const chip of chips) {
    const node = el("span", { class: `chip${chip.tone ? ` chip-${chip.tone}` : ""}` });
    node.appendChild(el("span", { class: "chip-label", text: chip.label }));
    node.appendChild(el("span", { class: "chip-value", text: chip.value }));
    if (chip.hint) node.title = String(chip.hint);
    bar.appendChild(node);
  }
}

function syncQuestionCounts() {
  const total = state.draft.questions.length;
  const usable = usableCount(state.draft);
  els.questionsCount.textContent = total ? `共 ${total} 条 · 可用 ${usable} 条` : "暂无题目";
  els.commonCount.textContent = `共 ${state.draft.commonAnswers.length} 条`;
}

/* ------------------------------------------------------------------ *
 * 未保存改动 / 工具条
 * ------------------------------------------------------------------ */

function refreshDirty() {
  const options = currentOptions();
  state.dirty = diffBank(bankShape(state.draft, options), bankShape(state.baseline, options));
  els.dirtyHint.hidden = !state.dirty;
  els.dirtyHint.textContent = state.dirty ? "有未保存的改动" : "";
  updateToolbar();
}

function updateToolbar() {
  const busy = Boolean(state.saving || state.loading);
  if (!state.fatal) els.app.dataset.state = busy ? "busy" : "ready";
  els.btnSave.disabled = busy || !state.dirty;
  els.btnReload.disabled = busy;
  els.btnAdd.disabled = busy;
  const count = state.draft.questions.length;
  els.btnExpandAll.disabled = busy || count === 0;
  els.btnCollapseAll.disabled = busy || count === 0;
}

/** 任何编辑之后的统一刷新：可用性、行摘要、计数、chips、保存按钮。 */
function refreshLive() {
  recomputeUsable(state.draft);
  for (const uid of Array.from(state.rowRefs.keys())) syncRow(uid);
  syncQuestionCounts();
  renderStatus();
  refreshDirty();
}

/* ------------------------------------------------------------------ *
 * 问题列表
 * ------------------------------------------------------------------ */

function modeOptionLabel(mode) {
  const globalMode = state.bank ? state.bank.matchMode : "contains";
  if (mode === "inherit") return `跟随全局（${MATCH_MODE_LABELS[globalMode] || globalMode}）`;
  return MATCH_MODE_LABELS[mode] || mode;
}

function buildModeSelect(current, controlId, describedBy, onChange) {
  const select = el("select", { id: controlId, class: "input input-select" });
  if (describedBy) select.setAttribute("aria-describedby", describedBy);
  const seen = new Set();
  for (const mode of currentOptions()) {
    if (seen.has(mode)) continue;
    seen.add(mode);
    select.appendChild(el("option", { value: mode, text: modeOptionLabel(mode) }));
  }
  const value = String(current || "inherit").toLowerCase();
  if (!seen.has(value)) {
    // 配置里存着当前不在选项里的值：保留它，避免用户一打开页面就把配置改掉
    select.appendChild(el("option", { value, text: `${value}（当前不可选）` }));
  }
  select.value = value;
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

function buildQuestionRow(q, index) {
  const uid = ensureUid(q);
  const open = state.expanded.has(uid);
  const toggleId = `qtoggle-${uid}`;
  const bodyId = `qbody-${uid}`;

  const row = el("article", { class: "qrow", dataset: { uid } });
  const head = el("div", { class: "qrow-head" });

  head.appendChild(el("span", { class: "q-index", text: `#${index + 1}` }));
  const enabledBadge = el("span", { class: "badge" });
  head.appendChild(enabledBadge);
  const title = el("span", { class: "q-title" });
  head.appendChild(title);
  const modeBadge = el("span", { class: "badge" });
  head.appendChild(modeBadge);
  const stats = el("span", { class: "q-stats" });
  head.appendChild(stats);
  const unusable = el("span", {
    class: "badge badge-warn",
    text: "不会被抽中",
    title: "需要启用、填好题干，并至少有一个该题答案或通用答案",
  });
  head.appendChild(unusable);

  const actions = el("div", { class: "q-actions" });
  const toggle = el("button", {
    type: "button",
    id: toggleId,
    class: "btn btn-small",
    "aria-expanded": open ? "true" : "false",
    "aria-controls": bodyId,
    text: open ? "收起" : "展开",
  });
  toggle.addEventListener("click", () => setRowOpen(uid, !state.expanded.has(uid)));
  const remove = el("button", {
    type: "button",
    class: "btn btn-small btn-danger",
    text: "删除",
    "aria-label": `删除第 ${index + 1} 题`,
  });
  remove.addEventListener("click", () => deleteQuestion(uid));
  actions.appendChild(toggle);
  actions.appendChild(remove);
  head.appendChild(actions);
  row.appendChild(head);

  const body = el("div", { class: "qrow-body", id: bodyId, role: "region", "aria-labelledby": toggleId });
  const fields = el("div", { class: "qrow-fields" });

  // 题干
  const questionId = `q-question-${uid}`;
  const questionHintId = `q-question-hint-${uid}`;
  const questionArea = el("textarea", {
    id: questionId,
    class: "input input-textarea",
    rows: "2",
    spellcheck: "false",
    value: q.question,
    "aria-describedby": questionHintId,
  });
  questionArea.addEventListener("input", () => {
    q.question = questionArea.value;
    refreshLive();
  });
  fields.appendChild(
    buildField({
      label: "题干",
      controlId: questionId,
      control: questionArea,
      hintId: questionHintId,
      hint: "入群提问的正文，会原样发给成员。",
      wide: true,
    }),
  );

  // 该题提示
  const hintId = `q-hint-${uid}`;
  const hintHintId = `q-hint-hint-${uid}`;
  const hintInput = el("input", {
    type: "text",
    id: hintId,
    class: "input",
    autocomplete: "off",
    spellcheck: "false",
    value: q.hint,
    "aria-describedby": hintHintId,
  });
  hintInput.addEventListener("input", () => {
    q.hint = hintInput.value;
    refreshDirty();
  });
  fields.appendChild(
    buildField({
      label: "该题提示",
      controlId: hintId,
      control: hintInput,
      hintId: hintHintId,
      hint: "附在提问后面发给成员，可留空。",
    }),
  );

  // 答案库
  const answersId = `q-answers-${uid}`;
  const answersHintId = `q-answers-hint-${uid}`;
  const answersEditor = buildChipsEditor({
    inputId: answersId,
    describedBy: answersHintId,
    placeholder: "输入答案后回车 / 逗号 / 顿号添加",
    get: () => q.answers,
    set: (next) => {
      q.answers = next;
    },
    onChange: () => refreshLive(),
  });
  fields.appendChild(
    buildField({
      label: "该题答案库",
      controlId: answersId,
      control: answersEditor,
      hintId: answersHintId,
      hint: "任意一条命中即通过；留空则依赖「通用答案库」。",
      wide: true,
    }),
  );

  // 匹配方式
  const modeId = `q-mode-${uid}`;
  const modeHintId = `q-mode-hint-${uid}`;
  const modeSelect = buildModeSelect(q.match_mode, modeId, modeHintId, (value) => {
    q.match_mode = value;
    refreshLive();
  });
  fields.appendChild(
    buildField({
      label: "匹配方式",
      controlId: modeId,
      control: modeSelect,
      hintId: modeHintId,
      hint: "「跟随全局」使用插件配置里的全局匹配方式。",
    }),
  );

  // 启用开关
  const enabledId = `q-enabled-${uid}`;
  const switchInput = el("input", { type: "checkbox", id: enabledId, class: "switch-input", checked: q.enabled });
  const switchText = el("span", { class: "switch-text" });
  switchInput.addEventListener("change", () => {
    q.enabled = switchInput.checked;
    refreshLive();
  });
  const switchWrap = el("div", { class: "field" }, [
    el("span", { class: "field-label", text: "启用状态" }),
    el("label", { class: "switch", for: enabledId }, [
      switchInput,
      el("span", { class: "switch-track", "aria-hidden": "true" }),
      switchText,
    ]),
    el("p", { class: "field-hint", text: "关掉后不会被抽中，配置会保留。" }),
  ]);
  fields.appendChild(switchWrap);

  body.appendChild(fields);
  body.hidden = !open;
  row.classList.toggle("is-open", open);
  row.appendChild(body);

  state.rowRefs.set(uid, {
    q,
    node: row,
    refs: { body, toggle, title, enabledBadge, modeBadge, stats, unusable, switchInput, switchText },
  });
  syncRow(uid);
  return row;
}

function buildField(options) {
  const wrap = el("div", { class: options.wide ? "field field-wide" : "field" });
  wrap.appendChild(el("label", { class: "field-label", for: options.controlId, text: options.label }));
  wrap.appendChild(options.control);
  if (options.hint) {
    wrap.appendChild(el("p", { class: "field-hint", id: options.hintId, text: options.hint }));
  }
  return wrap;
}

function syncRow(uid) {
  const entry = state.rowRefs.get(uid);
  if (!entry) return;
  const { q, node, refs } = entry;
  const text = String(q.question || "").trim();
  refs.title.textContent = text || "（未填写题干）";
  refs.title.classList.toggle("is-empty", !text);
  refs.title.title = text;
  refs.enabledBadge.textContent = q.enabled ? "启用" : "已停用";
  refs.enabledBadge.className = `badge ${q.enabled ? "badge-on" : "badge-off"}`;
  refs.modeBadge.textContent = matchModeLabel(q.match_mode, state.bank ? state.bank.matchMode : "contains");
  refs.stats.textContent = statsText(q);
  refs.unusable.hidden = q.usable === true;
  refs.switchInput.checked = Boolean(q.enabled);
  refs.switchText.textContent = q.enabled ? "启用该题" : "已停用";
  node.classList.toggle("is-disabled", !q.enabled);
  node.classList.toggle("is-unusable", q.usable !== true);
}

function setRowOpen(uid, open) {
  if (open) state.expanded.add(uid);
  else state.expanded.delete(uid);
  const entry = state.rowRefs.get(uid);
  if (!entry) return;
  entry.refs.body.hidden = !open;
  entry.refs.toggle.textContent = open ? "收起" : "展开";
  entry.refs.toggle.setAttribute("aria-expanded", open ? "true" : "false");
  entry.node.classList.toggle("is-open", open);
}

/* ------------------------------------------------------------------ *
 * 题库区渲染（问题列表 + 通用答案库）
 * ------------------------------------------------------------------ */

function renderBankSections() {
  flushAll();
  state.flushers = [];
  state.rowRefs.clear();

  const listEl = els.questionList;
  listEl.textContent = "";
  if (!state.draft.questions.length) {
    listEl.appendChild(
      el("div", {
        class: "empty-state",
        text: "题库还是空的：点「新增题目」，或在下方让大模型从一段文本里自动整理。",
      }),
    );
  } else {
    state.draft.questions.forEach((q, index) => {
      listEl.appendChild(buildQuestionRow(q, index));
    });
  }

  const commonWrap = els.commonEditor;
  commonWrap.textContent = "";
  commonWrap.appendChild(
    buildChipsEditor({
      inputId: "common-answers-input",
      describedBy: "common-hint",
      placeholder: "输入邀请码 / 口令后回车添加",
      get: () => state.draft.commonAnswers,
      set: (next) => {
        state.draft.commonAnswers = next;
      },
      onChange: () => refreshLive(),
      group: "bank",
    }),
  );

  refreshLive();
}

/* ------------------------------------------------------------------ *
 * 题目增删 / 展开收起
 * ------------------------------------------------------------------ */

function addQuestion() {
  flushAll();
  const q = newQuestion();
  state.draft.questions.push(q);
  const uid = ensureUid(q);
  state.expanded.add(uid);
  renderBankSections();
  const entry = state.rowRefs.get(uid);
  if (entry) {
    const area = entry.node.querySelector(`#q-question-${uid}`);
    if (area && typeof area.focus === "function") area.focus();
    if (typeof entry.node.scrollIntoView === "function") entry.node.scrollIntoView({ block: "nearest" });
  }
  showMessage("info", "已新增一条题目（默认展开），填好后别忘了点「保存题库」。", { autoHide: true });
}

function deleteQuestion(uid) {
  const index = state.draft.questions.findIndex((q) => ensureUid(q) === uid);
  if (index < 0) return;
  const raw = String(state.draft.questions[index].question || "").trim();
  const label = raw || `第 ${index + 1} 题（未填写题干）`;
  const ok = confirmAction(`确定删除「${label}」吗？\n删除后要点「保存题库」才会写入配置。`);
  if (!ok) {
    showMessage("info", "已取消删除。", { autoHide: true });
    return;
  }
  state.draft.questions.splice(index, 1);
  state.expanded.delete(uid);
  renderBankSections();
  showMessage("info", "已删除该题目，别忘了点「保存题库」。", { autoHide: true });
}

function expandAll(open) {
  flushAll();
  state.expanded.clear();
  if (open) {
    for (const q of state.draft.questions) state.expanded.add(ensureUid(q));
  }
  renderBankSections();
}

function focusQuestion(index) {
  const q = state.draft.questions[index];
  if (!q) return;
  const uid = ensureUid(q);
  setRowOpen(uid, true);
  const entry = state.rowRefs.get(uid);
  if (!entry) return;
  if (typeof entry.node.scrollIntoView === "function") {
    entry.node.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  const area = entry.node.querySelector(`#q-question-${uid}`);
  if (area && typeof area.focus === "function") area.focus();
}

/* ------------------------------------------------------------------ *
 * 加载 / 保存
 * ------------------------------------------------------------------ */

function applyBank(data, options = {}) {
  const bank = normalizeBank(data);
  const previous = Array.isArray(state.draft.questions) ? state.draft.questions : [];
  const questions = bank.questions.map((q) => cloneQuestion(q));
  assignUids(questions, previous);

  state.bank = bank;
  state.fatal = false;
  state.baseline = {
    questions: bank.questions.map((q) => cloneQuestion(q)),
    commonAnswers: bank.commonAnswers.slice(),
  };
  state.draft = {
    questions,
    commonAnswers: bank.commonAnswers.slice(),
  };
  if (!options.keepExpanded) state.expanded.clear();
  recomputeUsable(state.draft);
  renderAll();
}

function renderAll() {
  renderHeader();
  renderBankSections();
  renderParseDrafts();
  updateParseButton();
}

async function loadBank(options = {}) {
  const bridge = state.bridge;
  if (!bridge || typeof bridge.apiGet !== "function") {
    renderFatal("当前环境不支持 AstrBot 插件页面 bridge（缺少 apiGet）。");
    return;
  }
  state.loading = true;
  updateToolbar();
  try {
    const data = unwrap(await bridge.apiGet("questions"));
    if (!isPlainObject(data) || !Array.isArray(data.questions)) {
      renderFormatError("响应格式异常：后端返回的数据里没有 questions 数组，无法渲染题库。");
      return;
    }
    applyBank(data);
    if (options.announce) showMessage("info", "已重新加载题库，未保存的改动已丢弃。", { autoHide: true });
    else clearMessage();
  } catch (err) {
    clearMessage();
    showMessage("error", `读取题库失败：${errText(err, "读取题库失败")}`);
    renderErrorCard("读取题库失败。", errText(err, "读取题库失败"));
  } finally {
    state.loading = false;
    updateToolbar();
  }
}

async function saveBank() {
  const bridge = state.bridge;
  if (!bridge || typeof bridge.apiPost !== "function") {
    showMessage("error", "当前环境不支持 AstrBot 插件页面 bridge（缺少 apiPost）。");
    return;
  }
  if (state.saving || state.loading) return;

  flushAll();
  recomputeUsable(state.draft);

  const options = currentOptions();
  const report = validateBank(bankShape(state.draft, options));
  if (report.errors.length) {
    const firstIndex = state.draft.questions.findIndex((q) => validateQuestion(q, options) !== null);
    if (firstIndex >= 0) focusQuestion(firstIndex);
    const lines = [`有 ${report.errors.length} 处需要修正：`];
    for (const item of report.errors) lines.push(`· ${item}`);
    showMessage("error", lines.join("\n"));
    return;
  }

  state.saving = true;
  updateToolbar();
  try {
    const response = unwrap(
      await bridge.apiPost("questions", {
        questions: payloadQuestions(),
        common_answers: state.draft.commonAnswers.slice(),
      }),
    );
    if (!isPlainObject(response) || !Array.isArray(response.questions)) {
      showMessage(
        "error",
        "响应格式异常：保存结果里没有 questions 列表，请点「重新加载」确认配置是否已生效。",
      );
      return;
    }

    const saved = Number.isFinite(Number(response.saved))
      ? Number(response.saved)
      : state.draft.questions.length;
    applyBank(response, { keepExpanded: true });

    const warnings = Array.isArray(response.warnings)
      ? response.warnings.map((item) => String(item)).filter((item) => item.trim())
      : [];
    if (warnings.length) {
      // warnings 非空不算失败：基准值已经刷新，未保存标记也已清掉
      const lines = [`已保存 ${saved} 条题目，但有以下提示：`];
      for (const item of warnings) lines.push(`· ${item}`);
      showMessage("warning", lines.join("\n"));
    } else {
      showMessage("success", `已保存 ${saved} 条题目`, { autoHide: true });
    }
  } catch (err) {
    showMessage("error", `保存失败：${errText(err, "保存失败")}`);
  } finally {
    state.saving = false;
    updateToolbar();
  }
}

/* ------------------------------------------------------------------ *
 * 大模型自动填入
 * ------------------------------------------------------------------ */

function updateParseButton() {
  const bank = state.bank;
  const max = bank ? bank.parseMaxChars : DEFAULT_PARSE_MAX_CHARS;
  const limit = bank ? bank.parseLimit : 30;
  const count = charCount(els.parseText.value);
  const over = count > max;

  els.parseCounter.textContent = over
    ? `${count} / ${max} 字（超出 ${count - max} 字，请分段解析）`
    : `${count} / ${max} 字`;
  els.parseCounter.classList.toggle("is-over", over);

  const unavailable = !bank || !bank.llmAvailable;
  els.parseUnavailable.hidden = !unavailable;
  els.parseLimit.textContent = `上限 ${max} 字 · 最多 ${limit} 条`;

  els.btnParse.disabled = Boolean(state.parsing || unavailable || over || !count);
  els.btnParse.textContent = state.parsing ? "解析中…" : "让大模型解析";
  els.btnParse.title = unavailable
    ? "没有可用的对话模型，无法自动填入"
    : over
      ? "文本超过上限，请分段解析"
      : count
        ? ""
        : "请先粘贴要解析的文本";

  const selected = state.parse.drafts.filter((item) => item.checked).length;
  els.btnParseAdd.hidden = state.parse.drafts.length === 0;
  els.btnParseAdd.textContent = `加入题库（${selected} 条）`;
  els.btnParseAdd.disabled = Boolean(state.parsing) || selected === 0;
  els.btnParseClear.hidden = state.parse.drafts.length === 0;
  els.btnParseClear.disabled = Boolean(state.parsing);
}

function setParseRaw(raw) {
  const text = typeof raw === "string" ? raw : "";
  state.parse.raw = text;
  const has = Boolean(text.trim());
  els.parseRawWrap.hidden = !has;
  els.parseRaw.textContent = has ? text : "";
  if (!has) {
    els.parseRaw.hidden = true;
    els.btnParseRaw.setAttribute("aria-expanded", "false");
  }
}

function buildDraftCard(entry, index) {
  const card = el("article", { class: "draft-card" });
  const checkId = `draft-check-${index}`;
  const checkbox = el("input", { type: "checkbox", id: checkId, checked: entry.checked === true });
  checkbox.addEventListener("change", () => {
    entry.checked = checkbox.checked;
    updateParseButton();
  });

  const head = el("div", { class: "draft-head" }, [
    el("label", { class: "draft-check", for: checkId }, [
      checkbox,
      el("span", { text: `草稿 #${index + 1}` }),
    ]),
    el("span", { class: "badge", text: "未加入题库" }),
    el("span", {
      class: "badge",
      text: matchModeLabel(entry.item.match_mode, state.bank ? state.bank.matchMode : "contains"),
    }),
  ]);
  card.appendChild(head);

  const grid = el("div", { class: "draft-grid" });

  const questionId = `draft-question-${index}`;
  const questionArea = el("textarea", {
    id: questionId,
    class: "input input-textarea",
    rows: "2",
    spellcheck: "false",
    value: entry.item.question,
  });
  questionArea.addEventListener("input", () => {
    entry.item.question = questionArea.value;
  });
  grid.appendChild(
    buildField({ label: "题干", controlId: questionId, control: questionArea, wide: true }),
  );

  const answersId = `draft-answers-${index}`;
  const answersHintId = `draft-answers-hint-${index}`;
  const answersEditor = buildChipsEditor({
    inputId: answersId,
    describedBy: answersHintId,
    placeholder: "输入答案后回车添加",
    get: () => entry.item.answers,
    set: (next) => {
      entry.item.answers = next;
    },
    onChange: () => {
      /* 草稿不影响题库，无需刷新 */
    },
    group: "draft",
  });
  grid.appendChild(
    buildField({
      label: "答案库",
      controlId: answersId,
      control: answersEditor,
      hintId: answersHintId,
      hint: "任意一条命中即通过。",
      wide: true,
    }),
  );

  const hintId = `draft-hint-${index}`;
  const hintInput = el("input", {
    type: "text",
    id: hintId,
    class: "input",
    autocomplete: "off",
    value: entry.item.hint,
  });
  hintInput.addEventListener("input", () => {
    entry.item.hint = hintInput.value;
  });
  grid.appendChild(buildField({ label: "提示", controlId: hintId, control: hintInput }));

  const modeId = `draft-mode-${index}`;
  const modeSelect = buildModeSelect(entry.item.match_mode, modeId, "", (value) => {
    entry.item.match_mode = value;
    const badge = head.querySelectorAll(".badge")[1];
    if (badge) badge.textContent = matchModeLabel(value, state.bank ? state.bank.matchMode : "contains");
  });
  grid.appendChild(buildField({ label: "匹配方式", controlId: modeId, control: modeSelect }));

  card.appendChild(grid);
  return card;
}

function renderParseDrafts() {
  state.draftFlushers = [];
  const wrap = els.parseDrafts;
  wrap.textContent = "";
  const drafts = state.parse.drafts;
  if (!drafts.length) {
    updateParseButton();
    return;
  }
  wrap.appendChild(
    el("p", {
      class: "draft-note",
      text:
        state.parse.note ||
        "这些只是草稿：确认 / 修改后点「加入题库」追加到列表末尾，再点「保存题库」才会写入配置。",
    }),
  );
  drafts.forEach((entry, index) => wrap.appendChild(buildDraftCard(entry, index)));
  updateParseButton();
}

async function parseWithLlm() {
  const bridge = state.bridge;
  if (!bridge || typeof bridge.apiPost !== "function") {
    setParseStatus("error", "当前环境不支持 AstrBot 插件页面 bridge（缺少 apiPost）。");
    return;
  }
  if (state.parsing) return;

  const text = els.parseText.value;
  if (!text.trim()) {
    setParseStatus("error", "请先粘贴要解析的文本。");
    return;
  }
  const max = state.bank ? state.bank.parseMaxChars : DEFAULT_PARSE_MAX_CHARS;
  const count = charCount(text);
  if (count > max) {
    setParseStatus("error", `文本太长（${count} 字）：一次最多 ${max} 字，请分段解析。`);
    return;
  }
  if (state.bank && !state.bank.llmAvailable) {
    setParseStatus(
      "error",
      "没有可用的对话模型，请先在 AstrBot 配置模型或在插件配置里指定 review_llm_provider。",
    );
    return;
  }

  state.parsing = true;
  setParseRaw("");
  setParseStatus("info", "正在请求大模型解析，请稍候…");
  updateParseButton();
  try {
    const data = unwrap(await bridge.apiPost("questions/parse", { text }));
    if (!isPlainObject(data) || !Array.isArray(data.drafts)) {
      setParseStatus("error", "响应格式异常：解析结果里没有 drafts 草稿列表，请重试或检查后端版本。");
      return;
    }
    const options = currentOptions();
    state.parse.drafts = data.drafts.map((draft) => ({
      checked: true,
      item: normalizeQuestion(draft, options),
    }));
    state.parse.provider = typeof data.provider === "string" ? data.provider : "";
    state.parse.note = typeof data.note === "string" ? data.note : "";
    setParseRaw(typeof data.raw === "string" ? data.raw : "");
    renderParseDrafts();
    const head = state.parse.provider ? `使用的模型：${state.parse.provider}。` : "";
    setParseStatus(
      "info",
      `${head}解析出 ${state.parse.drafts.length} 条题目草稿（默认全部勾选），确认后点「加入题库」。`,
    );
  } catch (err) {
    setParseRaw(errorRaw(err));
    setParseStatus("error", `解析失败：${errText(err, "解析失败")}`);
  } finally {
    state.parsing = false;
    updateParseButton();
  }
}

function clearParseDrafts() {
  state.parse.drafts = [];
  state.parse.provider = "";
  state.parse.note = "";
  setParseRaw("");
  renderParseDrafts();
  setParseStatus("info", "已清空解析结果。");
}

function addSelectedDrafts() {
  flushAll();
  const drafts = state.parse.drafts;
  const selected = drafts.filter((item) => item.checked);
  if (!selected.length) {
    setParseStatus("info", "请先勾选要加入题库的草稿。");
    return;
  }

  const added = [];
  const addedUids = [];
  let skipped = 0;
  for (const entry of selected) {
    const question = draftToQuestion(entry.item);
    if (!String(question.question || "").trim()) {
      skipped += 1;
      continue;
    }
    state.draft.questions.push(question);
    const uid = ensureUid(question);
    state.expanded.add(uid);
    added.push(entry);
    addedUids.push(uid);
  }

  if (!added.length) {
    setParseStatus("error", "选中的草稿都没有题干，无法加入题库：请先补上题干。");
    return;
  }

  state.parse.drafts = drafts.filter((item) => !added.includes(item));
  state.parse.note = "";
  renderBankSections();
  renderParseDrafts();
  updateParseButton();

  const tail = skipped ? `；有 ${skipped} 条草稿没有题干，已跳过` : "";
  setParseStatus(
    "info",
    `已把 ${added.length} 条草稿追加到问题列表末尾（不会自动保存），别忘了点「保存题库」${tail}。`,
  );
  showMessage("info", "已加入列表，别忘了点「保存题库」。", { autoHide: true });

  const entry = state.rowRefs.get(addedUids[0]);
  if (entry && typeof entry.node.scrollIntoView === "function") {
    entry.node.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

/* ------------------------------------------------------------------ *
 * 致命错误卡片
 * ------------------------------------------------------------------ */

function renderFormatError(message) {
  state.fatal = true;
  els.app.dataset.state = "error";
  showMessage("error", message);
  els.statusBar.textContent = "";
  els.statusBar.hidden = true;
  els.questionList.textContent = "";
  els.questionList.appendChild(
    el("div", { class: "fatal" }, [
      el("p", { text: "响应格式异常，无法渲染题库。" }),
      el("p", { class: "field-hint", text: message }),
      el("button", {
        type: "button",
        class: "btn",
        text: "重新加载",
        onclick: () => {
          state.loading = false;
          loadBank({ announce: true });
        },
      }),
    ]),
  );
  syncQuestionCounts();
  updateToolbar();
}

function renderErrorCard(title, detail) {
  state.fatal = true;
  els.app.dataset.state = "error";
  els.questionList.textContent = "";
  els.questionList.appendChild(
    el("div", { class: "fatal" }, [
      el("p", { text: title }),
      el("p", { class: "field-hint", text: detail }),
      el("button", {
        type: "button",
        class: "btn",
        text: "重新加载",
        onclick: () => {
          state.loading = false;
          loadBank({ announce: true });
        },
      }),
    ]),
  );
  syncQuestionCounts();
  updateToolbar();
}

function renderFatal(message) {
  state.fatal = true;
  els.app.dataset.state = "error";
  showMessage("error", message);
  els.statusBar.textContent = "";
  els.statusBar.hidden = true;
  els.toolbar.hidden = true;
  els.main.textContent = "";
  els.main.appendChild(
    el("div", { class: "fatal" }, [
      el("p", { text: "无法在此环境中初始化题库页面。" }),
      el("p", { class: "field-hint", text: message }),
      el("button", {
        type: "button",
        class: "btn",
        text: "重试",
        onclick: () => window.location.reload(),
      }),
    ]),
  );
  state.loading = false;
}

/* ------------------------------------------------------------------ *
 * 启动
 * ------------------------------------------------------------------ */

function cacheElements() {
  els.app = document.getElementById("app");
  els.title = document.getElementById("page-title");
  els.desc = document.getElementById("page-desc");
  els.version = document.getElementById("plugin-version");
  els.statusBar = document.getElementById("status-bar");
  els.messageBar = document.getElementById("message-bar");
  els.toolbar = document.getElementById("toolbar");
  els.main = document.getElementById("main");
  els.dirtyHint = document.getElementById("dirty-hint");
  els.btnAdd = document.getElementById("btn-add");
  els.btnExpandAll = document.getElementById("btn-expand-all");
  els.btnCollapseAll = document.getElementById("btn-collapse-all");
  els.btnReload = document.getElementById("btn-reload");
  els.btnSave = document.getElementById("btn-save");
  els.questionList = document.getElementById("question-list");
  els.questionsCount = document.getElementById("questions-count");
  els.commonEditor = document.getElementById("common-editor");
  els.commonCount = document.getElementById("common-count");
  els.parseText = document.getElementById("parse-text");
  els.parseCounter = document.getElementById("parse-counter");
  els.parseUnavailable = document.getElementById("parse-unavailable");
  els.btnParse = document.getElementById("btn-parse");
  els.btnParseAdd = document.getElementById("btn-parse-add");
  els.btnParseClear = document.getElementById("btn-parse-clear");
  els.parseStatus = document.getElementById("parse-status");
  els.parseRawWrap = document.getElementById("parse-raw-wrap");
  els.btnParseRaw = document.getElementById("btn-parse-raw");
  els.parseRaw = document.getElementById("parse-raw");
  els.parseDrafts = document.getElementById("parse-drafts");
  els.parseLimit = document.getElementById("parse-limit");
}

function bindEvents() {
  els.btnSave.addEventListener("click", () => {
    saveBank();
  });
  els.btnReload.addEventListener("click", () => {
    flushAll();
    loadBank({ announce: true });
  });
  els.btnAdd.addEventListener("click", () => addQuestion());
  els.btnExpandAll.addEventListener("click", () => expandAll(true));
  els.btnCollapseAll.addEventListener("click", () => expandAll(false));

  els.parseText.addEventListener("input", () => updateParseButton());
  els.btnParse.addEventListener("click", () => {
    parseWithLlm();
  });
  els.btnParseClear.addEventListener("click", () => clearParseDrafts());
  els.btnParseAdd.addEventListener("click", () => addSelectedDrafts());
  els.btnParseRaw.addEventListener("click", () => {
    const open = els.parseRaw.hidden;
    els.parseRaw.hidden = !open;
    els.btnParseRaw.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

async function boot() {
  cacheElements();
  bindEvents();

  const bridge = window.AstrBotPluginPage;
  if (!bridge || typeof bridge !== "object") {
    renderFatal("未检测到 window.AstrBotPluginPage，请从 AstrBot Dashboard 的插件页面入口打开本页。");
    return;
  }
  state.bridge = bridge;

  try {
    const ctx = typeof bridge.ready === "function" ? await bridge.ready() : {};
    applyContext(ctx);
  } catch (err) {
    renderFatal(`bridge.ready() 失败：${errText(err, "初始化失败")}`);
    return;
  }

  applyI18n();

  if (typeof bridge.getContext === "function") {
    try {
      applyContext(bridge.getContext());
    } catch {
      /* 忽略上下文获取失败 */
    }
  }
  if (typeof bridge.onContext === "function") {
    try {
      bridge.onContext((next) => applyContext(next));
    } catch {
      /* 忽略订阅失败 */
    }
  }

  updateParseButton();
  await loadBank();
}

boot().catch((err) => {
  try {
    renderFatal(`题库页面初始化失败：${errText(err, "未知错误")}`);
  } catch {
    /* 兜底失败时不再抛异常，避免白屏 */
  }
});
