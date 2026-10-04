/**
 * app.js —— 插件 Pages「settings」的 DOM 与 bridge 交互层。
 *
 * 页面在受限 iframe 中运行（allow-scripts allow-forms allow-downloads），
 * 因此只使用 window.AstrBotPluginPage bridge，不依赖 localStorage / cookie / 父页面。
 */

import {
  fieldsFromSchema,
  groupFields,
  defaultsFromSchema,
  toDraft,
  diffValues,
  changedKeys,
  validateValue,
  parseListInput,
  newTemplateItem,
  statusChips,
  isWarnOnlyKey,
} from "./settings.js";

const FALLBACK_TITLE = "审核群设置";
const FALLBACK_DESC = "查看并修改临时审核群插件的全部配置";
const FALLBACK_SAVE_ERROR = "保存失败";

/* ------------------------------------------------------------------ *
 * 状态
 * ------------------------------------------------------------------ */

const state = {
  bridge: null,
  schema: {},
  values: {},
  secretFields: [],
  draft: {},
  meta: {},
  status: null,
  providers: [],
  loading: false,
  saving: false,
  offContext: null,
  fieldWraps: new Map(),
  flushers: [],
  messageTimer: null,
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

function errText(err, fallback = FALLBACK_SAVE_ERROR) {
  if (err === undefined || err === null) return fallback;
  if (typeof err === "string") return err.trim() || fallback;
  if (typeof err.message === "string" && err.message.trim()) return err.message;
  if (typeof err === "object") {
    try {
      const text = JSON.stringify(err);
      if (text && text !== "{}") return text;
    } catch {
      /* ignore */
    }
    return fallback;
  }
  try {
    return String(err);
  } catch {
    return fallback;
  }
}

function setProgress(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

/* ------------------------------------------------------------------ *
 * 主题 / i18n
 * ------------------------------------------------------------------ */

function applyContext(ctx) {
  if (ctx && typeof ctx === "object") state.ctx = Object.assign({}, state.ctx, ctx);
  const dark = Boolean(state.ctx && (state.ctx.isDark || state.ctx.theme === "dark"));
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

function applyI18n() {
  const bridge = state.bridge;
  let title = FALLBACK_TITLE;
  let description = FALLBACK_DESC;
  if (bridge && typeof bridge.t === "function") {
    try {
      const t1 = bridge.t("pages.settings.title", FALLBACK_TITLE);
      const t2 = bridge.t("pages.settings.description", FALLBACK_DESC);
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
 * 消息条
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

/* ------------------------------------------------------------------ *
 * 字段控件
 * ------------------------------------------------------------------ */

function registerFlusher(fn) {
  state.flushers.push(fn);
}

function flushAll() {
  // 注意：不要清空 state.flushers —— 控件仍留在 DOM 上，
  // renderGroups() 重新渲染时才会重建该注册表。
  for (const fn of state.flushers.slice()) {
    try {
      fn();
    } catch {
      /* 忽略单个控件的 flush 异常 */
    }
  }
}

function buildSwitch(binding, describedBy) {
  const input = el("input", { type: "checkbox", id: binding.id, class: "switch-input", checked: Boolean(binding.get()) });
  if (describedBy) input.setAttribute("aria-describedby", describedBy);
  input.addEventListener("change", () => {
    binding.set(input.checked);
    binding.onChange();
  });
  return el("label", { class: "switch", for: binding.id }, [
    input,
    el("span", { class: "switch-track", "aria-hidden": "true" }),
  ]);
}

function buildNumber(binding, describedBy) {
  const node = binding.node || {};
  const slider = node.slider && typeof node.slider === "object" ? node.slider : null;
  const isFloat = node.type === "float";
  const step = slider && typeof slider.step === "number" ? slider.step : isFloat ? 0.1 : 1;
  const row = el("div", { class: "number-row" });
  let range = null;

  const number = el("input", {
    type: "number",
    id: binding.id,
    class: "input input-number",
    step,
    inputmode: isFloat ? "decimal" : "numeric",
    value: setProgress(binding.get()) === "—" ? "" : String(binding.get()),
  });
  if (slider) {
    if (typeof slider.min === "number") number.setAttribute("min", String(slider.min));
    if (typeof slider.max === "number") number.setAttribute("max", String(slider.max));
  }
  if (describedBy) number.setAttribute("aria-describedby", describedBy);

  if (slider) {
    range = el("input", {
      type: "range",
      class: "input-range",
      min: typeof slider.min === "number" ? String(slider.min) : "0",
      max: typeof slider.max === "number" ? String(slider.max) : "100",
      step: String(step),
      "aria-label": `${node.description || binding.id} 滑块`,
      value: String(Number.isFinite(Number(binding.get())) ? Number(binding.get()) : 0),
    });
    range.addEventListener("input", () => {
      const parsed = Number(range.value);
      binding.set(isFloat ? parsed : Math.trunc(parsed));
      number.value = range.value;
      binding.onChange();
    });
    row.appendChild(range);
  }

  number.addEventListener("input", () => {
    const raw = number.value;
    if (raw.trim() === "") {
      binding.set("");
    } else {
      const parsed = Number(raw);
      binding.set(Number.isFinite(parsed) ? (isFloat ? parsed : Math.trunc(parsed)) : raw);
    }
    if (range) {
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) range.value = raw;
    }
    binding.onChange();
  });

  row.appendChild(number);
  if (slider) {
    const min = typeof slider.min === "number" ? slider.min : "—";
    const max = typeof slider.max === "number" ? slider.max : "—";
    row.appendChild(el("span", { class: "range-bounds", text: `${min} ~ ${max}` }));
  }
  return row;
}

function buildText(binding, describedBy) {
  const node = binding.node || {};
  const input = el("input", {
    type: "text",
    id: binding.id,
    class: "input",
    value: String(binding.get() ?? ""),
    autocomplete: "off",
    spellcheck: "false",
  });
  if (describedBy) input.setAttribute("aria-describedby", describedBy);
  input.addEventListener("input", () => {
    binding.set(input.value);
    binding.onChange();
  });
  return input;
}

function buildSecret(binding, describedBy) {
  const wrap = el("div", { class: "secret-row" });
  const input = el("input", {
    type: "password",
    id: binding.id,
    class: "input",
    value: "",
    autocomplete: "new-password",
    spellcheck: "false",
    placeholder: "留空表示保持原值",
  });
  if (describedBy) input.setAttribute("aria-describedby", describedBy);
  const toggle = el("button", {
    type: "button",
    class: "btn btn-ghost btn-small",
    text: "显示",
    "aria-pressed": "false",
    "aria-controls": binding.id,
  });
  toggle.addEventListener("click", () => {
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    toggle.textContent = showing ? "显示" : "隐藏";
    toggle.setAttribute("aria-pressed", showing ? "false" : "true");
  });
  input.addEventListener("input", () => {
    binding.set(input.value);
    binding.onChange();
  });
  wrap.appendChild(input);
  wrap.appendChild(toggle);
  return wrap;
}

function buildSelect(binding, describedBy) {
  const node = binding.node || {};
  const options = Array.isArray(node.options) ? node.options : [];
  const select = el("select", { id: binding.id, class: "input input-select" });
  if (describedBy) select.setAttribute("aria-describedby", describedBy);
  const current = binding.get();
  for (const option of options) {
    const value = typeof option === "string" ? option : String(option);
    const opt = el("option", { value, text: value });
    if (String(current) === value) opt.selected = true;
    select.appendChild(opt);
  }
  select.addEventListener("change", () => {
    binding.set(select.value);
    binding.onChange();
  });
  return select;
}

function buildTextarea(binding, describedBy) {
  const area = el("textarea", {
    id: binding.id,
    class: "input input-textarea",
    rows: "3",
    spellcheck: "false",
    value: String(binding.get() ?? ""),
  });
  if (describedBy) area.setAttribute("aria-describedby", describedBy);
  const autosize = () => {
    area.style.height = "auto";
    area.style.height = `${Math.min(Math.max(area.scrollHeight, 72), 420)}px`;
  };
  area.addEventListener("input", () => {
    binding.set(area.value);
    autosize();
    binding.onChange();
  });
  registerFlusher(autosize);
  setTimeout(autosize, 0);
  return area;
}

function buildChips(binding, describedBy) {
  const node = binding.node || {};
  const list = el("div", { class: "chips", role: "list" });
  const input = el("input", {
    type: "text",
    id: binding.id,
    class: "input chips-input",
    autocomplete: "off",
    spellcheck: "false",
    placeholder: node.description ? `输入${node.description}后回车或逗号添加` : "输入后回车或逗号添加",
  });
  if (describedBy) input.setAttribute("aria-describedby", describedBy);

  const readList = () => {
    const value = binding.get();
    return Array.isArray(value) ? value.slice() : [];
  };

  const renderChips = () => {
    list.textContent = "";
    const items = readList();
    items.forEach((item, index) => {
      const remove = el("button", {
        type: "button",
        class: "chip-remove",
        text: "×",
        "aria-label": `删除 ${item}`,
      });
      remove.addEventListener("click", () => {
        const next = readList();
        next.splice(index, 1);
        binding.set(next);
        renderChips();
        binding.onChange();
      });
      list.appendChild(
        el("span", { class: "chip chip-editable", role: "listitem" }, [el("span", { class: "chip-text", text: item }), remove]),
      );
    });
    if (!items.length) list.appendChild(el("span", { class: "chips-empty", text: "暂无内容" }));
  };

  const commit = (text) => {
    const parsed = parseListInput(text);
    if (!parsed.length) return;
    const next = readList();
    const seen = new Set(next);
    for (const item of parsed) {
      if (seen.has(item)) continue;
      seen.add(item);
      next.push(item);
    }
    binding.set(next);
    renderChips();
    binding.onChange();
  };

  input.addEventListener("keydown", (event) => {
    const key = event.key;
    if (key === "Enter" || key === "," || key === "，" || key === ";" || key === "；" || key === "、") {
      const text = input.value;
      if (key !== "Enter" || text.trim()) event.preventDefault();
      if (text.trim()) {
        commit(text);
        input.value = "";
      }
    } else if (key === "Backspace" && !input.value) {
      const next = readList();
      if (!next.length) return;
      next.pop();
      binding.set(next);
      renderChips();
      binding.onChange();
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

  registerFlusher(() => {
    if (input.value.trim()) {
      commit(input.value);
      input.value = "";
    }
  });

  renderChips();
  return el("div", { class: "chips-editor", onclick: (event) => {
    if (event.target === event.currentTarget) input.focus();
  } }, [list, input]);
}

function buildTemplateList(binding, describedBy) {
  const node = binding.node || {};
  const templates = node.templates && typeof node.templates === "object" ? node.templates : {};
  const templateKeys = Object.keys(templates);
  const container = el("div", { class: "template-list" });
  if (describedBy) container.setAttribute("aria-describedby", describedBy);

  const readList = () => {
    const value = binding.get();
    return Array.isArray(value) ? value : [];
  };

  const render = () => {
    container.textContent = "";
    const items = readList();
    items.forEach((item, index) => {
      container.appendChild(buildTemplateCard(binding, item, index, render, templates, templateKeys));
    });
    container.appendChild(buildTemplateAddRow(binding, templates, templateKeys, render));
  };

  render();
  return container;
}

function buildTemplateCard(binding, item, index, rerender, templates, templateKeys) {
  const templateKey = item && item.__template_key ? String(item.__template_key) : templateKeys[0] || "";
  const template = templates[templateKey] && typeof templates[templateKey] === "object" ? templates[templateKey] : {};
  const card = el("article", { class: "tpl-card", dataset: { index: String(index) } });
  const name = template.name || templateKey || "条目";
  const head = el("div", { class: "tpl-card-head" }, [
    el("span", { class: "tpl-card-title", text: `${name} #${index + 1}` }),
  ]);
  head.appendChild(
    el("button", {
      type: "button",
      class: "btn btn-ghost btn-small",
      text: "删除",
      "aria-label": `删除${name} #${index + 1}`,
      onclick: () => {
        const list = readSafeList(binding);
        list.splice(index, 1);
        binding.set(list);
        binding.onChange();
        rerender();
      },
    }),
  );
  card.appendChild(head);

  const body = el("div", { class: "tpl-card-body" });
  const itemSchema = template.items && typeof template.items === "object" ? template.items : {};
  for (const [subKey, subNodeRaw] of Object.entries(itemSchema)) {
    const subNode = subNodeRaw && typeof subNodeRaw === "object" ? subNodeRaw : {};
    const subId = `${binding.id}-${index}-${subKey}`;
    const hintId = `${subId}-hint`;
    const subWrap = el("div", { class: "field field-sub" });
    subWrap.appendChild(
      el("label", { class: "field-label", for: subId, text: subNode.description || subKey }),
    );
    const subBinding = {
      id: subId,
      node: subNode,
      get: () => (item ? item[subKey] : undefined),
      set: (value) => {
        if (item) item[subKey] = value;
      },
      onChange: binding.onChange,
    };
    subWrap.appendChild(buildControl(subBinding, subNode.hint ? hintId : ""));
    if (subNode.hint) {
      subWrap.appendChild(el("p", { class: "field-hint", id: hintId, text: String(subNode.hint) }));
    }
    body.appendChild(subWrap);
  }
  if (!Object.keys(itemSchema).length) {
    body.appendChild(el("p", { class: "field-hint", text: "该模板未定义子字段。" }));
  }
  card.appendChild(body);
  return card;
}

function readSafeList(binding) {
  const value = binding.get();
  return Array.isArray(value) ? value.slice() : [];
}

function buildTemplateAddRow(binding, templates, templateKeys, rerender) {
  const row = el("div", { class: "tpl-add" });
  const add = (templateKey) => {
    const list = readSafeList(binding);
    list.push(newTemplateItem(binding.node, templateKey));
    binding.set(list);
    binding.onChange();
    rerender();
  };

  if (templateKeys.length > 1) {
    const select = el("select", { class: "input input-select", "aria-label": "选择要添加的模板类型" });
    for (const key of templateKeys) {
      const tpl = templates[key] && typeof templates[key] === "object" ? templates[key] : {};
      select.appendChild(el("option", { value: key, text: tpl.name || key }));
    }
    row.appendChild(select);
    row.appendChild(el("button", { type: "button", class: "btn btn-small", text: "添加", onclick: () => add(select.value) }));
  } else if (templateKeys.length === 1) {
    row.appendChild(
      el("button", { type: "button", class: "btn btn-small", text: "添加一条", onclick: () => add(templateKeys[0]) }),
    );
  } else {
    row.appendChild(el("button", { type: "button", class: "btn btn-small", text: "添加", disabled: true }));
    row.appendChild(el("span", { class: "field-hint", text: "schema 未定义模板" }));
  }
  return row;
}

function buildProviderSelect(binding, describedBy, providers) {
  // _special = select_provider 的配置项：用后端返回的提供商列表渲染下拉框
  const select = el("select", { id: binding.id, class: "input input-select" });
  if (describedBy) select.setAttribute("aria-describedby", describedBy);
  const current = String(binding.get() ?? "");
  const list = Array.isArray(providers) ? providers : [];
  const first = el("option", { value: "", text: "（跟随当前会话使用的模型）" });
  if (!current) first.selected = true;
  select.appendChild(first);
  let matched = false;
  for (const item of list) {
    const id = String(item && item.id ? item.id : "");
    if (!id) continue;
    const opt = el("option", { value: id, text: String(item.name || id) });
    if (id === current) {
      opt.selected = true;
      matched = true;
    }
    select.appendChild(opt);
  }
  if (current && !matched) {
    // 配置里存着一个当前不可用的提供商：保留它，避免用户一打开页面就把配置改掉
    const opt = el("option", { value: current, text: `${current}（当前不可用）` });
    opt.selected = true;
    select.appendChild(opt);
  }
  if (!list.length) {
    select.appendChild(el("option", { value: current, text: "（当前没有可用的对话模型）", disabled: "disabled" }));
  }
  select.addEventListener("change", () => {
    binding.set(select.value);
    binding.onChange();
  });
  return select;
}

function buildControl(binding, describedBy, providers) {
  const node = binding.node || {};
  const type = node.type || "string";
  switch (type) {
    case "bool":
      return buildSwitch(binding, describedBy);
    case "int":
    case "float":
      return buildNumber(binding, describedBy);
    case "list":
      return buildChips(binding, describedBy);
    case "template_list":
      return buildTemplateList(binding, describedBy);
    case "text":
      return buildTextarea(binding, describedBy);
    default: {
      if (node._special === "select_provider") return buildProviderSelect(binding, describedBy, providers);
      if (Array.isArray(node.options) && node.options.length) return buildSelect(binding, describedBy);
      if (node.secret === true) return buildSecret(binding, describedBy);
      return buildText(binding, describedBy);
    }
  }
}

/* ------------------------------------------------------------------ *
 * 渲染
 * ------------------------------------------------------------------ */

function renderField(key, node) {
  const wrapper = el("div", { class: "field", id: `field-wrap-${key}`, dataset: { key } });
  const controlId = `field-${key}`;
  const hintId = `hint-${key}`;
  const errorId = `err-${key}`;

  const label = el("label", { class: "field-label", for: controlId });
  label.appendChild(el("span", { class: "field-label-text", text: node.description || key }));
  label.appendChild(el("code", { class: "field-key", text: key }));
  wrapper.appendChild(label);

  const binding = {
    id: controlId,
    node,
    get: () => state.draft[key],
    set: (value) => {
      state.draft[key] = value;
    },
    onChange: () => {
      refreshField(key);
      renderDirty();
    },
  };

  const control = el("div", { class: "field-control" });
  control.appendChild(buildControl(binding, node.hint ? `${hintId} ${errorId}` : errorId, state.providers));
  wrapper.appendChild(control);

  if (node.hint) {
    wrapper.appendChild(el("p", { class: "field-hint", id: hintId, text: String(node.hint) }));
  }
  wrapper.appendChild(el("p", { class: "field-error", id: errorId, hidden: true }));
  return wrapper;
}

function refreshField(key) {
  const wrap = state.fieldWraps.get(key);
  if (!wrap) return;
  const node = state.schema[key] || {};
  const error = validateValue(node, state.draft[key]);
  const warnOnly = Boolean(error) && isWarnOnlyKey(key);
  const errorEl = wrap.querySelector(`#err-${key}`);
  wrap.classList.toggle("has-error", Boolean(error) && !warnOnly);
  wrap.classList.toggle("has-warning", warnOnly);
  if (errorEl) {
    errorEl.textContent = error ? (warnOnly ? `${error}（仍可保存）` : error) : "";
    errorEl.hidden = !error;
    errorEl.classList.toggle("is-warning", warnOnly);
  }
}

function refreshAllFields() {
  for (const key of state.fieldWraps.keys()) refreshField(key);
}

function renderHeader() {
  const version = state.meta && state.meta.version ? String(state.meta.version) : "";
  els.version.textContent = version;
  els.version.hidden = !version;
  els.version.title = state.meta && state.meta.display_name ? String(state.meta.display_name) : "";
}

function renderStatus() {
  els.statusBar.textContent = "";
  const chips = statusChips(state.status);
  if (!chips.length) {
    els.statusBar.hidden = true;
    return;
  }
  els.statusBar.hidden = false;
  for (const chip of chips) {
    const node = el("span", { class: `chip chip-status${chip.tone ? ` chip-${chip.tone}` : ""}` });
    node.appendChild(el("span", { class: "chip-label", text: chip.label }));
    node.appendChild(el("span", { class: "chip-value", text: String(chip.value ?? "—") }));
    if (chip.hint) node.title = String(chip.hint);
    els.statusBar.appendChild(node);
  }
}

function renderGroupNav(groups) {
  els.groupNav.textContent = "";
  for (const group of groups) {
    if (!group.fields.length) continue;
    els.groupNav.appendChild(
      el("a", { class: "nav-item", href: `#group-${group.id}`, text: group.title }),
    );
  }
}

function renderGroups() {
  els.groups.textContent = "";
  state.fieldWraps = new Map();
  state.flushers = [];
  const groups = groupFields(state.schema);
  renderGroupNav(groups);

  for (const group of groups) {
    if (!group.fields.length) continue;
    const section = el("section", { class: "group", id: `group-${group.id}` });
    const head = el("div", { class: "group-head" }, [
      el("h2", { class: "group-title", text: group.title }),
      el("span", { class: "group-count", text: `${group.fields.length} 项` }),
    ]);
    section.appendChild(head);
    const grid = el("div", { class: "field-grid" });
    for (const field of group.fields) {
      const wrapper = renderField(field.key, field.node);
      state.fieldWraps.set(field.key, wrapper);
      grid.appendChild(wrapper);
    }
    section.appendChild(grid);
    els.groups.appendChild(section);
  }
}

function renderAll() {
  renderHeader();
  renderStatus();
  renderGroups();
  refreshAllFields();
  renderDirty();
}

function renderDirty() {
  const changed = changedKeys(state.schema, state.draft, state.values, state.secretFields);
  const count = changed.length;
  els.dirtyHint.hidden = count === 0;
  els.dirtyHint.textContent = count ? `有 ${count} 项未保存的改动` : "";
  const busy = state.saving || state.loading;
  els.btnSave.disabled = count === 0 || busy;
  els.btnReload.disabled = busy;
  els.btnRestore.disabled = busy || !Object.keys(state.schema).length;
}

function setBusy(busy) {
  els.app.dataset.state = busy ? "busy" : "ready";
}

/* ------------------------------------------------------------------ *
 * 加载 / 保存 / 默认值
 * ------------------------------------------------------------------ */

async function loadSettings(options = {}) {
  const bridge = state.bridge;
  if (!bridge || typeof bridge.apiGet !== "function") {
    showMessage("error", "当前环境不支持 AstrBot 插件页面 bridge（缺少 apiGet）。");
    return;
  }
  state.loading = true;
  renderDirty();
  try {
    const data = unwrap(await bridge.apiGet("settings"));
    const payload = data && typeof data === "object" ? data : {};
    state.schema = payload.schema && typeof payload.schema === "object" ? payload.schema : {};
    state.values = payload.values && typeof payload.values === "object" ? payload.values : {};
    state.secretFields = Array.isArray(payload.secret_fields)
      ? payload.secret_fields.map((item) => String(item))
      : [];
    state.status = payload.status && typeof payload.status === "object" ? payload.status : null;
    state.providers = Array.isArray(payload.providers) ? payload.providers : [];
    state.meta = payload.meta && typeof payload.meta === "object" ? payload.meta : {};
    state.draft = toDraft(state.schema, state.values, state.secretFields);
    renderAll();
    if (!Object.keys(state.schema).length) {
      renderSchemaError("响应格式异常：未能从后端响应中解析出配置 schema。");
    } else if (options.announce) {
      showMessage("info", "已重新加载配置，未保存的改动已丢弃。", { autoHide: true });
    } else {
      clearMessage();
    }
  } catch (err) {
    clearMessage();
    showMessage("error", `读取配置失败：${errText(err, "读取配置失败")}`);
  } finally {
    state.loading = false;
    renderDirty();
    setBusy(false);
  }
}

function restoreDefaults() {
  if (!Object.keys(state.schema).length) return;
  state.draft = defaultsFromSchema(state.schema);
  renderAll();
  showMessage("info", "已填入 schema 默认值（尚未保存），确认后点击「保存」。");
}

async function saveSettings() {
  const bridge = state.bridge;
  if (!bridge || typeof bridge.apiPost !== "function") {
    showMessage("error", "当前环境不支持 AstrBot 插件页面 bridge（缺少 apiPost）。");
    return;
  }
  if (state.saving || state.loading) return;

  flushAll();
  refreshAllFields();

  const blocking = [];
  for (const { key, node } of fieldsFromSchema(state.schema)) {
    if (isWarnOnlyKey(key)) continue;
    if (validateValue(node, state.draft[key]) !== null) blocking.push(key);
  }
  if (blocking.length) {
    const head = blocking.slice(0, 6).join("、");
    const tail = blocking.length > 6 ? " 等" : "";
    showMessage("error", `有 ${blocking.length} 个字段未通过校验，请先修正：${head}${tail}`);
    const first = state.fieldWraps.get(blocking[0]);
    if (first && typeof first.scrollIntoView === "function") {
      first.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    return;
  }

  const payload = diffValues(state.schema, state.draft, state.values, state.secretFields);
  const keys = Object.keys(payload);
  if (!keys.length) {
    showMessage("info", "没有需要保存的改动。", { autoHide: true });
    renderDirty();
    return;
  }

  state.saving = true;
  renderDirty();
  setBusy(true);
  try {
    const response = unwrap(await bridge.apiPost("settings", { values: payload }));
    if (
      response &&
      typeof response === "object" &&
      typeof response.status === "string" &&
      response.status !== "ok"
    ) {
      throw new Error(response.message || response.msg || `后端返回状态：${response.status}`);
    }
    if (!response || typeof response !== "object") {
      showMessage(
        "error",
        "响应格式异常：保存结果不是预期对象，请点击「重新加载」确认配置是否已生效。",
      );
      renderDirty();
      return;
    }
    state.values =
      response.values && typeof response.values === "object"
        ? response.values
        : Object.assign({}, state.values, payload);
    if (response.status && typeof response.status === "object") state.status = response.status;
    state.draft = toDraft(state.schema, state.values, state.secretFields);
    renderAll();
    const saved = Array.isArray(response.saved) && response.saved.length ? response.saved : keys;
    // 保存成功 + 后端 warnings：用警告样式原样逐条展示（不视为失败，仍需刷新基准值、清掉未保存标记）。
    const warnings = Array.isArray(response.warnings)
      ? response.warnings.map((item) => String(item)).filter((item) => item.trim())
      : [];
    if (warnings.length) {
      const lines = [`已保存 ${saved.length} 项配置，但有以下提示：`, ...warnings];
      showMessage("warning", lines.join("\n"));
    } else {
      showMessage("success", `已保存 ${saved.length} 项配置：${saved.join("、")}`, { autoHide: true });
    }
  } catch (err) {
    showMessage("error", `保存失败：${errText(err)}`);
  } finally {
    state.saving = false;
    renderDirty();
    setBusy(false);
  }
}

/* ------------------------------------------------------------------ *
 * 启动
 * ------------------------------------------------------------------ */

function renderSchemaError(message) {
  els.app.dataset.state = "error";
  showMessage("error", message);
  els.statusBar.textContent = "";
  els.statusBar.hidden = true;
  els.groups.textContent = "";
  els.groups.appendChild(
    el("div", { class: "fatal" }, [
      el("p", { text: "响应格式异常，无法渲染配置项。" }),
      el("p", { class: "field-hint", text: message }),
      el("button", {
        type: "button",
        class: "btn",
        text: "重新加载",
        onclick: () => {
          state.loading = false;
          loadSettings({ announce: true });
        },
      }),
    ]),
  );
  renderDirty();
}

function renderFatal(message) {
  els.app.dataset.state = "error";
  showMessage("error", message);
  els.statusBar.hidden = true;
  els.groupNav.hidden = true;
  els.groups.textContent = "";
  els.groups.appendChild(
    el("div", { class: "fatal" }, [
      el("p", { text: "无法在此环境中初始化插件页面。" }),
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
  state.schema = {};
  renderDirty();
}

function cacheElements() {
  els.app = document.getElementById("app");
  els.title = document.getElementById("page-title");
  els.desc = document.getElementById("page-desc");
  els.version = document.getElementById("plugin-version");
  els.statusBar = document.getElementById("status-bar");
  els.messageBar = document.getElementById("message-bar");
  els.groupNav = document.getElementById("group-nav");
  els.groups = document.getElementById("groups");
  els.dirtyHint = document.getElementById("dirty-hint");
  els.btnSave = document.getElementById("btn-save");
  els.btnReload = document.getElementById("btn-reload");
  els.btnRestore = document.getElementById("btn-restore-defaults");
}

async function boot() {
  cacheElements();
  els.btnSave.addEventListener("click", () => {
    saveSettings();
  });
  els.btnReload.addEventListener("click", () => {
    loadSettings({ announce: true });
  });
  els.btnRestore.addEventListener("click", () => {
    restoreDefaults();
  });

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
      state.offContext = bridge.onContext((next) => applyContext(next));
    } catch {
      state.offContext = null;
    }
  }

  await loadSettings();
}

boot();
