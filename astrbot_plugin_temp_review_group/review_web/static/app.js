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
  chipsToText,
  formatTime,
  hasQuestion,
  isTerminal,
  matchModeLabel,
  nextOffset,
  normalizeQuestion,
  normalizeThreshold,
  parseListInput,
  prevOffset,
  questionSourceLabel,
  questionStats,
  relativeTime,
  sortQuestions,
  statusClass,
  statusLabel,
  validateAnswerInput,
  validateForm,
} from "./form.js";

const PAGE_SIZE = DEFAULT_PAGE_SIZE;
const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 60000;
const SEARCH_DEBOUNCE_MS = 300;
const DASH = "—";
/** 题库接口的写操作 action（后端 ./api/admin/questions 契约）。 */
const QUESTION_ACTIONS = Object.freeze({
  upsert: "upsert",
  remove: "delete",
  toggle: "toggle",
  clear: "clear",
  setAsk: "set_ask",
  setCommon: "set_common",
  setMode: "set_mode",
  setAttempts: "set_attempts",
});
/** 后端答错时的固定文案（reason 里不含参考答案）。 */
const ANSWER_REJECTION_RE = /回答不正确|答案不正确|再试一次|重新作答/;
/** 提交失败但其实是“题目变了/没作答”，此时重新取一题更合适。 */
const QUESTION_RETRY_RE = /题目|作答|回答问题/;

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
  // 网页答题区域（apply.html 里的 question-block）
  const questionBlock = byId("question-block");
  const questionTextBox = byId("question-text");
  const questionHint = byId("question-hint");
  const questionAnswer = byId("question-answer");
  const questionField = byId("question-text-hidden");
  const questionActions = byId("question-actions");
  const questionNotice = byId("question-notice");

  /** 页面上是否真的显示了题目（决定提交时要不要带 question/answer）。 */
  let questionReady = false;
  let questionLoading = false;

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
    setText("question-answer-error", "");
    if (questionAnswer) questionAnswer.removeAttribute("aria-invalid");
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

  /* ---------- 审核问题（GET ./api/questions） ---------- */

  /** 不挡路的提示条：只说明情况，不阻止提交。 */
  function setQuestionNotice(message) {
    if (!questionNotice) return;
    questionNotice.replaceChildren();
    if (!message) {
      questionNotice.hidden = true;
      return;
    }
    questionNotice.hidden = false;
    questionNotice.append(el("span", { class: "alert-text", text: message }));
  }

  function hideQuestionBlock() {
    questionReady = false;
    if (questionField) questionField.value = "";
    if (questionAnswer) {
      questionAnswer.value = "";
      questionAnswer.removeAttribute("aria-invalid");
    }
    setText("question-answer-error", "");
    if (questionActions) questionActions.replaceChildren();
    if (questionBlock) questionBlock.hidden = true;
  }

  /** 把后端下发的题目渲染进表单（绝不显示答案）。 */
  function renderQuestion(data) {
    const info = normalizeQuestion(data);
    if (!hasQuestion(info)) {
      hideQuestionBlock();
      return false;
    }

    questionReady = true;
    if (questionBlock) questionBlock.hidden = false;
    if (questionTextBox) questionTextBox.textContent = info.question;
    if (questionField) questionField.value = info.question;
    if (questionHint) {
      questionHint.textContent = info.hint;
      questionHint.hidden = !info.hint;
    }
    if (questionAnswer) {
      questionAnswer.value = "";
      questionAnswer.removeAttribute("aria-invalid");
    }
    setText("question-answer-error", "");

    if (questionActions) {
      questionActions.replaceChildren();
      // total > 1 才给「换一题」，否则换无可换
      if (info.total > 1) {
        questionActions.append(
          el("button", {
            type: "button",
            id: "question-refresh",
            class: "btn btn-small",
            text: "换一题",
            "aria-label": "换一道审核问题",
            onclick: () => {
              loadQuestion(true);
            },
          }),
        );
      }
    }
    return true;
  }

  /** 取一道题；失败时只提示，不阻止用户直接提交。 */
  async function loadQuestion(focusAnswer) {
    if (questionLoading) return;
    questionLoading = true;
    const out = await requestJson("./api/questions");
    questionLoading = false;

    if (!out.ok) {
      hideQuestionBlock();
      setQuestionNotice("审核问题加载失败，可直接提交或刷新页面。");
      return;
    }
    const body = out.body || {};
    if (body.enabled !== true) {
      hideQuestionBlock();
      setQuestionNotice("");
      return;
    }
    setQuestionNotice("");
    const shown = renderQuestion(body);
    if (shown && focusAnswer && questionAnswer && typeof questionAnswer.focus === "function") {
      questionAnswer.focus();
    }
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

  /** 是否是“审核问题答错”导致的拒绝（后端不会在 reason 里给答案）。 */
  function isAnswerRejection(data) {
    // 优先用后端给的机器可读字段（v1.1.9 起返回 answer_ok），没有再退回文案匹配
    if (data && data.answer_ok === false) return true;
    const reason = data && typeof data.reason === "string" ? data.reason : "";
    return ANSWER_REJECTION_RE.test(reason);
  }

  function renderRejected(data, status) {
    showResultCard(status, (card) => {
      card.append(el("p", { text: statusLabel(status) === "已拉黑" ? "该 QQ 已被拉黑，无法申请入群。" : "很抱歉，本次申请未通过。" }));
      card.append(el("p", { text: `原因：${text(data.reason, "未提供原因")}` }));
      if (isAnswerRejection(data)) {
        card.append(
          el("p", {
            class: "hint",
            text: "点下面的「重新填写」会重新取一道题，清空答案后可以再答一次。",
          }),
        );
      }
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

  function addRestartButton(refreshQuestion) {
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
            if (qqInput) qqInput.value = "";
            if (uidInput) uidInput.value = "";
            if (questionAnswer) questionAnswer.value = "";
            if (refreshQuestion) {
              // 答错后换一题更有意义
              loadQuestion(true).then(() => {
                if (!questionReady && qqInput && typeof qqInput.focus === "function") qqInput.focus();
              });
              return;
            }
            if (qqInput && typeof qqInput.focus === "function") qqInput.focus();
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
        addRestartButton(isAnswerRejection(data));
      }
    }, POLL_INTERVAL_MS);
  }

  function handleApplyResponse(data) {
    const payload = data || {};
    if (isTerminal(payload.status)) {
      renderResult(payload);
      addRestartButton(isAnswerRejection(payload));
      return;
    }
    renderPending();
    addRestartButton(false);
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
      question: questionReady && questionField ? questionField.value : "",
      answer: questionAnswer ? questionAnswer.value : "",
    };

    const check = validateForm(values);
    if (!check.ok) {
      showErrors(check.errors);
      setProgress("");
      return;
    }
    clearErrors();

    // 需要答题时答案不能为空：就地报错，不发起提交
    const answerError = validateAnswerInput(questionReady, values.answer);
    if (answerError) {
      setText("question-answer-error", answerError);
      if (questionAnswer) {
        questionAnswer.setAttribute("aria-invalid", "true");
        if (typeof questionAnswer.focus === "function") questionAnswer.focus();
      }
      setProgress("");
      return;
    }

    setSubmitting(true);
    setProgress("正在向 B站核验，请稍候…");

    const out = await postJson("./api/apply", buildApplyPayload(values));
    setSubmitting(false);

    if (!out.ok) {
      setProgress("");
      const message = out.error || "申请提交失败，请稍后重试。";
      showAlert(message);
      // 题目被管理员改掉 / 没带题目：顺手重新取一题，用户可直接再答
      if (QUESTION_RETRY_RE.test(message)) {
        await loadQuestion(false);
        setQuestionNotice(`${message}（已重新取题，请在审核问题区域重新作答）`);
      }
      return;
    }
    handleApplyResponse(out.body);
  });

  loadQuestion(false);
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
 * 管理后台 · 题库（网页答题）
 * ------------------------------------------------------------------ */

