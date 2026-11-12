/* ==========================================================================
 * fnxunlei 前端逻辑
 *
 * 只有两个页面：
 *   推送 —— 粘贴链接推送到迅雷远程设备
 *   设置 —— 迅雷账号登录 / 登录后选择目标设备与保存目录
 *
 * 界面结构沿用 fnwifi：左侧导航 + 右侧卡片，主题跟随飞牛设置。
 * ========================================================================== */

const API = "api";
const PASSWORD_MASK = "********";

const state = {
  page: "push",
  app: {},
  config: {},
  status: { configured: false, reachable: false, detail: "", devices: [], review: {} },
  review: { active: false, mobile: "" },
  devices: [],
  loaded: false,
  busy: false,
  preview: { total: 0, dispatchable: 0 },
  showPassword: false,
  dir: {
    path: "",
    manual: false,
    browse: { ok: true, loading: false, error: "", dirs: [], parent: null, root: "" },
  },
};

/* ---------------------------- 页面元素引用 ---------------------------- */
const els = {
  footState: document.getElementById("footState"),
  footText: document.getElementById("footText"),
  // 推送
  loginBanner: document.getElementById("loginBanner"),
  linkInput: document.getElementById("linkInput"),
  linkHint: document.getElementById("linkHint"),
  dispatchBtn: document.getElementById("dispatchBtn"),
  clearLinks: document.getElementById("clearLinks"),
  resultCard: document.getElementById("resultCard"),
  resultList: document.getElementById("resultList"),
  resultSummary: document.getElementById("resultSummary"),
  // 设置
  accountForm: document.getElementById("accountForm"),
  passwordInput: document.getElementById("passwordInput"),
  togglePwdBtn: document.getElementById("togglePwdBtn"),
  accountBadge: document.getElementById("accountBadge"),
  accountState: document.getElementById("accountState"),
  loginBtn: document.getElementById("loginBtn"),
  logoutBtn: document.getElementById("logoutBtn"),
  // 下载位置（登录后显示）
  locationCard: document.getElementById("locationCard"),
  locationBadge: document.getElementById("locationBadge"),
  targetSelect: document.getElementById("targetSelect"),
  dirBrowse: document.getElementById("dirBrowse"),
  dirSelect: document.getElementById("dirSelect"),
  dirText: document.getElementById("dirText"),
  dirState: document.getElementById("dirState"),
  locationState: document.getElementById("locationState"),
  refreshDevices: document.getElementById("refreshDevices"),
  refreshDir: document.getElementById("refreshDir"),
  saveLocation: document.getElementById("saveLocation"),
  // 通用
  modal: document.getElementById("modal"),
  modalTitle: document.getElementById("modalTitle"),
  modalBody: document.getElementById("modalBody"),
  modalOk: document.getElementById("modalOk"),
  modalCancel: document.getElementById("modalCancel"),
  reviewModal: document.getElementById("reviewModal"),
  reviewTip: document.getElementById("reviewTip"),
  smsSendBtn: document.getElementById("smsSendBtn"),
  smsCodeInput: document.getElementById("smsCodeInput"),
  smsCheckBtn: document.getElementById("smsCheckBtn"),
  reviewCancel: document.getElementById("reviewCancel"),
  toast: document.getElementById("toast"),
};

