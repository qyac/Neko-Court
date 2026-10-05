/**
 * review-site/static/app.js
 *
 * 临时审核群 · 网页审核站交互层（ESM）。
 * - 申请页：本地校验 → POST ./api/apply → 轮询 ./api/apply/<ticket> → 三态结果卡片 + 复制验证码。
 * - 管理后台：首屏数据块直出表格 → 搜索/筛选/分页 → 决策（通过/拒绝/拉黑/解除/删除）→ 设置保存。
 * 只依赖同源 JSON 接口与 ./form.js；所有写入到页面的数据都走 textContent（不做 innerHTML 拼接）。
 */

import {
  DEFAULT_PAGE_SIZE,
  NOTE_MAX,
  buildApplyPayload,
  buildDecisionPayload,
  formatTime,
  isTerminal,
  nextOffset,
  prevOffset,
  relativeTime,
  statusClass,
  statusLabel,
  validateForm,
} from "./form.js";

const PAGE_SIZE = DEFAULT_PAGE_SIZE;
const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 60000;
const SEARCH_DEBOUNCE_MS = 300;
const DASH = "—";

/* ------------------------------------------------------------------ *
 * DOM / 通用工具
 * ------------------------------------------------------------------ */

function byId(id) {
  return document.getElementById(id);
}

/** 极简元素构造器：文本一律走 textContent，避免注入。 */
function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = String(value);
      else if (key === "text") node.textContent = String(value);
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2), value);
      } else node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children || []) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function setText(id, text) {
  const node = byId(id);
  if (node) node.textContent = text;
}

function text(value, fallback = DASH) {
  if (value === null || value === undefined || value === "") return fallback;
  return String(value);
}

/** 顶部告警条：message 为空则隐藏。 */
function showAlert(message, kind = "error") {
  const bar = byId("alert-bar");
  if (!bar) return;
  bar.replaceChildren();
  if (!message) {
    bar.hidden = true;
    bar.removeAttribute("role");
    return;
  }
  bar.hidden = false;
  bar.setAttribute("role", "alert");
  bar.className = `alert alert-${kind}`;
  bar.append(el("span", { class: "alert-text", text: message }));
  bar.append(
    el("button", {
      type: "button",
      class: "alert-close",
      "aria-label": "关闭提示",
      text: "✕",
      onclick: () => showAlert(""),
    }),
  );
}

/** 登录态失效：给出明确文案与登录页链接。 */
function showAuthExpired() {
  const bar = byId("alert-bar");
  if (!bar) return;
  bar.hidden = false;
  bar.className = "alert alert-error";
  bar.setAttribute("role", "alert");
  bar.replaceChildren(
    el("span", { class: "alert-text", text: "登录已过期，请重新登录后继续操作。" }),
    el("a", { class: "btn btn-small", href: "./admin", text: "去登录" }),
  );
}

function toast(message, kind = "ok") {
  let host = byId("toast-host");
  if (!host) {
    host = el("div", { id: "toast-host", class: "toast-host", "aria-live": "polite" });
    document.body.append(host);
  }
  const item = el("div", { class: `toast toast-${kind}`, text: message });
  host.append(item);
  window.setTimeout(() => item.remove(), 2600);
}

/**
 * 统一请求封装：始终带同源 cookie，尽量解析 JSON，
 * 按 res.ok / body.ok 判定成功，并把后端 error 文案原样带出。
 */
async function requestJson(path, options = {}) {
  const init = { credentials: "same-origin", ...options };
  init.headers = Object.assign({ Accept: "application/json" }, options.headers || {});

  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    return { ok: false, status: 0, body: null, error: "网络异常，请检查网络后重试。" };
  }

  let raw = "";
  try {
    raw = await res.text();
  } catch (err) {
    raw = "";
  }
  let body = null;
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch (err) {
      body = null;
    }
  }

  if (res.status === 401) showAuthExpired();

  const backendError = body && typeof body.error === "string" ? body.error : "";
  const ok = res.ok && !(body && body.ok === false);
  return {
    ok,
    status: res.status,
    body,
    error: backendError || (res.ok ? "" : `请求失败（HTTP ${res.status}）`),
  };
}