function initQuestionBankPanel() {
  const panel = byId("questions-panel");
  if (!panel) return;

  const statusLine = byId("questions-status");
  const askToggle = byId("ask-questions");
  const modeSelect = byId("site-match-mode");
  const thresholdInput = byId("site-fuzzy-threshold");
  const attemptsInput = byId("answer-max-attempts");
  const windowHoursInput = byId("answer-window-hours");
  const commonHost = byId("site-common-editor");
  const siteHost = byId("site-questions");
  const pluginHost = byId("plugin-questions");
  const refreshBtn = byId("questions-refresh");
  const addBtn = byId("question-add");
  const clearBtn = byId("questions-clear");
  const syncedLine = byId("plugin-synced-at");

  const csrfMeta = document.querySelector('meta[name="csrf"]');
  const bank = {
    csrf: csrfMeta ? csrfMeta.getAttribute("content") || "" : "",
    ask: true,
    source: "",
    activeCount: 0,
    siteCount: 0,
    pluginCount: 0,
    questionMode: "plugin",
    answerMaxAttempts: 3,
    answerWindowHours: 24,
    mode: "contains",
    threshold: 0.8,
    modeOptions: [],
    commonAnswers: [],
    syncedAt: 0,
    drafts: [],
    plugin: [],
    // 服务端最后一次确认的值（写操作失败时回滚控件用）
    serverAsk: true,
    serverMode: "contains",
    serverThreshold: 0.8,
    serverCommon: [],
  };

  const initial = readInitialData() || {};
  if (initial.csrf) bank.csrf = String(initial.csrf);

  let seq = 0;

  /* ---------- 小工具 ---------- */

  function nextUid() {
    seq += 1;
    return `q${seq}`;
  }

  /** 后端站点题目 / 新增草稿 → 编辑模型（answers 支持数组或字符串）。 */
  function toDraft(raw) {
    const item = raw && typeof raw === "object" ? raw : {};
    const id = Number(item.id);
    const hasId = Number.isFinite(id) && id > 0;
    return {
      uid: nextUid(),
      id: hasId ? id : 0,
      isNew: !hasId,
      source: "site",
      enabled: item.enabled !== false,
      question: text(item.question, ""),
      hint: text(item.hint, ""),
      answers: parseListInput(item.answers),
      match_mode: text(item.match_mode, "inherit"),
    };
  }

  function currentOptions() {
    const options = bank.modeOptions.length
      ? bank.modeOptions.slice()
      : ["inherit", "contains", "exact", "regex", "fuzzy"];
    return options;
  }

  function modeOptionsInto(select, current) {
    const options = currentOptions();
    if (options.indexOf(current) === -1 && current) options.push(current);
    select.replaceChildren();
    for (const mode of options) {
      const option = el("option", { value: mode, text: `${matchModeLabel(mode)}（${mode}）` });
      if (mode === current) option.selected = true;
      select.append(option);
    }
    return select;
  }

  function metaText(draft) {
    return `${draft.enabled ? "已启用" : "已停用"}｜${matchModeLabel(draft.match_mode)}｜${draft.answers.length} 个答案`;
  }

  /** 独立在 .field 之外的说明文字：补上 style.css 里 .field .hint 的次要样式。 */
  function hintLine(message) {
    return el("p", {
      class: "hint",
      style: "color:var(--text-muted);font-size:0.86rem;margin:6px 0",
      text: message,
    });
  }

  function labeled(labelText, control) {
    return el("div", { class: "field" }, [
      el("span", { class: "section-title", text: labelText }),
      control,
    ]);
  }

  /**
   * 答案 chips 编辑器：直接读写传入的数组，变更后回调 onChange。
   * 只用 style.css 里已有的类名 + 内联样式拼装。
   */
  function chipsEditor(values, options) {
    const opts = options || {};
    const host = el("div", { class: "row", role: "list", "aria-label": opts.label || "答案" });
    const input = el("input", {
      type: "text",
      autocomplete: "off",
      placeholder: opts.placeholder || "输入后按回车或点「添加」",
      "aria-label": `${opts.label || "答案"}：新增一项`,
    });
    const addButton = el("button", { type: "button", class: "btn btn-small", text: "添加" });
    const errorText = el("p", { class: "field-error", role: "alert" });

    const renderChips = () => {
      host.replaceChildren();
      if (!values.length) {
        host.append(el("span", { class: "hint", text: opts.emptyText || "还没有内容" }));
        return;
      }
      values.forEach((value, index) => {
        const chip = el("span", {
          class: "badge badge-unknown",
          role: "listitem",
          style: "display:inline-flex;align-items:center;gap:6px;padding:3px 6px 3px 9px;margin:0 6px 6px 0",
        });
        chip.append(el("span", { text: String(value) }));
        chip.append(
          el("button", {
            type: "button",
            class: "btn btn-small btn-danger",
            style: "min-height:24px;padding:2px 8px;font-size:0.78rem",
            "aria-label": `删除：${String(value)}`,
            text: "✕",
            onclick: () => {
              values.splice(index, 1);
              renderChips();
              if (typeof opts.onChange === "function") opts.onChange(values.slice());
            },
          }),
        );
        host.append(chip);
      });
    };

    const add = () => {
      const incoming = parseListInput(input.value);
      if (!incoming.length) {
        errorText.textContent = "请输入内容";
        return;
      }
      let added = 0;
      for (const item of incoming) {
        if (values.indexOf(item) === -1) {
          values.push(item);
          added += 1;
        }
      }
      input.value = "";
      errorText.textContent = added ? "" : "这项已经存在";
      renderChips();
      if (added && typeof opts.onChange === "function") opts.onChange(values.slice());
    };

    addButton.addEventListener("click", add);
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        add();
      }
    });

    renderChips();
    return el("div", { class: "stack" }, [
      host,
      el("div", { class: "row" }, [input, addButton]),
      errorText,
    ]);
  }

  /* ---------- 渲染 ---------- */

  function renderStatus() {
    if (statusLine) {
      const sourceLabel = questionSourceLabel(bank.source) || "无可用题目";
      const modeText = bank.questionMode === "site"
        ? "题目以网站题库为准（插件会把这里维护的题目同步回 QQ）"
        : "题目以插件题库为准（站点题库暂不参与出题）";
      const limitText = bank.answerMaxAttempts > 0
        ? `答错上限 ${bank.answerMaxAttempts} 次/${bank.answerWindowHours} 小时`
        : "答错次数不限";
      statusLine.textContent =
        `${modeText}。当前出题来源：${sourceLabel}（站点题库 ${bank.siteCount} 条 / 插件题库 ${bank.pluginCount} 条，` +
        `可用题目 ${bank.activeCount} 道）；网页答题${bank.ask ? "已开启" : "已关闭"}；${limitText}。`;
    }
    if (syncedLine) {
      syncedLine.textContent = bank.syncedAt
        ? `插件题库同步时间：${formatTime(bank.syncedAt)}（${relativeTime(bank.syncedAt)}）`
        : "插件题库同步时间：尚未同步";
    }
  }

  function renderCommonEditor() {
    if (!commonHost) return;
    commonHost.replaceChildren(
      chipsEditor(bank.commonAnswers, {
        label: "站点通用答案库",
        placeholder: "输入通用答案后按回车",
        emptyText: "还没有通用答案",
        onChange: () => {
          submitBank({
            action: QUESTION_ACTIONS.setCommon,
            common_answers: bank.commonAnswers.slice(),
          });
        },
      }),
    );
  }

  // 答题错误次数上限 / 统计窗口（同一个 action 提交）
  const submitAttempts = () => {
    const limit = Math.max(0, Math.min(50, Math.floor(Number(attemptsInput && attemptsInput.value)) || 0));
    const hours = Math.max(1, Math.min(720, Math.floor(Number(windowHoursInput && windowHoursInput.value)) || 24));
    if (attemptsInput) attemptsInput.value = String(limit);
    if (windowHoursInput) windowHoursInput.value = String(hours);
    submitBank({ action: QUESTION_ACTIONS.setAttempts, answer_max_attempts: limit, answer_window_hours: hours });
  };
  if (attemptsInput) attemptsInput.addEventListener("change", submitAttempts);
  if (windowHoursInput) windowHoursInput.addEventListener("change", submitAttempts);

  function renderGlobalControls() {
    if (askToggle) askToggle.checked = bank.ask === true;
    if (modeSelect) modeOptionsInto(modeSelect, bank.mode);
    if (attemptsInput) attemptsInput.value = String(bank.answerMaxAttempts);
    if (windowHoursInput) windowHoursInput.value = String(bank.answerWindowHours);
    if (thresholdInput) thresholdInput.value = String(normalizeThreshold(bank.threshold));
    renderCommonEditor();
  }

  function buildPluginCard(raw) {
    const item = raw && typeof raw === "object" ? raw : {};
    const answers = parseListInput(item.answers);
    const details = el("details", {
      style: "border:1px dashed var(--border);border-radius:8px;padding:10px;margin:0 0 12px",
    });
    const summary = el("summary", { style: "cursor:pointer" }, [
      el("span", { style: "font-weight:600", text: text(item.question, "（未填写题干）") }),
      el("span", {
        class: "muted small",
        style: "margin-left:8px",
        text: `插件题库｜${matchModeLabel(item.match_mode)}｜${answers.length} 个答案｜${item.enabled === false ? "已停用" : "已启用"}`,
      }),
    ]);
    const body = el("div", { class: "stack", style: "margin-top:10px" }, [
      el("dl", { class: "kv" }, [
        el("dt", { text: "答案" }),
        el("dd", { text: answers.length ? answers.join("、") : DASH }),
        el("dt", { text: "提示" }),
        el("dd", { text: text(item.hint) }),
      ]),
      el("p", { class: "hint", text: "来自 QQ 插件（改它请到 AstrBot 插件配置）。" }),
      hintLine("只读展示：插件题目在这里不提供编辑或删除按钮。"),
    ]);
    details.append(summary, body);
    return details;
  }

  function buildSiteCard(draft) {
    const details = el("details", {
      id: `site-item-${draft.uid}`,
      style: "border:1px solid var(--border);border-radius:8px;padding:10px;margin:0 0 12px",
    });
    details.open = draft.open === true;
    details.addEventListener("toggle", () => {
      draft.open = details.open;
    });

    const summaryTitle = el("span", {
      style: "font-weight:600",
      text: draft.question || "（未填写题干）",
    });
    const summaryMeta = el("span", { class: "muted small", style: "margin-left:8px" });
    summaryMeta.textContent = metaText(draft);
    const summary = el("summary", { style: "cursor:pointer" }, [summaryTitle, summaryMeta]);

    const questionArea = el("textarea", {
      id: `site-question-${draft.uid}`,
      rows: "2",
      maxlength: "300",
      "aria-label": "题干",
    });
    questionArea.value = draft.question;
    questionArea.addEventListener("input", () => {
      draft.question = questionArea.value;
      summaryTitle.textContent = draft.question || "（未填写题干）";
    });

    const hintInput = el("input", {
      id: `site-hint-${draft.uid}`,
      type: "text",
      maxlength: "200",
      autocomplete: "off",
      "aria-label": "提示",
    });
    hintInput.value = draft.hint;
    hintInput.addEventListener("input", () => {
      draft.hint = hintInput.value;
    });

    const enabledBox = el("input", { id: `site-enabled-${draft.uid}`, type: "checkbox" });
    enabledBox.checked = draft.enabled !== false;
    enabledBox.addEventListener("change", () => {
      draft.enabled = enabledBox.checked === true;
      summaryMeta.textContent = metaText(draft);
    });

    const answersEditor = chipsEditor(draft.answers, {
      label: "答案",
      placeholder: "输入答案后按回车",
      emptyText: "至少填一个答案",
      onChange: () => {
        summaryMeta.textContent = metaText(draft);
      },
    });

    const modeField = modeOptionsInto(el("select", { "aria-label": "匹配方式" }), draft.match_mode);
    modeField.addEventListener("change", () => {
      draft.match_mode = modeField.value;
      summaryMeta.textContent = metaText(draft);
    });

    const body = el("div", { class: "stack", style: "margin-top:10px" }, [
      labeled("题干（必填，最多 300 字）", questionArea),
      labeled("提示（可选）", hintInput),
      labeled("答案（参考答案，可多个）", answersEditor),
      labeled("匹配方式", modeField),
      el("div", { class: "checkbox" }, [
        enabledBox,
        el("label", { for: `site-enabled-${draft.uid}`, text: "启用这道题" }),
      ]),
    ]);

    const saveBtn = el("button", {
      type: "button",
      class: "btn btn-primary btn-small",
      text: draft.isNew ? "新增" : "保存",
    });
    saveBtn.addEventListener("click", () => {
      const payload = {
        action: QUESTION_ACTIONS.upsert,
        question: draft.question,
        hint: draft.hint,
        answers: draft.answers.slice(),
        match_mode: draft.match_mode,
        enabled: draft.enabled !== false,
      };
      if (draft.id) payload.id = draft.id;
      submitBank(payload, saveBtn);
    });

    const deleteBtn = el("button", {
      type: "button",
      class: "btn btn-danger btn-small",
      text: "删除",
    });
    deleteBtn.addEventListener("click", () => {
      if (!draft.id) {
        bank.drafts = bank.drafts.filter((item) => item !== draft);
        renderLists();
        return;
      }
      const label = draft.question ? `「${draft.question}」` : `#${draft.id}`;
      if (!window.confirm(`确定删除这道题吗？${label}`)) return;
      submitBank({ action: QUESTION_ACTIONS.remove, id: draft.id }, deleteBtn);
    });

    body.append(el("div", { class: "btn-row" }, [saveBtn, deleteBtn]));
    details.append(summary, body);
    return details;
  }

  function renderLists() {
    if (siteHost) siteHost.replaceChildren();
    if (pluginHost) pluginHost.replaceChildren();

    // sortQuestions：站点题在前、插件题在后，各自保持后端给的顺序
    for (const item of sortQuestions(bank.drafts.concat(bank.plugin))) {
      const isPlugin =
        item && typeof item === "object" && String(item.source || "").toLowerCase() === "plugin";
      if (isPlugin) {
        if (pluginHost) pluginHost.append(buildPluginCard(item));
      } else if (siteHost) {
        siteHost.append(buildSiteCard(item));
      }
    }

    if (siteHost && !bank.drafts.length) {
      siteHost.append(
        hintLine(
          "站点题库为空时，网页出的题来自插件同步的题库（也可以点「新增题目」自己加）。" +
            "注意：题目来源由 AstrBot 插件配置里的「网页出题以谁的题库为准」决定——选 plugin 时以插件题库为准，选 site 时才以这里的站点题库为准。",
        ),
      );
    }
    if (pluginHost && !bank.plugin.length) {
      pluginHost.append(hintLine("插件题库暂时没有题目，插件同步后会显示在这里。"));
    }
  }

  function renderAll() {
    renderStatus();
    renderGlobalControls();
    renderLists();
  }

  /** 用 ./api/admin/questions 的返回值刷新整个分区。 */
  function applyBank(data) {
    const source = data && typeof data === "object" ? data : {};
    if (source.csrf) bank.csrf = String(source.csrf);
    const stats = questionStats(source);
    bank.ask = stats.askEnabled;
    bank.source = stats.source;
    bank.activeCount = stats.activeCount;
    bank.siteCount = stats.siteCount;
    bank.pluginCount = stats.pluginCount;
    bank.mode = text(source.site_match_mode, "contains");
    bank.threshold = normalizeThreshold(source.site_fuzzy_threshold);
    // 「站点默认匹配方式」不能用 inherit（那等于"跟随全局"而它本身就是全局），
    // 所以优先用后端单独下发的 default_mode_options；没有就从总选项里剔除 inherit。
    const rawOptions = Array.isArray(source.default_mode_options) && source.default_mode_options.length
      ? source.default_mode_options
      : Array.isArray(source.match_mode_options)
        ? source.match_mode_options
        : [];
    bank.modeOptions = rawOptions
      .map((item) => String(item))
      .filter((item) => item && item !== "inherit");
    bank.commonAnswers = parseListInput(source.site_common_answers);
    // 记住服务端最后一次确认的值，写操作失败时用它把控件恢复原样
    bank.serverCommon = bank.commonAnswers.slice();
    bank.serverMode = bank.mode;
    bank.serverThreshold = bank.threshold;
    bank.serverAsk = bank.ask;
    bank.syncedAt = Number(source.plugin_synced_at) || 0;
    bank.questionMode = text(source.question_mode, "plugin") === "site" ? "site" : "plugin";
    bank.answerMaxAttempts = Number(source.answer_max_attempts);
    if (!Number.isFinite(bank.answerMaxAttempts) || bank.answerMaxAttempts < 0) bank.answerMaxAttempts = 3;
    bank.answerWindowHours = Number(source.answer_window_hours);
    if (!Number.isFinite(bank.answerWindowHours) || bank.answerWindowHours < 1) bank.answerWindowHours = 24;
    // 局部刷新时尽量保留已保存题目的展开状态（重渲染会重建 DOM）
    const previous = new Map();
    for (const item of bank.drafts) {
      if (item.id) previous.set(String(item.id), item);
    }
    bank.drafts = (Array.isArray(source.site) ? source.site : []).map((raw) => {
      const draft = toDraft(raw);
      const old = draft.id ? previous.get(String(draft.id)) : null;
      if (old) {
        draft.uid = old.uid;
        draft.open = old.open === true;
      }
      return draft;
    });
    bank.plugin = Array.isArray(source.plugin) ? source.plugin : [];
    renderAll();
  }

  async function submitBank(payload, button) {
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    }
    const out = await postJson(
      "./api/admin/questions",
      Object.assign({ csrf: bank.csrf }, payload),
    );
    if (button) {
      button.disabled = false;
      button.setAttribute("aria-busy", "false");
    }

    if (out.status === 403) {
      showAlert(out.error || "CSRF 校验失败，请刷新页面后重试。");
      restoreControls(payload);
      return;
    }
    if (!out.ok) {
      if (out.status !== 401) showAlert(out.error || "题库保存失败，请稍后重试。");
      restoreControls(payload);
      return;
    }
    showAlert("");
    applyBank(out.body || {});
    toast("题库已更新");
  }

  /**
   * 写操作失败时把「出题设置」里的控件恢复成服务端最后一次确认的值。
   * 典型场景：站点默认匹配方式不允许选「跟随全局」，后端会返回 400。
   */
  function restoreControls(payload) {
    const action = payload && payload.action;
    if (action === QUESTION_ACTIONS.setAsk) {
      bank.ask = bank.serverAsk === undefined ? bank.ask : bank.serverAsk;
    } else if (action === QUESTION_ACTIONS.setMode) {
      bank.mode = bank.serverMode === undefined ? bank.mode : bank.serverMode;
      bank.threshold = bank.serverThreshold === undefined ? bank.threshold : bank.serverThreshold;
    } else if (action === QUESTION_ACTIONS.setCommon) {
      bank.commonAnswers = (bank.serverCommon || []).slice();
    } else {
      return; // 题目卡片里的修改保留在草稿里，方便用户改完再存
    }
    renderGlobalControls();
  }

  async function loadBank() {
    if (statusLine) statusLine.textContent = "正在加载题库…";
    const out = await requestJson("./api/admin/questions");
    if (!out.ok) {
      if (out.status !== 401) {
        const message = out.error || "题库加载失败，请稍后重试。";
        if (statusLine) statusLine.textContent = message;
        showAlert(message);
      }
      return;
    }
    applyBank(out.body || {});
  }

  /* ---------- 事件绑定 ---------- */

  if (askToggle) {
    askToggle.addEventListener("change", () => {
      submitBank({ action: QUESTION_ACTIONS.setAsk, enabled: askToggle.checked === true });
    });
  }

  if (modeSelect) {
    modeSelect.addEventListener("change", () => {
      submitBank({
        action: QUESTION_ACTIONS.setMode,
        match_mode: modeSelect.value,
        fuzzy_threshold: normalizeThreshold(thresholdInput ? thresholdInput.value : bank.threshold),
      });
    });
  }

  if (thresholdInput) {
    thresholdInput.addEventListener("change", () => {
      submitBank({
        action: QUESTION_ACTIONS.setMode,
        match_mode: modeSelect ? modeSelect.value : bank.mode,
        fuzzy_threshold: normalizeThreshold(thresholdInput.value),
      });
    });
  }

  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      loadBank();
      toast("正在刷新题库");
    });
  }

  if (addBtn) {
    addBtn.addEventListener("click", () => {
      const draft = toDraft({ enabled: true, match_mode: "inherit" });
      draft.isNew = true;
      bank.drafts.push(draft);
      renderLists();
      const details = siteHost ? siteHost.querySelector(`#site-item-${draft.uid}`) : null;
      if (details) {
        details.open = true;
        const area = byId(`site-question-${draft.uid}`);
        if (area && typeof area.focus === "function") area.focus();
      }
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      if (!window.confirm("确定清空站点题库吗？会删除全部站点题目（插件同步过来的题目不受影响）。")) return;
      submitBank({ action: QUESTION_ACTIONS.clear }, clearBtn);
    });
  }

  loadBank();
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
  try {
    initQuestionBankPanel();
  } catch (err) {
    showAlert("题库面板初始化失败，请刷新页面重试。");
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