/* ------------------------------ 基础工具 ------------------------------ */
function escapeHtml(value) {
  return String(value === null || value === undefined ? "" : value).replace(
    /[&<>"']/g,
    (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
  );
}

function apiUrl(action) {
  const [name, query] = String(action).split("?");
  return `${API}/${encodeURIComponent(name)}?${query || ""}`;
}

async function api(action, { method = "GET", data = null } = {}) {
  const options = { method, cache: "no-store" };
  if (data) {
    options.method = method === "GET" ? "POST" : method;
    options.headers = { "Content-Type": "application/x-www-form-urlencoded" };
    options.body = new URLSearchParams(data);
  }
  const response = await fetch(apiUrl(action), options);
  let result;
  try {
    result = await response.json();
  } catch (error) {
    throw new Error(`服务返回异常（HTTP ${response.status}）`);
  }
  if (!response.ok || result.ok === false) {
    throw new Error(result.error || result.message || `HTTP ${response.status}`);
  }
  return result;
}

function fmtSize(bytes) {
  const value = Number(bytes) || 0;
  if (value <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let size = value;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return index === 0 ? `${size.toFixed(0)} ${units[index]}` : `${size.toFixed(1)} ${units[index]}`;
}

function shortUrl(url) {
  const text = String(url || "");
  return text.length > 64 ? `${text.slice(0, 61)}…` : text;
}

function maskMobile(mobile) {
  return String(mobile || "").replace(/^(\d{3})\d+(\d{2})$/, "$1****$2");
}

function setText(el, text) {
  if (el) el.textContent = text;
}

/* ============================ 页面切换 ============================ */
const PAGES = ["push", "settings"];
const NAV_IDS = { push: "navPush", settings: "navSettings" };
const PAGE_IDS = { push: "page-push", settings: "page-settings" };

function hashPage() {
  const raw = String(window.location.hash || "").replace(/^#/, "").trim();
  return PAGES.includes(raw) ? raw : "";
}

function switchPage(name) {
  if (!PAGES.includes(name)) return;
  state.page = name;
  try {
    if (window.history && window.history.replaceState) {
      window.history.replaceState(null, "", `#${name}`);
    }
  } catch (error) {
    // 内嵌环境可能禁止改 history，忽略
  }
  PAGES.forEach((page) => {
    const nav = document.getElementById(NAV_IDS[page]);
    const section = document.getElementById(PAGE_IDS[page]);
    if (nav) nav.classList.toggle("active", page === name);
    if (section) section.classList.toggle("active", page === name);
  });
  if (name === "settings" && state.status.reachable && !state.dir.browse.loading) {
    // 首次进入设置页时按已保存的目录定位
    if (!state.dir.path && !state.dir.manual && !state.dir.browse.root) {
      loadDirs(state.config.dir || "").catch(() => { });
    }
  }
}

/* ============================ 数据加载 ============================ */
async function boot() {
  setBusy(true);
  try {
    const configRes = await api("config_get");
    state.app = configRes.app || {};
    state.config = configRes.config || {};
    state.review = configRes.review || { active: false, mobile: "" };
    renderAccount();
    await refreshStatus();
    if (state.status.reachable) {
      await loadDirs(state.config.dir || "").catch(() => { });
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.loaded = true;
    setBusy(false);
    updatePreview();
  }
}

async function refreshStatus() {
  const res = await api("status");
  applyLoginResult(res);
}

function applyLoginResult(res) {
  state.status = res.status || state.status;
  state.config = res.config || state.config;
  state.app = res.app || state.app;
  state.review = state.status.review || state.review || { active: false, mobile: "" };
  state.devices = state.status.devices || state.devices || [];
  renderAccount();
  renderLocation();
  renderPushHints();
}

/* ============================ 渲染：账号 ============================ */
function renderAccount() {
  const config = state.config || {};
  const status = state.status || {};

  els.accountForm.elements.login_name.value = config.login_name || "";
  els.passwordInput.value = config.has_password ? PASSWORD_MASK : "";
  els.passwordInput.dataset.masked = config.has_password ? "1" : "0";
  els.passwordInput.type = state.showPassword ? "text" : "password";
  els.togglePwdBtn.classList.toggle("eye-active", state.showPassword);

  if (status.reachable) {
    els.accountBadge.className = "badge badge-ok";
    els.accountBadge.textContent = "已登录";
    setText(els.accountState, status.user ? `已登录：${status.user}` : "已登录");
    els.accountState.className = "form-state ok";
  } else if (status.configured) {
    els.accountBadge.className = "badge badge-warn";
    els.accountBadge.textContent = "未登录";
    setText(els.accountState, "登录失效或需要重新登录");
    els.accountState.className = "form-state bad";
  } else {
    els.accountBadge.className = "badge badge-waiting";
    els.accountBadge.textContent = "未配置";
    setText(els.accountState, "填写账号密码后点「登录」");
    els.accountState.className = "form-state";
  }
  els.loginBtn.textContent = status.reachable ? "重新登录" : "登录";

  setText(els.footText, status.reachable ? "已登录" : "未登录");
  els.footState.className = status.reachable ? "dot-ok" : status.configured ? "dot-bad" : "dot-idle";

  renderPushHints();
}

/* ============================ 渲染：推送页提示 ============================ */
function renderPushHints() {
  const reachable = Boolean(state.status.reachable);
  els.loginBanner.classList.toggle("hidden", reachable);
  els.dispatchBtn.disabled = state.busy || !reachable || state.preview.dispatchable === 0;
}

/* ============================ 渲染：下载位置 ============================ */
function renderLocation() {
  const visible = Boolean(state.status.reachable);
  els.locationCard.classList.toggle("hidden", !visible);
  if (!visible) return;

  const devices = state.devices.length ? state.devices : state.status.devices || [];
  const saved = (state.config.target || "").trim();
  const online = devices.map((device) => device.target || "");
  // 没有保存过目标时，默认选中第一台在线设备
  const selected = saved || (online.length ? online[0] : "");

  const options = [];
  devices.forEach((device) => {
    const value = device.target || "";
    const name = (device.name || "远程设备").trim();
    options.push(`<option value="${escapeHtml(value)}">${escapeHtml(name)}</option>`);
  });
  if (saved && !online.includes(saved)) {
    options.push(`<option value="${escapeHtml(saved)}">已指定的设备（当前离线）</option>`);
  }
  if (!options.length) {
    options.push('<option value="">未发现远程设备</option>');
  }
  els.targetSelect.innerHTML = options.join("");
  els.targetSelect.value = selected;

  els.locationBadge.textContent = devices.length ? `远程设备 ${devices.length} 台` : "未发现设备";
  renderDirBrowser();
}

function renderDirBrowser() {
  const dir = state.dir;
  const browse = dir.browse;

  if (browse.loading) {
    setText(els.dirState, "正在读取设备目录…");
  }

  if (dir.manual || !browse.ok) {
    els.dirBrowse.classList.add("hidden");
    els.dirText.classList.remove("hidden");
    if (document.activeElement !== els.dirText) {
      els.dirText.value = dir.path;
    }
    if (!browse.loading) {
      setText(els.dirState,
        `无法读取设备目录：${browse.error || "未知原因"}。可手动输入相对路径（如 movies/2025），留空表示设备下载根目录。`);
    }
    return;
  }

  els.dirBrowse.classList.remove("hidden");
  els.dirText.classList.add("hidden");

  const items = browse.dirs || [];
  const opts = ['<option value="">根目录（设备默认下载目录）</option>'];
  if (dir.path) {
    // 当前目录单独列出并选中，下拉框才能显示出「现在选的是哪一层」
    opts.push(`<option value="${escapeHtml(dir.path)}">当前：${escapeHtml(dir.path)}</option>`);
    opts.push('<option value="..">↑ 上一级</option>');
  }
  items.forEach((item) => {
    opts.push(`<option value="${escapeHtml(item.path)}">${escapeHtml(item.name)}</option>`);
  });
  els.dirSelect.innerHTML = opts.join("");
  els.dirSelect.value = dir.path || "";

  if (!browse.loading) {
    const rootPath = browse.root || "/";
    const actual = dir.path ? `${rootPath}${dir.path}/` : rootPath;
    setText(els.dirState,
      `实际路径：${actual}${browse.truncated ? "（目录较多，仅显示前 300 个）" : ""}`);
  }
}

async function loadDirs(path) {
  state.dir.path = String(path || "").replace(/^\/+|\/+$/g, "");
  state.dir.browse = { ok: true, loading: true, error: "", dirs: [], parent: null, root: "" };
  renderDirBrowser();
  try {
    const res = await api("dirs", {
      method: "POST",
      data: { path: state.dir.path, target: els.targetSelect.value || "" },
    });
    if (res.browse_ok === false) {
      state.dir.manual = true;
      state.dir.browse = { ok: false, loading: false, error: res.error || "读取失败", dirs: [], parent: null, root: "" };
    } else {
      state.dir.manual = false;
      state.dir.path = res.path || "";
      state.dir.browse = {
        ok: true,
        loading: false,
        error: "",
        dirs: res.dirs || [],
        parent: res.parent,
        root: res.root || "",
      };
    }
  } catch (error) {
    state.dir.manual = true;
    state.dir.browse = { ok: false, loading: false, error: error.message, dirs: [], parent: null, root: "" };
  }
  renderDirBrowser();
}

/* ============================ 交互：账号 ============================ */
function currentPassword() {
  const value = els.passwordInput.value;
  if (els.passwordInput.dataset.masked === "1" && value === PASSWORD_MASK) {
    return "******";
  }
  return value;
}

async function saveAccount(showToastAfter = true) {
  const res = await api("config_set", {
    method: "POST",
    data: {
      login_name: els.accountForm.elements.login_name.value.trim(),
      password: currentPassword(),
    },
  });
  state.config = res.config || state.config;
  renderAccount();
  if (showToastAfter) showToast("已保存");
}

async function doLogin() {
  if (state.busy) return false;
  setBusy(true);
  els.loginBtn.textContent = "登录中...";
  try {
    await saveAccount(false);
    const res = await api("login", {
      method: "POST",
      data: {
        login_name: els.accountForm.elements.login_name.value.trim(),
        password: currentPassword(),
      },
    });
    applyLoginResult(res);
    closeReviewModal();
    showToast("登录成功");
    renderLocation();
    await loadDirs(state.config.dir || "").catch(() => { });
    return true;
  } catch (error) {
    const message = error.message || "";
    await refreshStatus().catch(() => { });
    if (state.review.active) {
      openReviewModal();
    } else {
      showToast(message, true);
    }
    return false;
  } finally {
    setBusy(false);
    els.loginBtn.textContent = state.status.reachable ? "重新登录" : "登录";
  }
}

async function logout() {
  const ok = await confirmDialog("退出登录", "将清空本机保存的登录令牌（账号密码保留），确定退出？");
  if (!ok) return;
  try {
    await api("logout", { method: "POST", data: { nothing: "1" } });
    state.review = { active: false, mobile: "" };
    state.devices = [];
    state.dir = { path: "", manual: false, browse: { ok: true, loading: false, error: "", dirs: [], parent: null, root: "" } };
    showToast("已退出登录");
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
  }
}

/* ============================ 交互：短信验证弹窗 ============================ */
function openReviewModal() {
  const review = state.review || {};
  const lines = ["本次登录触发迅雷风控，需要短信验证一次。"];
  if (review.mobile) lines.push(`手机号：${maskMobile(review.mobile)}`);
  lines.push("点「获取验证码」→ 输入短信里的 6 位数字 → 「提交验证」，验证通过会自动重新登录。");
  setText(els.reviewTip, lines.join("\n"));
  els.reviewModal.classList.remove("hidden");
  els.smsCodeInput.value = "";
  setTimeout(() => els.smsCodeInput.focus(), 50);
}

function closeReviewModal() {
  els.reviewModal.classList.add("hidden");
}

let smsCountdownTimer = null;

function startSmsCountdown(seconds) {
  clearInterval(smsCountdownTimer);
  let left = seconds;
  els.smsSendBtn.disabled = true;
  els.smsSendBtn.textContent = `${left}s 后重发`;
  smsCountdownTimer = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      clearInterval(smsCountdownTimer);
      els.smsSendBtn.disabled = false;
      els.smsSendBtn.textContent = "重新获取";
      return;
    }
    els.smsSendBtn.textContent = `${left}s 后重发`;
  }, 1000);
}

async function sendSms() {
  if (state.busy) return;
  setBusy(true);
  els.smsSendBtn.textContent = "发送中...";
  try {
    const res = await api("review_send", { method: "POST", data: { nothing: "1" } });
    state.review = res.review || state.review;
    showToast(res.detail || "验证码已发送");
    startSmsCountdown(60);
    els.smsCodeInput.focus();
  } catch (error) {
    showToast(error.message, true);
    els.smsSendBtn.disabled = false;
    els.smsSendBtn.textContent = "获取验证码";
  } finally {
    setBusy(false);
  }
}

async function checkSms() {
  const code = els.smsCodeInput.value.trim();
  if (!/^\d{6}$/.test(code)) {
    showToast("请输入 6 位数字验证码", true);
    return;
  }
  if (state.busy) return;
  setBusy(true);
  els.smsCheckBtn.textContent = "验证中...";
  let passed = false;
  try {
    const res = await api("review_check", { method: "POST", data: { code } });
    state.review = res.review || { active: false, mobile: "" };
    closeReviewModal();
    showToast(res.detail || "验证通过，正在重新登录…");
    passed = true;
  } catch (error) {
    showToast(error.message, true);
  } finally {
    els.smsCheckBtn.textContent = "提交验证";
    setBusy(false);
  }
  if (passed) {
    // 验证通过后自动重新登录；仍被风控会再次弹出本弹窗
    await doLogin();
  }
}

/* ============================ 交互：下载位置 ============================ */
async function saveLocation() {
  if (state.busy) return;
  const target = els.targetSelect.value;
  const manual = state.dir.manual;
  let dir = manual ? els.dirText.value.trim() : state.dir.path;
  dir = String(dir || "").replace(/^\/+|\/+$/g, "");

  const option = els.targetSelect.selectedOptions && els.targetSelect.selectedOptions[0];
  const deviceName = option ? option.textContent.trim() : "";
  const where = dir ? `${deviceName ? `${deviceName} / ` : ""}${dir}` : `${deviceName || "设备"} / 根目录`;

  els.saveLocation.disabled = true;
  els.saveLocation.textContent = "保存中...";
  els.locationState.className = "form-state";
  setText(els.locationState, "正在保存…");
  try {
    const res = await api("config_set", { method: "POST", data: { target, dir } });
    state.config = res.config || state.config;
    state.dir.path = dir;
    renderAccount();
    renderLocation();
    els.locationState.className = "form-state ok";
    setText(els.locationState, `已保存：${where}`);
    showToast(`下载位置已保存（${where}）`);
  } catch (error) {
    els.locationState.className = "form-state bad";
    setText(els.locationState, error.message);
    showToast(error.message, true);
  } finally {
    els.saveLocation.disabled = false;
    els.saveLocation.textContent = "保存";
  }
}

/* ============================ 交互：推送 ============================ */
let previewTimer = null;

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 250);
}