function postJson(path, payload) {
  return requestJson(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

/** 复制文本：优先异步剪贴板，失败退回 execCommand，再失败返回 false。 */
async function copyText(value) {
  const content = String(value === null || value === undefined ? "" : value);
  if (!content) return false;

  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(content);
      return true;
    }
  } catch (err) {
    /* 继续尝试 execCommand */
  }

  try {
    const area = document.createElement("textarea");
    area.value = content;
    area.setAttribute("readonly", "readonly");
    area.setAttribute("aria-hidden", "true");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    if (typeof area.setSelectionRange === "function") area.setSelectionRange(0, content.length);
    const done = document.execCommand("copy");
    area.remove();
    if (done) return true;
  } catch (err) {
    /* 退回手动复制提示 */
  }

  return false;
}

/* ------------------------------------------------------------------ *
 * 申请页
 * ------------------------------------------------------------------ */

function initApplyPage() {
  const form = byId("apply-form");
  if (!form) return;

  const qqInput = byId("qq");
  const uidInput = byId("uid");
  const noteInput = byId("note");
  const noteCount = byId("note-count");
  const submitBtn = byId("apply-submit");
  const progress = byId("apply-progress");
  const progressText = byId("apply-progress-text");
  const resultBox = byId("apply-result");

  let timer = null;
  let polling = false;

  function setProgress(message) {
    if (!progress) return;
    if (!message) {
      progress.hidden = true;
      return;
    }
    progress.hidden = false;
    if (progressText) progressText.textContent = message;
  }

  function setSubmitting(busy) {
    if (!submitBtn) return;
    submitBtn.disabled = busy;
    submitBtn.setAttribute("aria-busy", busy ? "true" : "false");
    submitBtn.textContent = busy ? "核验中…" : "提交申请";
  }

  function clearErrors() {
    for (const key of ["qq", "uid", "note"]) {
      setText(`${key}-error`, "");
      const field = byId(key);
      if (field) field.removeAttribute("aria-invalid");
    }
  }

  function showErrors(errors) {
    clearErrors();
    let first = null;
    for (const key of ["qq", "uid", "note"]) {
      const message = errors && errors[key];
      if (!message) continue;
      setText(`${key}-error`, message);
      const field = byId(key);
      if (field) {
        field.setAttribute("aria-invalid", "true");
        if (!first) first = field;
      }
    }
    if (first && typeof first.focus === "function") first.focus();
  }

  function stopPolling() {
    polling = false;
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  }

  function showResultCard(status, builder) {
    setProgress("");
    stopPolling();
    const card = el("div", { class: `result-card result-${statusClass(status)}` });
    card.append(el("p", { class: "result-status", text: `申请状态：${statusLabel(status)}` }));
    builder(card);
    if (resultBox) {
      resultBox.hidden = false;
      resultBox.replaceChildren(card);
      if (typeof resultBox.focus === "function") resultBox.focus();
    }
  }

  function renderApproved(data) {
    showResultCard("approved", (card) => {
      const code = text(data.code, "");
      const pluginConnected = data.plugin_connected !== false;

      if (!code) {
        // 已通过核验但插件还没同步验证码：不要给出空白的大号验证码
        card.append(
          el("p", {
            class: "alert alert-warn",
            role: "alert",
            text: "已通过核验，但站点还没有拿到当日验证码（插件可能未连接或未同步）。请联系管理员获取验证码。",
          }),
        );
      } else {
        card.append(
          el("div", { class: "code-box" }, [
            el("p", { class: "code-label", text: "今日验证码（请发送给群机器人或管理员）" }),
            el("output", { class: "code-value", id: "code-value", text: code }),
          ]),
        );

        const copyBtn = el("button", {
          type: "button",
          class: "btn btn-primary btn-block",
          "aria-label": "复制验证码",
          text: "复制验证码",
        });
        copyBtn.addEventListener("click", async () => {
          const done = await copyText(code);
          if (done) {
            toast("验证码已复制");
            copyBtn.textContent = "已复制";
          } else {
            const node = byId("code-value");
            if (node && typeof node.focus === "function") node.focus();
            toast("复制失败，请长按上方验证码手动复制", "error");
          }
        });
        card.append(copyBtn);
      }

      if (!pluginConnected) {
        card.append(el("p", { class: "hint", text: "提示：站点插件当前未连接，验证码可能尚未同步。" }));
      }

      const list = el("dl", { class: "kv" });
      list.append(el("dt", { text: "有效期" }), el("dd", { text: text(data.expire) }));
      list.append(el("dt", { text: "B站昵称" }), el("dd", { text: text(data.uid_name) }));
      list.append(el("dt", { text: "B站 UID" }), el("dd", { text: text(data.uid) }));
      list.append(el("dt", { text: "QQ 号" }), el("dd", { text: text(data.qq) }));
      card.append(list);
      card.append(el("p", { class: "hint", text: "把验证码发给群里的机器人或管理员即可入群。" }));
    });
  }

  function renderRejected(data, status) {
    showResultCard(status, (card) => {
      card.append(el("p", { text: statusLabel(status) === "已拉黑" ? "该 QQ 已被拉黑，无法申请入群。" : "很抱歉，本次申请未通过。" }));
      card.append(el("p", { text: `原因：${text(data.reason, "未提供原因")}` }));
      card.append(el("p", { class: "hint", text: "如有疑问，请在群里联系管理员确认。" }));
    });
  }

  function renderManual(data) {
    showResultCard("manual", (card) => {
      card.append(el("p", { text: "已转人工审核，管理员会尽快处理。" }));
      if (data.reason) card.append(el("p", { text: `说明：${text(data.reason)}` }));
      card.append(el("p", { class: "hint", text: "处理完成后可回到本页刷新查看结果。" }));
      if (data.ticket) card.append(el("p", { class: "small muted", text: `申请编号：${text(data.ticket)}` }));
    });
  }

  function renderPending() {
    showResultCard("pending", (card) => {
      card.append(el("p", { text: "已提交，仍在核验中…" }));
      card.append(el("p", { class: "hint", text: "页面会自动刷新状态，也可以稍后重新打开本页查看。" }));
    });
  }

  function renderResult(data) {
    const status = data && data.status ? String(data.status) : "pending";
    if (status === "approved") return renderApproved(data);
    if (status === "blocked") return renderRejected(data, "blocked");
    if (status === "rejected") return renderRejected(data, "rejected");
    if (status === "manual") return renderManual(data);
    return renderPending();
  }

  function addRestartButton() {
    const card = resultBox && resultBox.querySelector(".result-card");
    if (!card || card.querySelector("#apply-restart")) return;
    card.append(
      el("div", { class: "btn-row" }, [
        el("button", {
          type: "button",
          id: "apply-restart",
          class: "btn",
          text: "重新填写",
          onclick: () => {
            stopPolling();
            if (resultBox) {
              resultBox.hidden = true;
              resultBox.replaceChildren();
            }
            setProgress("");
            clearErrors();
            if (noteInput) noteInput.value = "";
            if (noteCount) noteCount.textContent = `0 / ${NOTE_MAX}`;
            if (qqInput) {
              qqInput.value = "";
              if (typeof qqInput.focus === "function") qqInput.focus();
            }
            if (uidInput) uidInput.value = "";
          },
        }),
      ]),
    );
  }

  function startPolling(ticket) {
    if (!ticket) {
      setProgress("已提交，等待管理员处理，可稍后刷新页面查看结果。");
      return;
    }
    const startedAt = Date.now();
    polling = true;
    setProgress("已提交，正在核验…（每 2 秒自动刷新）");
    timer = window.setInterval(async () => {
      if (!polling) return;
      if (Date.now() - startedAt >= POLL_TIMEOUT_MS) {
        stopPolling();
        setProgress("仍在核验，可刷新页面查看。");
        return;
      }
      const out = await requestJson(`./api/apply/${encodeURIComponent(ticket)}`);
      if (!out.ok) {
        if (out.status === 404) {
          stopPolling();
          setProgress("");
          showAlert(out.error || "编号不存在。");
        }
        return;
      }
      const data = out.body || {};
      if (isTerminal(data.status)) {
        stopPolling();
        renderResult(data);
        addRestartButton();
      }
    }, POLL_INTERVAL_MS);
  }

  function handleApplyResponse(data) {
    const payload = data || {};
    if (isTerminal(payload.status)) {
      renderResult(payload);
      addRestartButton();
      return;
    }
    renderPending();
    addRestartButton();
    startPolling(payload.ticket);
  }

  if (noteInput && noteCount) {
    noteInput.addEventListener("input", () => {
      noteCount.textContent = `${noteInput.value.length} / ${NOTE_MAX}`;
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    stopPolling();
    showAlert("");
    if (resultBox) {
      resultBox.hidden = true;
      resultBox.replaceChildren();
    }

    const values = {
      qq: qqInput ? qqInput.value : "",
      uid: uidInput ? uidInput.value : "",
      note: noteInput ? noteInput.value : "",
    };

    const check = validateForm(values);
    if (!check.ok) {
      showErrors(check.errors);
      setProgress("");
      return;
    }
    clearErrors();
    setSubmitting(true);
    setProgress("正在向 B站核验，请稍候…");

    const out = await postJson("./api/apply", buildApplyPayload(values));
    setSubmitting(false);

    if (!out.ok) {
      setProgress("");
      showAlert(out.error || "申请提交失败，请稍后重试。");
      return;
    }
    handleApplyResponse(out.body);
  });
}

/* ------------------------------------------------------------------ *
 * 登录页
 * ------------------------------------------------------------------ */

function setLoginError(message) {
  const slot = byId("login-error-slot");
  if (slot) {
    slot.replaceChildren();
    if (message) slot.append(el("div", { class: "alert alert-error", role: "alert", text: message }));
  }
  showAlert(message, "error");
}

function initLoginPage() {
  const form = byId("login-form");
  if (!form) return;

  const passwordInput = byId("password");
  const submitBtn = byId("login-submit");

  form.addEventListener("submit", async (event) => {
    // 无 JS 时表单会原生 POST 到 ./api/admin/login（后端会渲染登录页并回填 $error_html）
    event.preventDefault();
    const password = passwordInput ? passwordInput.value : "";
    if (!password) {
      setText("password-error", "请输入管理员密码");
      if (passwordInput) {
        passwordInput.setAttribute("aria-invalid", "true");
        passwordInput.focus();
      }
      return;
    }
    setText("password-error", "");
    if (passwordInput) passwordInput.removeAttribute("aria-invalid");
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.setAttribute("aria-busy", "true");
      submitBtn.textContent = "登录中…";
    }

    const out = await postJson("./api/admin/login", { password });
    if (out.ok) {
      showAlert("");
      if (submitBtn) submitBtn.textContent = "登录成功，正在进入后台…";
      window.location.assign("./admin");
      return;
    }

    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.setAttribute("aria-busy", "false");
      submitBtn.textContent = "登录";
    }
    setLoginError(out.error || "登录失败，请稍后重试。");
    if (passwordInput) {
      passwordInput.setAttribute("aria-invalid", "true");
      passwordInput.select();
    }
  });
}

/* ------------------------------------------------------------------ *
 * 管理后台
 * ------------------------------------------------------------------ */

/** 读取后端塞入的首屏数据块；解析失败返回 null（不影响后续接口刷新）。 */
function readInitialData() {
  const node = byId("initial-data");
  if (!node) return null;
  const raw = (node.textContent || "").trim();
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (err) {
    return null;
  }
}

function initAdminPage() {
  const table = byId("applications-table");
  if (!table) return;

  const tbody = byId("applications-body");
  const statusSelect = byId("status-filter");
  const searchInput = byId("search-input");
  const pagerInfo = byId("pager-info");
  const prevBtn = byId("prev-page");
  const nextBtn = byId("next-page");
  const refreshBtn = byId("refresh-btn");
  const settingsForm = byId("settings-form");
  const settingsSubmit = byId("settings-submit");

  const csrfMeta = document.querySelector('meta[name="csrf"]');
  const state = {
    csrf: csrfMeta ? csrfMeta.getAttribute("content") || "" : "",
    status: "all",
    q: "",
    offset: 0,
    total: 0,
    items: [],
  };

  let searchTimer = null;
  let requestSeq = 0;

  /* ---------- 首屏数据块 ---------- */

  const initial = readInitialData() || {};
  if (initial.csrf) state.csrf = String(initial.csrf);
  if (typeof initial.status === "string" && initial.status) {
    state.status = initial.status;
    if (statusSelect) statusSelect.value = initial.status;
  }
  if (typeof initial.offset === "number" && initial.offset >= 0) state.offset = initial.offset;

  const initialItems = Array.isArray(initial.items)
    ? initial.items
    : Array.isArray(initial.applications)
      ? initial.applications
      : [];

  const renderRows = () => {
    if (!tbody) return;
    tbody.replaceChildren();
    if (!state.items.length) {
      tbody.append(
        el("tr", { class: "empty-row" }, [
          el("td", { colspan: "9", text: "没有符合条件的申请记录。" }),
        ]),
      );
      return;
    }
    for (const item of state.items) tbody.append(buildRow(item));
  };

  const renderPager = () => {
    const from = state.total === 0 ? 0 : state.offset + 1;
    const to = Math.min(state.offset + state.items.length, state.total);
    if (pagerInfo) pagerInfo.textContent = `第 ${from}-${to} 条，共 ${state.total} 条`;
    if (prevBtn) prevBtn.disabled = state.offset <= 0;
    if (nextBtn) {
      nextBtn.disabled = state.offset + Math.max(state.items.length, 0) >= state.total;
    }
  };

  function actionsFor(status) {
    if (status === "blocked") {
      return [
        { action: "unblock", label: "解除拉黑", cls: "btn-ok" },
        { action: "delete", label: "删除", cls: "btn-danger" },
      ];
    }
    const list = [];
    if (status !== "approved") list.push({ action: "approve", label: "通过", cls: "btn-ok" });
    if (status !== "rejected") list.push({ action: "reject", label: "拒绝", cls: "btn-warn" });
    list.push({ action: "block", label: "拉黑", cls: "btn-danger" });
    list.push({ action: "delete", label: "删除", cls: "btn-danger" });
    return list;
  }

  function buildRow(item) {
    const data = item && typeof item === "object" ? item : {};
    const status = data.status ? String(data.status) : "unknown";
    const tr = el("tr");

    tr.append(
      el("td", {
        title: relativeTime(data.created_at),
        text: formatTime(data.created_at),
      }),
    );
    tr.append(el("td", { text: text(data.qq) }));

    const uidText = data.uid === null || data.uid === undefined ? "" : String(data.uid).trim();
    const uidCell = el("td");
    if (/^\d+$/.test(uidText)) {
      uidCell.append(
        el("a", {
          href: `https://space.bilibili.com/${uidText}`,
          target: "_blank",
          rel: "noopener noreferrer",
          text: uidText,
        }),
      );
    } else {
      uidCell.textContent = uidText || DASH;
    }
    tr.append(uidCell);

    tr.append(el("td", { text: text(data.uid_name) }));

    const level = Number.isFinite(Number(data.level)) ? String(Number(data.level)) : DASH;
    const fans = Number.isFinite(Number(data.fans)) ? String(Number(data.fans)) : DASH;
    tr.append(el("td", { text: `${level} 级 / ${fans} 粉` }));

    tr.append(
      el("td", {}, [
        el("span", {
          class: `badge badge-${statusClass(status)}`,
          text: statusLabel(status),
        }),
      ]),
    );

    tr.append(el("td", { text: text(data.code) }));
    tr.append(el("td", { class: "cell-note", text: text(data.note) }));

    const actionsCell = el("td", { class: "cell-actions" });
    const row = el("div", { class: "btn-row" });
    for (const spec of actionsFor(status)) {
      row.append(
        el("button", {
          type: "button",
          class: `btn btn-small ${spec.cls || ""}`.trim(),
          dataset: { action: spec.action, id: String(data.id) },
          "aria-label": `${spec.label}：QQ ${text(data.qq, "未知")}`,
          text: spec.label,
        }),
      );
    }
    actionsCell.append(row);
    tr.append(actionsCell);
    return tr;
  }

  function renderStats(stats) {
    const host = byId("stats");
    if (!host) return;
    const data = stats && typeof stats === "object" ? stats : {};
    const pick = (key) => {
      if (Number.isFinite(Number(data[key]))) return Number(data[key]);
      // 后端 counts() 里“拉黑名单”记在 blocked_list；退回记录级 blocked
      if (key === "blocked_list" && Number.isFinite(Number(data.blocked))) return Number(data.blocked);
      return 0;
    };
    const defs = [
      ["total", "总数"],
      ["pending", "待审核"],
      ["approved", "已通过"],
      ["rejected", "已拒绝"],
      ["manual", "待人工"],
      ["blocked_list", "拉黑"],
    ];
    const grid = el("div", { class: "stat-grid", role: "list" });
    for (const [key, label] of defs) {
      grid.append(
        el("div", { class: "stat", role: "listitem" }, [
          el("div", { class: "stat-label", text: label }),
          el("div", { class: "stat-value", text: String(pick(key)) }),
        ]),
      );
    }
    host.replaceChildren(grid);
  }

  function renderPluginStatus(plugin) {
    const host = byId("plugin-status");
    if (!host) return;
    const data = plugin && typeof plugin === "object" ? plugin : {};
    const connected = data.connected === true;

    const list = el("dl", { class: "plugin-lines" });
    const rows = [
      [
        "最后同步时间",
        data.last_sync_at
          ? `${formatTime(data.last_sync_at)}（${relativeTime(data.last_sync_at)}）`
          : DASH,
      ],
      ["当前验证码", text(data.code)],
      ["有效期", text(data.expire)],
      ["已拉取条数", Number.isFinite(Number(data.pulled)) ? String(Number(data.pulled)) : "0"],
    ];
    if (Array.isArray(data.groups) && data.groups.length) {
      rows.push(["审核群", data.groups.map((item) => String(item)).join("、")]);
    }
    if (data.pull_cursor) rows.push(["拉取游标", text(data.pull_cursor)]);
    for (const [key, value] of rows) {
      list.append(el("dt", { text: key }), el("dd", { text: value }));
    }

    host.replaceChildren(
      el("p", { class: "row" }, [
        el("span", {
          class: connected ? "status-pill status-pill-on" : "status-pill status-pill-off",
          text: connected ? "已连接" : "未连接",
        }),
      ]),
      list,
    );

    if (!connected) {
      host.append(
        el("p", {
          class: "alert alert-warn",
          role: "alert",
          text: "插件未连接：请检查 plugin_url 与 plugin_token 是否正确、插件是否已启动。",
        }),
      );
    }
  }

  function fillSettingsForm(settings) {
    if (!settingsForm || !settings || typeof settings !== "object") return;
    const map = {
      site_name: "text",
      group_line: "text",
      bili_min_level: "number",
      bili_min_fans: "number",
      plugin_url: "text",
    };
    for (const [key, kind] of Object.entries(map)) {
      const node = byId(key);
      if (!node) continue;
      const value = settings[key];
      node.value = value === null || value === undefined ? "" : String(value);
      if (kind === "number" && node.value === "") node.value = "0";
    }

    const keywords = Array.isArray(settings.bili_name_keywords)
      ? settings.bili_name_keywords.join(", ")
      : text(settings.bili_name_keywords, "");
    const keywordsInput = byId("bili_name_keywords");
    if (keywordsInput) keywordsInput.value = keywords;

    const unique = byId("bili_unique");
    if (unique) unique.checked = settings.bili_unique !== false;

    const usePlugin = byId("use_plugin_rules");
    if (usePlugin) usePlugin.checked = settings.use_plugin_rules !== false;

    const onError = byId("bili_on_error");
    if (onError) onError.value = settings.bili_on_error ? String(settings.bili_on_error) : "manual";

    // 后端不回显 plugin_token；密码相关输入框一律清空
    const tokenInput = byId("plugin_token");
    if (tokenInput) tokenInput.value = "";
    const passwordInput = byId("admin_password");
    if (passwordInput) passwordInput.value = "";
  }

  function readSettingsForm() {
    const value = (id, fallback = "") => {
      const node = byId(id);
      return node ? String(node.value).trim() : fallback;
    };
    const checked = (id, fallback = true) => {
      const node = byId(id);
      return node ? node.checked === true : fallback;
    };
    const toInt = (raw, fallback = 0) => {
      const n = Number.parseInt(raw, 10);
      return Number.isFinite(n) && n >= 0 ? n : fallback;
    };
    const keywords = value("bili_name_keywords")
      .split(/[,，、;；\s]+/)
      .map((item) => item.trim())
      .filter(Boolean);

    return {
      site_name: value("site_name"),
      group_line: value("group_line"),
      bili_min_level: toInt(value("bili_min_level", "0"), 0),
      bili_min_fans: toInt(value("bili_min_fans", "0"), 0),
      bili_name_keywords: keywords,
      bili_unique: checked("bili_unique"),
      bili_on_error: value("bili_on_error", "manual") || "manual",
      use_plugin_rules: checked("use_plugin_rules"),
      plugin_url: value("plugin_url"),
      plugin_token: value("plugin_token"),
      admin_password: value("admin_password"),
    };
  }

  /* ---------- 数据加载 ---------- */

  async function loadApplications() {
    const seq = ++requestSeq;
    table.setAttribute("aria-busy", "true");

    const params = new URLSearchParams();
    params.set("status", state.status || "all");
    if (state.q) params.set("q", state.q);
    params.set("offset", String(state.offset));

    const out = await requestJson(`./api/admin/applications?${params.toString()}`);
    if (seq !== requestSeq) return; // 已有更新的请求，丢弃这次响应
    table.setAttribute("aria-busy", "false");

    if (!out.ok) {
      if (out.status !== 401) showAlert(out.error || "列表加载失败，请稍后重试。");
      return;
    }

    const body = out.body || {};
    state.items = Array.isArray(body.items) ? body.items : [];
    state.total = Number.isFinite(Number(body.total)) ? Number(body.total) : state.items.length;
    if (Number.isFinite(Number(body.offset)) && Number(body.offset) >= 0) {
      state.offset = Number(body.offset);
    }
    if (body.counts) renderStats(body.counts);
    renderRows();
    renderPager();
  }

  async function loadSettings(refillForm) {
    const out = await requestJson("./api/admin/settings");
    if (!out.ok) {
      if (out.status !== 401) showAlert(out.error || "设置加载失败，请稍后重试。");
      return;
    }
    const body = out.body || {};
    renderPluginStatus(body.plugin || {});
    renderStats(body.stats || null);
    if (refillForm) fillSettingsForm(body.settings || {});
  }

  async function submitDecision(id, action, item) {
    const label = { approve: "通过", reject: "拒绝", block: "拉黑", unblock: "解除拉黑", delete: "删除" };
    const qqText = text(item && item.qq, "未知");
    let confirmText = "";
    if (action === "reject") confirmText = `确定拒绝 QQ ${qqText} 的申请吗？`;
    else if (action === "block") confirmText = `确定拉黑 QQ ${qqText} 吗？该 QQ 之后将无法申请入群。`;
    else if (action === "delete") confirmText = `确定删除这条记录吗？删除记录不等于拉黑。`;
    if (confirmText && !window.confirm(confirmText)) return;

    let out;
    if (action === "delete") {
      out = await postJson("./api/admin/delete", { csrf: state.csrf, id });
    } else {
      out = await postJson("./api/admin/decision", buildDecisionPayload(state.csrf, id, action, ""));
    }

    if (out.status === 403) {
      showAlert(out.error || "CSRF 校验失败，请刷新页面后重试。");
      return;
    }
    if (!out.ok) {
      if (out.status !== 401) showAlert(out.error || `${label[action] || "操作"}失败，请稍后重试。`);
      return;
    }

    showAlert("");
    toast(`${label[action] || "操作"}成功`);
    if (out.body && out.body.counts) renderStats(out.body.counts);
    await loadApplications();
    await loadSettings(false);
  }

  /* ---------- 事件绑定 ---------- */

  if (tbody) {
    tbody.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      const button = target.closest("button[data-action]");
      if (!button) return;
      const id = Number(button.dataset.id);
      if (!Number.isFinite(id)) return;
      const action = button.dataset.action || "";
      const item = state.items.find((entry) => Number(entry && entry.id) === id) || { id };
      button.disabled = true;
      submitDecision(id, action, item).finally(() => {
        button.disabled = false;
      });
    });
  }

  if (statusSelect) {
    statusSelect.addEventListener("change", () => {
      state.status = statusSelect.value || "all";
      state.offset = 0;
      loadApplications();
    });
  }

  if (searchInput) {
    searchInput.addEventListener("input", () => {
      if (searchTimer !== null) window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        state.q = searchInput.value.trim();
        state.offset = 0;
        loadApplications();
      }, SEARCH_DEBOUNCE_MS);
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      loadApplications();
      loadSettings(false);
    });
  }

  if (prevBtn) {
    prevBtn.addEventListener("click", () => {
      state.offset = prevOffset(state.offset, PAGE_SIZE);
      loadApplications();
    });
  }

  if (nextBtn) {
    nextBtn.addEventListener("click", () => {
      state.offset = nextOffset(state.offset, state.total, PAGE_SIZE);
      loadApplications();
    });
  }

  const reloadBtn = byId("settings-reload");
  if (reloadBtn) {
    reloadBtn.addEventListener("click", () => {
      loadSettings(true);
      toast("已重新载入设置");
    });
  }

  if (settingsForm) {
    settingsForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (settingsSubmit) {
        settingsSubmit.disabled = true;
        settingsSubmit.setAttribute("aria-busy", "true");
      }

      const payload = Object.assign({ csrf: state.csrf }, readSettingsForm());
      const changedPassword = String(payload.admin_password || "").length > 0;
      const out = await postJson("./api/admin/settings", payload);

      if (settingsSubmit) {
        settingsSubmit.disabled = false;
        settingsSubmit.setAttribute("aria-busy", "false");
      }

      if (out.status === 403) {
        showAlert(out.error || "CSRF 校验失败，请刷新页面后重试。");
        return;
      }
      if (!out.ok) {
        if (out.status !== 401) showAlert(out.error || "设置保存失败，请稍后重试。");
        return;
      }

      showAlert("");
      toast(changedPassword ? "设置已保存，管理员密码已更新" : "设置已保存");
      if (changedPassword) showAlert("管理员密码已更新，请牢记新密码。", "ok");
      const body = out.body || {};
      if (body.settings) fillSettingsForm(body.settings);
      if (body.plugin) renderPluginStatus(body.plugin);
      if (body.stats) renderStats(body.stats);
      if (!body.settings) await loadSettings(true);

      // plugin_token 后端不回显：保存成功后清空输入框
      const tokenInput = byId("plugin_token");
      if (tokenInput) tokenInput.value = "";
    });
  }

  /* ---------- 首屏渲染 ---------- */

  if (initialItems.length) {
    state.items = initialItems;
    state.total = Number.isFinite(Number(initial.total)) ? Number(initial.total) : initialItems.length;
    renderRows();
  }
  const initialCounts = initial.counts || initial.stats;
  if (initialCounts) renderStats(initialCounts);
  if (initial.plugin) renderPluginStatus(initial.plugin);
  if (initial.settings) fillSettingsForm(initial.settings);
  renderPager();

  loadApplications();
  loadSettings(true);
}

/* ------------------------------------------------------------------ *
 * 启动
 * ------------------------------------------------------------------ */

function boot() {
  try {
    initApplyPage();
  } catch (err) {
    showAlert("申请页初始化失败，请刷新页面重试。");
  }
  try {
    initLoginPage();
  } catch (err) {
    setLoginError("登录页初始化失败，请刷新页面重试。");
  }
  try {
    initAdminPage();
  } catch (err) {
    showAlert("管理后台初始化失败，请刷新页面重试。");
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