async function updatePreview() {
  const text = els.linkInput.value.trim();
  if (!text) {
    state.preview = { total: 0, dispatchable: 0 };
    setText(els.linkHint, "支持 magnet / thunder / http(s) / ftp，单次最多 20 条。");
    els.dispatchBtn.disabled = true;
    return;
  }
  try {
    const res = await api("parse", { method: "POST", data: { text } });
    const links = res.links || [];
    const ok = links.filter((item) => item.dispatchable).length;
    state.preview = { total: links.length, dispatchable: ok };
    setText(
      els.linkHint,
      `识别到 ${links.length} 条链接：${ok} 条可推送${links.length - ok ? `，${links.length - ok} 条不可推送` : ""}。`,
    );
  } catch (error) {
    state.preview = { total: 0, dispatchable: 0 };
  }
  renderPushHints();
}

async function dispatchLinks() {
  const text = els.linkInput.value.trim();
  if (!text) {
    showToast("请先粘贴下载链接", true);
    return;
  }
  setBusy(true);
  els.dispatchBtn.textContent = "推送中...";
  try {
    const res = await api("add", { method: "POST", data: { text } });
    renderResults(res.results);
    showToast(
      res.success === res.total ? `已推送 ${res.success} 条` : `已推送 ${res.success} / ${res.total} 条`,
      res.success === 0,
    );
    if (res.success > 0) {
      els.linkInput.value = "";
      updatePreview();
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
    els.dispatchBtn.textContent = "推送下载";
    renderPushHints();
  }
}

function renderResults(results) {
  const list = results || [];
  if (!list.length) {
    els.resultCard.classList.add("hidden");
    return;
  }
  const success = list.filter((item) => item.ok).length;
  setText(els.resultSummary, `${success} / ${list.length} 成功`);
  els.resultList.innerHTML = list
    .map((item) => {
      const bad = !item.ok;
      const extra = bad
        ? `<span class="result-meta bad">${escapeHtml(item.error || "推送失败")}</span>`
        : `<span class="result-meta">${item.duplicate ? `⚠ 之前推送过（${escapeHtml(item.duplicate)}）· ` : ""}#${escapeHtml(item.id)}${item.size ? ` · ${fmtSize(item.size)}` : ""}${item.dir ? ` · ${escapeHtml(item.dir)}` : ""}</span>`;
      return `
      <div class="result-item ${bad ? "bad" : ""}">
        <span class="result-dot"></span>
        <div class="result-body">
          <span class="result-name">${escapeHtml(item.name || shortUrl(item.url))}</span>
          <span class="result-meta">${escapeHtml(shortUrl(item.url))}</span>
          ${extra}
        </div>
      </div>`;
    })
    .join("");
  els.resultCard.classList.remove("hidden");
}

/* ============================ 通用：toast / 弹窗 / busy ============================ */
function showToast(message, error = false) {
  els.toast.textContent = String(message || "");
  els.toast.classList.toggle("error", error);
  els.toast.classList.remove("hidden");
  clearTimeout(els.toast._timer);
  els.toast._timer = setTimeout(() => els.toast.classList.add("hidden"), 3600);
}

function confirmDialog(title, body) {
  return new Promise((resolve) => {
    els.modalTitle.textContent = title;
    els.modalBody.textContent = body;
    els.modal.classList.remove("hidden");
    const done = (value) => {
      els.modal.classList.add("hidden");
      els.modalOk.onclick = null;
      els.modalCancel.onclick = null;
      resolve(value);
    };
    els.modalOk.onclick = () => done(true);
    els.modalCancel.onclick = () => done(false);
  });
}

function setBusy(busy) {
  state.busy = Boolean(busy);
  els.loginBtn.disabled = state.busy;
  els.logoutBtn.disabled = state.busy;
  els.saveLocation.disabled = state.busy;
  els.refreshDevices.disabled = state.busy;
  els.refreshDir.disabled = state.busy;
  els.smsCheckBtn.disabled = state.busy;
  if (state.busy) {
    els.dispatchBtn.disabled = true;
  } else {
    renderPushHints();
  }
}

/* ============================ 事件绑定 ============================ */
document.getElementById("navPush").addEventListener("click", () => switchPage("push"));
document.getElementById("navSettings").addEventListener("click", () => switchPage("settings"));

els.linkInput.addEventListener("input", schedulePreview);
els.clearLinks.addEventListener("click", () => {
  els.linkInput.value = "";
  updatePreview();
});
els.dispatchBtn.addEventListener("click", dispatchLinks);

els.togglePwdBtn.addEventListener("click", () => {
  state.showPassword = !state.showPassword;
  els.passwordInput.type = state.showPassword ? "text" : "password";
  els.togglePwdBtn.classList.toggle("eye-active", state.showPassword);
});

els.loginBtn.addEventListener("click", () => doLogin());
els.logoutBtn.addEventListener("click", () => logout());

els.targetSelect.addEventListener("change", () => {
  setText(els.locationState, "已切换设备，请重新选择保存目录后保存");
  loadDirs("").catch(() => { });
});

els.dirSelect.addEventListener("change", () => {
  const value = els.dirSelect.value;
  if (value === "..") {
    loadDirs(state.dir.browse.parent || "").catch(() => { });
    return;
  }
  if (value === state.dir.path) return;   // 选中的就是当前目录
  loadDirs(value).catch(() => { });
});

els.refreshDir.addEventListener("click", async () => {
  if (state.busy) return;
  els.refreshDir.disabled = true;
  els.refreshDir.textContent = "刷新中...";
  els.locationState.className = "form-state";
  setText(els.locationState, "正在读取设备目录…");
  try {
    await loadDirs(state.dir.path);
    if (state.dir.browse.ok) {
      const count = (state.dir.browse.dirs || []).length;
      els.locationState.className = "form-state ok";
      setText(els.locationState, `目录已刷新（当前层 ${count} 个子目录）`);
      showToast("目录已刷新");
    } else {
      els.locationState.className = "form-state bad";
      setText(els.locationState, "目录读取失败，已切换为手动输入");
      showToast("目录读取失败，已切换为手动输入", true);
    }
  } catch (error) {
    els.locationState.className = "form-state bad";
    setText(els.locationState, error.message);
    showToast(error.message, true);
  } finally {
    els.refreshDir.disabled = false;
    els.refreshDir.textContent = "刷新目录";
  }
});

els.refreshDevices.addEventListener("click", async () => {
  if (state.busy) return;
  els.refreshDevices.disabled = true;
  els.refreshDevices.textContent = "刷新中...";
  els.locationState.className = "form-state";
  setText(els.locationState, "正在刷新设备…");
  try {
    await refreshStatus();
    if (state.status.reachable) await loadDirs(state.dir.path);
    const count = (state.devices || []).length;
    els.locationState.className = "form-state ok";
    setText(els.locationState, `设备已刷新（${count} 台在线）`);
    showToast("设备列表已刷新");
  } catch (error) {
    els.locationState.className = "form-state bad";
    setText(els.locationState, error.message);
    showToast(error.message, true);
  } finally {
    els.refreshDevices.disabled = false;
    els.refreshDevices.textContent = "刷新设备";
  }
});

els.saveLocation.addEventListener("click", saveLocation);

els.smsSendBtn.addEventListener("click", sendSms);
els.smsCheckBtn.addEventListener("click", checkSms);
els.reviewCancel.addEventListener("click", closeReviewModal);
els.smsCodeInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") checkSms();
});

document.addEventListener("click", (event) => {
  if (event.target === els.modal) els.modal.classList.add("hidden");
});

/* ============================ 初始化 ============================ */
const initialPage = hashPage();
if (initialPage && initialPage !== "push") {
  switchPage(initialPage);
}

boot().catch((error) => showToast(error.message, true));
