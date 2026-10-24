// Spor İstanbul Rezervasyon Botu — web UI frontend.
// Vanilla ES2020, no build step, no CDN, no network beyond same-origin /api/*.

// ---------------------------------------------------------------- constants

// F6: the real token arrives via the <meta name="x-token"> tag (server-injected --
// src.webapp._serve_static), never the launch URL's ?t= (that's a one-time nonce).
// Cached in sessionStorage so a same-tab reload (no fresh server-injected meta) still
// has it.
const TOKEN = (() => {
  const m = document.querySelector('meta[name="x-token"]');
  const t = (m && m.content) || "";
  try {
    if (t) sessionStorage.setItem("x-token", t);
    return t || sessionStorage.getItem("x-token") || "";
  } catch {
    return t;
  }
})();
const WEEKDAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const TR_DAY_SHORT = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"];
const TR_DAY_LONG = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"];
const NAV_LABELS = {
  dashboard: ["list", "Pano"],
  targets: ["target", "Hedefler"],
  reservations: ["calendar", "Rezervasyonlarım"],
  account: ["user", "Hesap"],
};
const PAGE_TITLES = {
  dashboard: "Pano",
  targets: "Hedefler",
  reservations: "Rezervasyonlarım",
  account: "Hesap",
};
const STATUS_TEXT = {
  idle: "Hazır", starting: "Başlıyor", monitoring: "İzleniyor", hot: "Açılış penceresi",
  found: "Seans bulundu", sms_wait: "SMS bekleniyor", booked: "Rezerve edildi",
  stopped: "Durduruldu", error: "Hata",
};

// -------------------------------------------------------------------- icons

const ICONS = {
  play: '<path d="M7 5v14l11-7z"/>',
  square: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  calendar: '<rect x="4" y="5" width="16" height="16" rx="2.5"/><path d="M8 3v4M16 3v4M4 10h16"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".6" fill="currentColor"/>',
  list: '<path d="M8 6h12M8 12h12M8 18h12"/><circle cx="4" cy="6" r="1" fill="currentColor"/><circle cx="4" cy="12" r="1" fill="currentColor"/><circle cx="4" cy="18" r="1" fill="currentColor"/>',
  user: '<circle cx="12" cy="8" r="3.6"/><path d="M5 20c0-3.6 3.1-6.4 7-6.4s7 2.8 7 6.4"/>',
  refresh: '<path d="M4 10a8 8 0 0 1 14-5.3M20 14a8 8 0 0 1-14 5.3"/><path d="M18 3v5h-5M6 21v-5h5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  pencil: '<path d="M4 20l1-4.2L16.3 4.5a1.4 1.4 0 0 1 2 0l1.2 1.2a1.4 1.4 0 0 1 0 2L8.2 19 4 20z"/>',
  trash: '<path d="M5 7h14M9 7V5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5v2m2 0-.8 12.2A2 2 0 0 1 14.2 21H9.8a2 2 0 0 1-2-1.8L7 7"/>',
  chevronUp: '<path d="M6 15l6-6 6 6"/>',
  chevronDown: '<path d="M6 9l6 6 6-6"/>',
  chevronLeft: '<path d="M15 6l-6 6 6 6"/>',
  check: '<path d="M5 13l5 5L19 7"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  phone: '<path d="M6 3h3l1.5 5-2 1.5a12 12 0 0 0 6 6l1.5-2 5 1.5v3a2 2 0 0 1-2 2A17 17 0 0 1 4 5a2 2 0 0 1 2-2z"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  wifi: '<path d="M3.5 9.5a12 12 0 0 1 17 0M6.5 12.8a8 8 0 0 1 11 0M9.7 16a4 4 0 0 1 4.6 0"/><circle cx="12" cy="19" r="1" fill="currentColor"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
  eyeOff: '<path d="M3 3l18 18"/><path d="M10.6 5.6A10.6 10.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a16 16 0 0 1-3 3.9M6.5 7.5C4 9.2 2.5 12 2.5 12S6 18.5 12 18.5a9.8 9.8 0 0 0 3.1-.5"/><path d="M9.9 14.1a3 3 0 0 0 4.2-4.2"/>',
  grip: '<circle cx="9" cy="6" r="1" fill="currentColor"/><circle cx="9" cy="12" r="1" fill="currentColor"/><circle cx="9" cy="18" r="1" fill="currentColor"/><circle cx="15" cy="6" r="1" fill="currentColor"/><circle cx="15" cy="12" r="1" fill="currentColor"/><circle cx="15" cy="18" r="1" fill="currentColor"/>',
  alert: '<path d="M12 4.5l9 15.5H3z"/><path d="M12 10v4"/><circle cx="12" cy="16.8" r=".6" fill="currentColor"/>',
  dot: '<circle cx="12" cy="12" r="3" fill="currentColor" stroke="none"/>',
};

function icon(name, cls = "") {
  const body = ICONS[name] || "";
  return `<svg class="icon ${cls}" viewBox="0 0 24 24" aria-hidden="true">${body}</svg>`;
}

// --------------------------------------------------------------- DOM utils

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
function el(tag, attrs = {}, html) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
  }
  if (html != null) node.innerHTML = html;
  return node;
}
function announce(text) {
  const live = $("#aria-live-region");
  if (live) live.textContent = text;
}

// ------------------------------------------------------------------- API

class ApiError extends Error {}

// Send a diagnostic line to the server log (state/logs/bot.log). Never throws.
function clientLog(level, text) {
  try {
    fetch("/api/client_log", {
      method: "POST", cache: "no-store",
      headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
      body: JSON.stringify({ level, text: String(text).slice(0, 500) }),
    }).catch(() => {});
  } catch { /* ignore */ }
}
window.addEventListener("error", (e) => clientLog("error", `JS hata: ${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", (e) => clientLog("error", `İşlenmemiş hata: ${e.reason && (e.reason.stack || e.reason.message) || e.reason}`));
// Every button click (capture phase, so it is recorded even if a handler misbehaves).
document.addEventListener("click", (e) => {
  const b = e.target && e.target.closest && e.target.closest("button");
  if (b) clientLog("info", `tıklama: #${b.id || "?"} "${(b.textContent || "").trim().slice(0, 40)}"${b.disabled ? " (pasif)" : ""}`);
}, true);

async function apiFetch(path, opts = {}) {
  const method = (opts.method || "GET");
  if (method !== "GET") clientLog("info", `istek gönderiliyor: ${method} ${path}`);
  const res = await fetch(path, {
    ...opts,
    headers: { "X-Token": TOKEN, "Content-Type": "application/json", ...(opts.headers || {}) },
    cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok && !(data && "ok" in data)) {
    throw new ApiError(`HTTP ${res.status}`);
  }
  return data;
}

const api = {
  state: (since) => apiFetch(`/api/state?since=${since}`),
  catalog: () => apiFetch(`/api/catalog`),
  command: (type, payload = {}) => apiFetch(`/api/command`, { method: "POST", body: JSON.stringify({ type, ...payload }) }),
  putSettings: (settings) => apiFetch(`/api/settings`, { method: "PUT", body: JSON.stringify(settings) }),
  saveCredentials: (tc, password) => apiFetch(`/api/credentials`, { method: "POST", body: JSON.stringify({ tc, password }) }),
  clearCredentials: (id) => apiFetch(`/api/credentials`, { method: "DELETE", body: id ? JSON.stringify({ id }) : undefined }),
  switchAccount: (id) => apiFetch(`/api/command`, { method: "POST", body: JSON.stringify({ type: "switch_account", id }) }),
};

// --------------------------------------------------------------- app state

let state = null;           // last /api/state payload
let catalog = null;         // Catalog.to_dict()
let catalogVersion = -1;
let serverNowOffsetMs = 0;  // serverNow - clientNow, refreshed every poll
let pendingDryRun = false;  // local UI value for the next Start, seeded from dry_run_ui
let connLost = false;
let browserShownLocal = false; // optimistic UI only -- see wireHero()
let drawerTarget = null;    // target being edited, or null for "new"
let drawerState = null;     // working form state while drawer is open
let addAccountOpen = false; // "Hesap ekle" collapsible form -- user-toggled, forced open when there are no accounts
let dismissedUpdateVersion = null; // dismissed update version, cleared when a new version arrives

// ------------------------------------------------------------------- toasts

function toast(kind, text) {
  const stack = $("#toast-stack");
  const node = el("div", { class: `toast ${kind}` }, `${icon(kind === "success" ? "check" : "alert")}<span>${escapeHtml(text)}</span>`);
  stack.appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity 180ms ease-out";
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 200);
  }, 4200);
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// "Bilge Aydın" -> "BA" (avatar initials); "" / null -> "?"
function nameInitials(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  const first = parts[0][0] || "";
  const last = parts.length > 1 ? parts[parts.length - 1][0] : "";
  return (first + last).toUpperCase() || "?";
}

// "Bilge Aydın" -> "Bilge A." (sidebar chip); single word stays as-is.
function nameFirstLastInitial(name) {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "";
  if (parts.length === 1) return parts[0];
  return `${parts[0]} ${parts[parts.length - 1][0].toUpperCase()}.`;
}

// ------------------------------------------------------------- focus trap

// Shared by the SMS modal, the confirm dialog, and the target drawer (fix list item 6):
// while one of those is open, Tab/Shift+Tab must cycle only within it.
function focusableElements(container) {
  if (!container) return [];
  return $$('button, [href], input, select, textarea, [tabindex]', container)
    .filter((node) => !node.disabled && node.tabIndex !== -1 && node.offsetParent !== null);
}

function trapTabKey(e, container) {
  if (e.key !== "Tab") return;
  const items = focusableElements(container);
  if (!items.length) return;
  // Always drive focus movement ourselves rather than only intervening at the first/last
  // boundary and otherwise falling back to the browser's native Tab default-action: macOS
  // WebKit's default "Full Keyboard Access" setting only tab-stops on text fields/lists,
  // silently skipping buttons -- which would walk focus right out of the dialog instead of
  // onto e.g. a submit button. preventDefault()-ing unconditionally and cycling through our
  // own `items` list sidesteps that engine-specific default behavior entirely.
  e.preventDefault();
  const active = document.activeElement;
  let idx = items.indexOf(active);
  idx = e.shiftKey
    ? (idx <= 0 ? items.length - 1 : idx - 1)
    : (idx === -1 || idx === items.length - 1 ? 0 : idx + 1);
  items[idx].focus();
}

// ----------------------------------------------------------------- confirm

function confirmDialog({ title, text, okText = "Onayla", danger = true }) {
  return new Promise((resolve) => {
    const scrim = $("#confirm-scrim");
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    $("#confirm-ok").textContent = okText;
    $("#confirm-ok").className = danger ? "btn btn-danger" : "btn btn-primary";
    $("#confirm-icon").innerHTML = icon("alert", "lg");
    scrim.classList.add("show");
    const okBtn = $("#confirm-ok");
    const cancelBtn = $("#confirm-cancel");
    function close(result) {
      scrim.classList.remove("show");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKey);
      resolve(result);
    }
    function onOk() { close(true); }
    function onCancel() { close(false); }
    function onKey(e) {
      if (e.key === "Escape") close(false);
      else if (e.key === "Enter") close(true);
      else if (e.key === "Tab") trapTabKey(e, $("#confirm-dialog"));
    }
    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    document.addEventListener("keydown", onKey);
    setTimeout(() => okBtn.focus(), 50);
  });
}

// -------------------------------------------------------------- navigation

function showPage(name) {
  for (const key of Object.keys(PAGE_TITLES)) {
    $(`#page-${key}`).hidden = key !== name;
  }
  $("#page-title").textContent = PAGE_TITLES[name];
  for (const btn of $$(".nav-btn")) {
    btn.toggleAttribute("aria-current", btn.dataset.nav === name);
    if (btn.dataset.nav === name) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  }
  location.hash = name;
}

function initNav() {
  for (const [key, [iconName, label]] of Object.entries(NAV_LABELS)) {
    const btn = $(`.nav-btn[data-nav="${key}"]`);
    btn.innerHTML = `${icon(iconName)}<span class="label">${label}</span>`;
    // Always set (not only under 1050px): the label is visually hidden at narrow
    // widths via CSS, so the accessible name must not depend on the visible text.
    btn.setAttribute("aria-label", label);
    btn.title = label;
  }
  for (const btn of $$("[data-nav]")) {
    btn.addEventListener("click", () => showPage(btn.dataset.nav));
  }
  const initial = (location.hash || "").replace("#", "");
  showPage(PAGE_TITLES[initial] ? initial : "dashboard");
}

// ---------------------------------------------------------- time/format

function pad2(n) { return String(Math.max(0, Math.trunc(n))).padStart(2, "0"); }

function serverNow() { return new Date(Date.now() + serverNowOffsetMs); }

function fmtCountdown(targetIso) {
  const diffMs = new Date(targetIso).getTime() - serverNow().getTime();
  if (!isFinite(diffMs)) return "—";
  if (diffMs <= 0) return "00:00:00";
  let s = Math.floor(diffMs / 1000);
  const days = Math.floor(s / 86400); s -= days * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (days > 0) return `${days}g ${pad2(h)}:${pad2(m)}:${pad2(s)}`;
  return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
}

function fmtDateTime(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return `${pad2(d.getDate())}.${pad2(d.getMonth() + 1)}.${d.getFullYear()} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function fmtRelative(iso) {
  const diffS = Math.max(0, Math.floor((serverNow().getTime() - new Date(iso).getTime()) / 1000));
  if (diffS < 10) return "az önce";
  if (diffS < 60) return `${diffS} sn önce`;
  if (diffS < 3600) return `${Math.floor(diffS / 60)} dk önce`;
  if (diffS < 86400) return `${Math.floor(diffS / 3600)} sa önce`;
  return `${Math.floor(diffS / 86400)} gün önce`;
}

function fmtTurkishDate(isoDate) {
  // "YYYY-MM-DD" -> "16.10.2026 Cuma"
  const [y, m, d] = isoDate.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  return `${pad2(d)}.${pad2(m)}.${y} ${TR_DAY_LONG[(dt.getDay() + 6) % 7]}`;
}

function getDayOfWeekShort(iso) {
  // Extract short Turkish weekday from ISO timestamp (Mon=0)
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return TR_DAY_SHORT[(d.getDay() + 6) % 7];
}

function getDayOfWeekLong(iso) {
  // Extract long Turkish weekday from ISO timestamp (Mon=0)
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return TR_DAY_LONG[(d.getDay() + 6) % 7];
}

function fmtReminderTime(iso) {
  // Extract HH:MM from ISO timestamp
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// ---------------------------------------------------------------- rendering

function render() {
  if (!state) return;
  renderTopbar();
  renderUpdate();
  renderBanner();
  renderHero();
  renderNextWindow();
  renderReminders();
  renderLog();
  renderMiniTargets();
  renderTargetsPage();
  renderReservations();
  renderAccount();
  renderSidebarFooter();
  renderSms();
}

function renderTopbar() {
  const eng = state.engine || {};
  const pill = $("#status-pill");
  pill.dataset.state = eng.state || "idle";
  pill.classList.toggle("pulse", ["monitoring", "hot", "starting"].includes(eng.state));
  $("#status-pill-text").textContent = (eng.status_text || STATUS_TEXT[eng.state] || "—") + (eng.running && eng.dry_run ? " · Deneme" : "");
}

function renderUpdate() {
  const bar = $("#update-bar");
  const upd = state.update;

  if (!upd || dismissedUpdateVersion === upd.version) {
    bar.className = "";
    bar.innerHTML = "";
    return;
  }

  if (upd.status === "available") {
    const eng = state.engine || {};
    const disabled = eng.running || eng.busy;
    const text = `Uygulamaya güncelleme geldi (v${escapeHtml(upd.version)}), lütfen güncelleyiniz.`;
    const updateBtn = el("button", { class: "btn btn-primary btn-sm", id: "btn-update-now", disabled }, icon("refresh") + "<span>Şimdi güncelle</span>");
    if (disabled) updateBtn.title = "Önce durdurun";
    updateBtn.addEventListener("click", async () => {
      try { await api.command("update"); } catch (e) { toast("error", e.message || "Güncelleme başarısız"); }
    });
    const laterBtn = el("button", { class: "btn btn-secondary btn-sm", id: "btn-update-later" }, "<span>Daha sonra</span>");
    laterBtn.addEventListener("click", () => {
      dismissedUpdateVersion = upd.version;
      bar.className = "";
      bar.innerHTML = "";
    });
    bar.className = "show available";
    bar.innerHTML = `${icon("refresh")}<span class="update-text">${text}</span>`;
    const actions = el("div", { class: "update-actions" });
    actions.appendChild(updateBtn);
    actions.appendChild(laterBtn);
    bar.appendChild(actions);
  } else if (upd.status === "downloading") {
    const text = "Güncelleme indiriliyor, uygulama yeniden başlayacak…";
    bar.className = "show downloading";
    bar.innerHTML = `${icon("refresh")}<span class="update-text">${text}</span>`;
  } else if (upd.status === "error") {
    const text = `Güncelleme başarısız: ${escapeHtml(upd.error || "Bilinmeyen hata")}`;
    const retryBtn = el("button", { class: "btn btn-danger btn-sm", id: "btn-update-retry" }, "<span>Tekrar dene</span>");
    retryBtn.addEventListener("click", async () => {
      try { await api.command("update"); } catch (e) { toast("error", e.message || "Güncelleme başarısız"); }
    });
    const laterBtn = el("button", { class: "btn btn-secondary btn-sm", id: "btn-update-later-error" }, "<span>Daha sonra</span>");
    laterBtn.addEventListener("click", () => {
      dismissedUpdateVersion = upd.version;
      bar.className = "";
      bar.innerHTML = "";
    });
    bar.className = "show error";
    bar.innerHTML = `${icon("alert")}<span class="update-text">${text}</span>`;
    const actions = el("div", { class: "update-actions" });
    actions.appendChild(retryBtn);
    actions.appendChild(laterBtn);
    bar.appendChild(actions);
  }
}

function renderBanner() {
  const slot = $("#banner-slot");
  slot.innerHTML = "";
  const banner = state.banner;
  if (!banner) return;
  const node = el("div", { class: `banner ${banner.kind}` },
    `${icon(banner.kind === "success" ? "check" : "alert")}<span class="banner-text">${escapeHtml(banner.text)}</span>`);
  const closeBtn = el("button", { class: "btn btn-ghost btn-icon btn-sm", "aria-label": "Kapat" }, icon("x", "sm"));
  closeBtn.addEventListener("click", async () => {
    try { await api.command("dismiss_banner"); } catch { /* retried on next poll */ }
    slot.innerHTML = "";
  });
  node.appendChild(closeBtn);
  slot.appendChild(node);
}

function renderHero() {
  const eng = state.engine || {};
  const running = !!eng.running;
  const modeSuffix = eng.running ? (eng.dry_run ? " · DENEME MODU (Kaydet'e basılmaz)" : " · GERÇEK MOD") : "";
  $("#hero-status-text").textContent = (eng.status_text || STATUS_TEXT[eng.state] || "—") + modeSuffix;

  const startBtn = $("#btn-start-stop");
  const label = $("#btn-start-stop-label");
  startBtn.className = `btn btn-lg ${running ? "btn-danger" : "btn-primary"}`;
  startBtn.innerHTML = `${icon(running ? "square" : "play")}<span id="btn-start-stop-label">${running ? "Durdur" : "Başlat"}</span>`;
  startBtn.disabled = running ? !eng.can_stop : !eng.can_start;
  startBtn.onclick = running ? onStopClick : onStartClick;

  const hint = $("#block-hint");
  if (!running && eng.start_block_reason) {
    hint.hidden = false;
    const target = eng.start_block_reason.includes("hedef") ? "targets"
      : eng.start_block_reason.includes("iriş") || eng.start_block_reason.includes("esap") ? "account" : null;
    const linkText = target === "targets" ? "Hedeflere git →" : target === "account" ? "Hesaba git →" : "";
    hint.innerHTML = `${icon("alert")}<span>${escapeHtml(eng.start_block_reason)}${target ? ` <a href="#" data-nav="${target}">${linkText}</a>` : ""}</span>`;
    const link = hint.querySelector("a");
    if (link) link.addEventListener("click", (e) => { e.preventDefault(); showPage(target); });
  } else {
    hint.hidden = true;
  }

  const dryRunToggle = $("#toggle-dry-run");
  dryRunToggle.checked = pendingDryRun;
  dryRunToggle.disabled = running;
  const stopToggle = $("#toggle-stop-after-success");
  stopToggle.checked = !!(state.settings && state.settings.stop_after_success);
  stopToggle.disabled = running;
  const remindersToggle = $("#toggle-reminders-enabled");
  remindersToggle.checked = !!(state.settings && state.settings.reminders_enabled);
  remindersToggle.disabled = running;

  // The API contract has no field reporting the browser window's current visibility
  // (only the show_browser/hide_browser commands) -- track it optimistically from our
  // own last click instead of trying to read it back from server state.
  $("#btn-toggle-browser-label").textContent = browserShownLocal ? "Tarayıcıyı gizle" : "Tarayıcıyı göster";

  renderLastPoll();
}

// Fix list item 5: "Son kontrol" (last successful grid poll), shown only while running.
function renderLastPoll() {
  const node = $("#hero-last-poll");
  if (!node) return;
  const running = !!(state.engine && state.engine.running);
  if (!running || !state.last_poll) {
    node.hidden = true;
    node.textContent = "";
    node.classList.remove("stale");
    return;
  }
  const secondsAgo = Math.max(0, Math.floor((serverNow().getTime() - new Date(state.last_poll).getTime()) / 1000));
  node.hidden = false;
  node.textContent = `İzleniyor · son kontrol ${fmtRelative(state.last_poll)}`;
  node.title = `Son kontrol: ${fmtDateTime(state.last_poll)} (${secondsAgo} sn önce)`;
  node.classList.toggle("stale", secondsAgo > 180);
}

function renderNextWindow() {
  const slot = $("#next-window-slot");
  const nw = state.next_window;
  if (!nw) {
    slot.innerHTML = `<div class="next-window-empty">Henüz planlanan açılış yok</div>`;
    return;
  }
  slot.innerHTML = `
    <div class="countdown tabular" id="nw-countdown">${fmtCountdown(nw.open_at)}</div>
    <div class="next-window-meta">
      <span class="next-window-name">${escapeHtml(nw.target_name)}</span>
      <span class="next-window-sub">${escapeHtml(nw.facility)}</span>
      <span class="next-window-sub">${fmtDateTime(nw.session_start)}</span>
    </div>`;
}

function renderReminders() {
  const card = $("#reminders-card");
  const slot = $("#reminders-slot");
  const rem = state.reminders;
  if (!rem) {
    card.hidden = true;
    return;
  }

  // Determine whether to show the card
  const anyEnabled = (state.settings?.targets || []).some((t) => t.enabled);
  let html = "";

  // Status line
  let badge;
  if (!rem.supported) badge = ["off", "Bu sistemde desteklenmiyor"];
  else if (!rem.enabled) badge = ["off", "Kapalı"];
  else if (rem.error) badge = ["warn", `Kaydedilemedi: ${escapeHtml(rem.error)}`];
  else if (rem.registered) badge = ["ok", "Sisteme kayıtlı"];
  else badge = ["off", "Kaydediliyor…"];
  html += `<div class="reminder-status"><span class="status-badge ${badge[0]}"><span class="dot"></span>${badge[1]}</span></div>`;

  // Upcoming items or empty state
  const upcoming = rem.upcoming || [];
  if (!rem.enabled) {
    html += `<div class="mini-empty">Hatırlatmalar kapalı</div>`;
  } else if (upcoming.length === 0) {
    html += anyEnabled
      ? `<div class="mini-empty">Hatırlatma hesaplanamadı: hedefin tesisini Hedefler'den tarayın ya da bir saat / saat aralığı seçin.</div>`
      : `<div class="mini-empty">Önce bir hedef ekleyin</div>`;
  } else {
    html += `<div class="reminder-list">` + upcoming.slice(0, 5).map((r) => `
      <div class="reminder-item">
        <div class="reminder-when"><span>${getDayOfWeekShort(r.remind_at)}</span><strong>${fmtReminderTime(r.remind_at)}</strong></div>
        <div class="reminder-what">
          <span class="reminder-session">${getDayOfWeekLong(r.session_start)} ${fmtReminderTime(r.session_start)} seansı</span>
          <span class="reminder-target">${escapeHtml(r.target_name)}</span>
        </div>
      </div>`).join("") + `</div>`;
  }

  slot.innerHTML = html;
  card.hidden = !rem.enabled && !rem.supported;
}

function renderMiniTargets() {
  const list = $("#mini-targets-list");
  const summary = state.targets_summary || [];
  if (!summary.length) {
    list.innerHTML = `<div class="mini-empty">Henüz hedef yok</div>`;
    return;
  }
  const byId = new Map((state.settings?.targets || []).map((t) => [t.id, t]));
  list.innerHTML = summary.map((s) => {
    const t = byId.get(s.id);
    const on = t ? !!t.enabled : true;
    return `<div class="mini-item"><span class="dot ${on ? "on" : ""}"></span><span class="label">${escapeHtml(s.describe)}</span></div>`;
  }).join("");
}

function renderLog() {
  const feed = $("#log-feed");
  const log = state.log || [];
  if (!log.length) {
    feed.innerHTML = `<div class="log-empty">Henüz etkinlik yok</div>`;
    return;
  }
  const items = [...log].sort((a, b) => b.seq - a.seq);
  feed.innerHTML = items.map((l) => {
    const lvlIcon = l.level === "ERROR" || l.level === "WARNING" ? "alert" : "dot";
    return `<div class="log-row level-${l.level}">${icon(lvlIcon, "sm")}<span class="log-text">${escapeHtml(l.text)}</span><span class="log-time" title="${escapeHtml(fmtDateTime(l.ts))}">${fmtRelative(l.ts)}</span></div>`;
  }).join("");
}

function renderSidebarFooter() {
  const chip = $("#sidebar-account-chip");
  const acc = state.account || {};
  const dot = chip.querySelector(".dot");
  const label = chip.querySelector(".chip-label");
  const running = !!(state.engine && state.engine.running);
  let text, title;
  if (acc.name) {
    // Multi-account (2026-10-18): once the site has told us the member's name, lead
    // with "Bilge A." instead of the generic status text -- a tooltip keeps the full
    // name + masked TC for anyone who needs to double check which account is active.
    dot.className = `dot ${acc.logged_in ? "on" : "off"}`;
    text = nameFirstLastInitial(acc.name);
    title = `${acc.name}${acc.tc_masked ? ` · ${acc.tc_masked}` : ""}`;
  } else if (acc.has_credentials) {
    if (!acc.logged_in && running) {
      // Fix list item 4: _ever_logged_in resets on LoggedOut -- make that visible
      // instead of silently falling back to the masked TC/"Hesap kayıtlı" text.
      dot.className = "dot off";
      text = "Oturum kapalı — yenileniyor";
    } else {
      dot.className = `dot ${acc.logged_in ? "on" : "off"}`;
      text = acc.logged_in ? "Giriş yapılı" : (acc.tc_masked || "Hesap kayıtlı");
    }
    title = text;
  } else {
    dot.className = "dot off";
    text = "Hesap yok";
    title = text;
  }
  label.textContent = text;
  chip.title = title; // fix list item 6: keep a tooltip even when the label is hidden
  $("#btn-quit-browser").hidden = state.mode !== "browser";

  const versionEl = $("#sidebar-version");
  if (versionEl) {
    const v = state.app_version;
    versionEl.textContent = v && v !== "—" ? `v${v}` : "";
    versionEl.title = v ? `Sürüm ${v}` : "";
  }
}

// ------------------------------------------------------------------- targets

function renderTargetsPage() {
  const targets = state.settings?.targets || [];
  const running = !!(state.engine && state.engine.running);
  $("#targets-running-note").hidden = !running;
  if (running) $("#targets-running-note").textContent = "Çalışırken hedef ayarları değiştirilemez; önce durdurun.";
  for (const b of ["btn-add-target", "btn-add-target-empty"]) $(`#${b}`).disabled = running;

  $("#targets-empty").hidden = targets.length > 0;
  const list = $("#targets-list");
  list.hidden = targets.length === 0;
  if (!targets.length) { list.innerHTML = ""; return; }

  list.innerHTML = "";
  targets.forEach((t, i) => {
    const card = el("div", { class: `target-card ${t.enabled ? "" : "disabled-row"}`, draggable: "true", "data-id": t.id });
    const days = (t.days || []).map((d) => TR_DAY_SHORT[WEEKDAYS_EN.indexOf(d)]).join(", ");
    const timeStr = t.starts && t.starts.length ? t.starts.join(", ")
      : (t.time_from && t.time_to) ? `${t.time_from}–${t.time_to}`
      : t.time_from ? `${t.time_from}+` : t.time_to ? `–${t.time_to}` : "her saat";
    const hallsStr = (t.halls && t.halls.length) ? t.halls.join(", ") : "tüm salonlar";
    const facilityStr = t.facility === "*" ? "tüm tesisler" : t.facility;

    card.innerHTML = `
      <span class="drag-handle" title="Sürükleyip sırala">${icon("grip")}</span>
      <div class="reorder-col">
        <button class="btn btn-ghost btn-icon btn-sm" data-act="up" ${i === 0 || running ? "disabled" : ""} aria-label="Yukarı taşı">${icon("chevronUp", "sm")}</button>
        <button class="btn btn-ghost btn-icon btn-sm" data-act="down" ${i === targets.length - 1 || running ? "disabled" : ""} aria-label="Aşağı taşı">${icon("chevronDown", "sm")}</button>
      </div>
      <label class="switch" style="margin-right:4px">
        <input type="checkbox" data-act="toggle" ${t.enabled ? "checked" : ""} ${running ? "disabled" : ""}>
        <span class="track"></span><span class="thumb"></span>
      </label>
      <div class="target-main">
        <div class="target-name-row"><span class="target-name">${escapeHtml(t.name)}</span></div>
        <span class="target-desc">${escapeHtml(t.branch)} · ${escapeHtml(facilityStr)}</span>
        <div class="target-chips">
          <span class="chip chip-brand">${escapeHtml(days || "?")}</span>
          <span class="chip">${escapeHtml(timeStr)}</span>
          <span class="chip">${escapeHtml(hallsStr)}</span>
        </div>
      </div>
      <div class="target-actions">
        <button class="btn btn-ghost btn-icon" data-act="edit" ${running ? "disabled" : ""} aria-label="Düzenle">${icon("pencil")}</button>
        <button class="btn btn-ghost btn-icon" data-act="delete" ${running ? "disabled" : ""} aria-label="Sil">${icon("trash")}</button>
      </div>`;

    card.querySelector('[data-act="up"]')?.addEventListener("click", () => reorderTarget(i, -1));
    card.querySelector('[data-act="down"]')?.addEventListener("click", () => reorderTarget(i, 1));
    card.querySelector('[data-act="toggle"]')?.addEventListener("change", (e) => toggleTargetEnabled(t.id, e.target.checked));
    card.querySelector('[data-act="edit"]')?.addEventListener("click", () => openDrawer(t));
    card.querySelector('[data-act="delete"]')?.addEventListener("click", () => deleteTarget(t));

    card.addEventListener("dragstart", () => card.classList.add("dragging"));
    card.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.addEventListener("dragover", (e) => {
      e.preventDefault();
      const dragging = list.querySelector(".dragging");
      if (!dragging || dragging === card) return;
      const rect = card.getBoundingClientRect();
      const before = (e.clientY - rect.top) < rect.height / 2;
      list.insertBefore(dragging, before ? card : card.nextSibling);
    });
    card.addEventListener("drop", () => commitDragOrder());

    list.appendChild(card);
  });
}

function commitDragOrder() {
  const ids = $$(".target-card", $("#targets-list")).map((c) => c.dataset.id);
  const targets = state.settings.targets;
  const byId = new Map(targets.map((t) => [t.id, t]));
  const reordered = ids.map((id) => byId.get(id));
  saveSettingsPatch({ targets: reordered });
}

function reorderTarget(index, delta) {
  const targets = [...state.settings.targets];
  const j = index + delta;
  if (j < 0 || j >= targets.length) return;
  [targets[index], targets[j]] = [targets[j], targets[index]];
  saveSettingsPatch({ targets });
}

function toggleTargetEnabled(id, enabled) {
  const targets = state.settings.targets.map((t) => (t.id === id ? { ...t, enabled } : t));
  saveSettingsPatch({ targets });
}

async function deleteTarget(t) {
  const ok = await confirmDialog({
    title: "Hedef silinsin mi?",
    text: `"${t.name}" hedefi kalıcı olarak silinecek.`,
    okText: "Sil",
  });
  if (!ok) return;
  const targets = state.settings.targets.filter((x) => x.id !== t.id);
  saveSettingsPatch({ targets });
}

async function saveSettingsPatch(patch) {
  const full = { ...state.settings, ...patch };
  try {
    const res = await api.putSettings(full);
    if (res && res.ok) {
      state.settings = res.settings;
      render();
    } else {
      toast("error", (res && res.error) || "Ayarlar kaydedilemedi");
    }
  } catch {
    toast("error", "Sunucuya bağlanılamadı");
  }
}

// -------------------------------------------------------------------- drawer

function openDrawer(target) {
  drawerTarget = target || null;
  drawerState = target ? targetToForm(target) : {
    name: "", branch: "", facility: "", halls: [], level: "", days: [],
    time_mode: "range", time_from: "", time_to: "", starts: [], enabled: true,
  };
  $("#drawer-title").textContent = target ? "Hedef düzenle" : "Hedef ekle";
  $("#btn-delete-target").hidden = !target;
  $("#drawer-error").textContent = "";
  const running = !!(state.engine && state.engine.running);
  $("#drawer-running-note").hidden = !running;
  if (running) $("#drawer-running-note").innerHTML = `${icon("alert", "sm")}<span>Çalışırken hedefler düzenlenemez.</span>`;
  for (const id of ["field-name", "field-branch", "field-facility", "field-level", "btn-scan-branches",
    "btn-scan-facilities", "btn-scan-grids", "field-time-from", "field-time-to", "field-custom-start",
    "btn-add-custom-start", "btn-drawer-save"]) {
    $(`#${id}`).disabled = running;
  }

  $("#field-name").value = drawerState.name;
  populateBranchSelect();
  populateFacilitySelect();
  populateLevelSelect();
  populateHallChips();
  populateDayChips();
  setTimeMode(drawerState.time_mode);
  populateTimeRangeSelects();
  populateStartChips();
  updateCatalogNote();

  $("#drawer-scrim").classList.add("show");
  $("#target-drawer").classList.add("show");
  setTimeout(() => $("#field-name").focus(), 60);
}

function closeDrawer() {
  $("#drawer-scrim").classList.remove("show");
  $("#target-drawer").classList.remove("show");
  drawerTarget = null;
  drawerState = null;
}

const FACILITY_ANY = "Herhangi tesis";
const LEVEL_ANY = "Herhangi";
const TIME_ANY = "Herhangi";

function targetToForm(t) {
  return {
    name: t.name,
    branch: t.branch,
    facility: t.facility === "*" ? FACILITY_ANY : t.facility,
    halls: [...(t.halls || [])],
    level: t.level_prefix || LEVEL_ANY,
    days: [...(t.days || [])].map((d) => WEEKDAYS_EN.indexOf(d)).sort((a, b) => a - b),
    time_mode: (t.starts && t.starts.length) ? "specific" : "range",
    time_from: t.time_from || TIME_ANY,
    time_to: t.time_to || TIME_ANY,
    starts: [...(t.starts || [])].sort(),
    enabled: t.enabled,
  };
}

function timeChoices(step = 30) {
  const out = [TIME_ANY];
  for (let m = 6 * 60; m <= 23 * 60 + 30; m += step) out.push(`${pad2(Math.floor(m / 60))}:${pad2(m % 60)}`);
  return out;
}

function catalogBranches() { return catalog ? Object.keys(catalog.branches || {}) : []; }
function catalogFacilities(branch) {
  const b = catalog?.branches?.[branch];
  return b ? Object.keys(b.facilities || {}) : [];
}
function catalogHalls(branch, facility) {
  const f = catalog?.branches?.[branch]?.facilities?.[facility];
  return f ? Object.keys(f.halls || {}) : [];
}
function catalogLevels(branch, facility) {
  const f = catalog?.branches?.[branch]?.facilities?.[facility];
  return f ? Object.keys(f.levels || {}) : [];
}
function catalogStarts(branch, facility, weekday) {
  const f = catalog?.branches?.[branch]?.facilities?.[facility];
  if (!f) return [];
  const set = new Set();
  for (const key of Object.keys(f.slots || {})) {
    const [wd, start] = key.split("|");
    if (WEEKDAYS_EN.indexOf(wd) === weekday) set.add(start);
  }
  return [...set].sort();
}

function populateBranchSelect() {
  const sel = $("#field-branch");
  const names = catalogBranches();
  sel.innerHTML = (names.length ? names : [drawerState.branch].filter(Boolean))
    .map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("") || `<option value="">—</option>`;
  if (drawerState.branch && !names.includes(drawerState.branch)) {
    sel.insertAdjacentHTML("afterbegin", `<option value="${escapeHtml(drawerState.branch)}">${escapeHtml(drawerState.branch)}</option>`);
  }
  sel.value = drawerState.branch || sel.options[0]?.value || "";
  drawerState.branch = sel.value;
  $("#btn-scan-branches").innerHTML = `${icon("refresh", "sm")} Branşları getir`;
  sel.onchange = () => { drawerState.branch = sel.value; populateFacilitySelect(); };
}

function populateFacilitySelect() {
  const sel = $("#field-facility");
  const names = [FACILITY_ANY, ...catalogFacilities(drawerState.branch)];
  sel.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
  if (!names.includes(drawerState.facility)) drawerState.facility = FACILITY_ANY;
  sel.value = drawerState.facility;
  $("#btn-scan-facilities").innerHTML = `${icon("refresh", "sm")} Tesisleri getir`;
  sel.onchange = () => {
    drawerState.facility = sel.value;
    populateLevelSelect();
    populateHallChips();
    populateStartChips();
    updateCatalogNote();
  };
}

function populateLevelSelect() {
  const sel = $("#field-level");
  const branch = drawerState.branch, facility = drawerState.facility === FACILITY_ANY ? null : drawerState.facility;
  const names = [LEVEL_ANY, ...(facility ? catalogLevels(branch, facility) : [])];
  sel.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
  if (!names.includes(drawerState.level)) drawerState.level = LEVEL_ANY;
  sel.value = drawerState.level;
  sel.onchange = () => { drawerState.level = sel.value; };
}

function populateHallChips() {
  // Fix list item 2 (P0): this button had no label/icon at all (0-width, invisible) --
  // give it the same icon+text treatment as its "Branşları getir"/"Tesisleri getir"
  // siblings (see populateBranchSelect/populateFacilitySelect).
  $("#btn-scan-grids").innerHTML = `${icon("refresh", "sm")} Salon ve saatleri tara`;
  const wrap = $("#hall-chips");
  const branch = drawerState.branch, facility = drawerState.facility === FACILITY_ANY ? null : drawerState.facility;
  const halls = facility ? catalogHalls(branch, facility) : [];
  if (!halls.length) {
    wrap.innerHTML = `<p class="field-hint" style="margin:0">Önce 'Salon ve saatleri tara' ile salonları getirin.</p>`;
    return;
  }
  wrap.innerHTML = halls.map((h) => `<button type="button" class="hall-chip" data-h="${escapeHtml(h)}" aria-pressed="${drawerState.halls.includes(h)}">${escapeHtml(h)}</button>`).join("");
  for (const btn of $$(".hall-chip", wrap)) {
    btn.addEventListener("click", () => {
      const h = btn.dataset.h;
      if (drawerState.halls.includes(h)) drawerState.halls = drawerState.halls.filter((x) => x !== h);
      else drawerState.halls.push(h);
      btn.setAttribute("aria-pressed", drawerState.halls.includes(h));
    });
  }
}

function populateDayChips() {
  const wrap = $("#day-chips");
  wrap.innerHTML = TR_DAY_SHORT.map((label, i) => `<button type="button" class="day-chip" data-d="${i}" aria-pressed="${drawerState.days.includes(i)}">${label}</button>`).join("");
  for (const btn of $$(".day-chip", wrap)) {
    btn.addEventListener("click", () => {
      const d = Number(btn.dataset.d);
      if (drawerState.days.includes(d)) drawerState.days = drawerState.days.filter((x) => x !== d);
      else drawerState.days.push(d);
      btn.setAttribute("aria-pressed", drawerState.days.includes(d));
      populateStartChips();
    });
  }
}

function setTimeMode(mode) {
  drawerState.time_mode = mode;
  for (const btn of $$("#time-mode-segmented button")) btn.setAttribute("aria-pressed", btn.dataset.mode === mode);
  $("#time-range-group").hidden = mode !== "range";
  $("#time-specific-group").hidden = mode !== "specific";
}

function populateTimeRangeSelects() {
  const choices = timeChoices();
  for (const id of ["field-time-from", "field-time-to"]) {
    const sel = $(`#${id}`);
    sel.innerHTML = choices.map((c) => `<option value="${c}">${c}</option>`).join("");
  }
  $("#field-time-from").value = drawerState.time_from;
  $("#field-time-to").value = drawerState.time_to;
  $("#field-time-from").onchange = (e) => { drawerState.time_from = e.target.value; };
  $("#field-time-to").onchange = (e) => { drawerState.time_to = e.target.value; };
}

function populateStartChips() {
  const wrap = $("#start-chips");
  const branch = drawerState.branch, facility = drawerState.facility === FACILITY_ANY ? null : drawerState.facility;
  const suggested = new Set(drawerState.starts);
  if (facility) {
    const days = drawerState.days.length ? drawerState.days : [0, 1, 2, 3, 4, 5, 6];
    for (const d of days) for (const s of catalogStarts(branch, facility, d)) suggested.add(s);
  }
  const names = [...suggested].sort();
  if (!names.length) {
    wrap.innerHTML = `<p class="field-hint" style="margin:0">Önce branş/tesis/gün seçin veya özel saat ekleyin.</p>`;
    return;
  }
  wrap.innerHTML = names.map((s) => `<button type="button" class="start-chip" data-s="${s}" aria-pressed="${drawerState.starts.includes(s)}">${s}</button>`).join("");
  for (const btn of $$(".start-chip", wrap)) {
    btn.addEventListener("click", () => {
      const s = btn.dataset.s;
      if (drawerState.starts.includes(s)) drawerState.starts = drawerState.starts.filter((x) => x !== s);
      else drawerState.starts.push(s);
      btn.setAttribute("aria-pressed", drawerState.starts.includes(s));
    });
  }
}

function updateCatalogNote() {
  const branch = drawerState.branch, facility = drawerState.facility === FACILITY_ANY ? null : drawerState.facility;
  const halls = facility ? catalogHalls(branch, facility) : [];
  const levels = facility ? catalogLevels(branch, facility) : [];
  const note = $("#catalog-note");
  if (!halls.length && !levels.length) {
    note.hidden = false;
    note.textContent = "Listeler boşsa önce 'Branşları getir'e basın (giriş gerekir). Salonlar ve saatler yaklaşık 3 gün ileriyi gösterir; zamanla zenginleşir.";
  } else {
    note.hidden = true;
  }
}

const HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;

function collectDrawerForm() {
  return {
    name: $("#field-name").value.trim(),
    branch: drawerState.branch,
    facility: drawerState.facility,
    halls: drawerState.halls,
    level: drawerState.level,
    days: drawerState.days,
    time_mode: drawerState.time_mode,
    time_from: drawerState.time_from,
    time_to: drawerState.time_to,
    starts: drawerState.starts,
    enabled: drawerTarget ? drawerTarget.enabled : true,
  };
}

function formToTarget(values, existingId) {
  if (!values.name) throw new Error("Ad boş olamaz");
  if (!values.branch) throw new Error("Branş seçilmelidir");
  if (!values.facility) throw new Error("Tesis seçilmelidir");
  const facility = values.facility === FACILITY_ANY ? "*" : values.facility;
  const halls = values.halls.filter(Boolean);
  const level_prefix = values.level === LEVEL_ANY ? "" : values.level;
  const days = [...values.days].sort((a, b) => a - b);
  if (!days.length) throw new Error("En az bir gün seçilmelidir");

  let starts = [], time_from = null, time_to = null;
  if (values.time_mode === "specific") {
    starts = [...new Set(values.starts)].sort();
    if (!starts.length) throw new Error("'Belirli saatler' seçiliyken en az bir saat eklenmelidir");
    for (const s of starts) if (!HHMM_RE.test(s)) throw new Error(`Geçersiz saat: ${s}`);
  } else {
    time_from = values.time_from === TIME_ANY ? null : values.time_from;
    time_to = values.time_to === TIME_ANY ? null : values.time_to;
    if (time_from && !HHMM_RE.test(time_from)) throw new Error(`Başlangıç saati geçersiz: ${time_from}`);
    if (time_to && !HHMM_RE.test(time_to)) throw new Error(`Bitiş saati geçersiz: ${time_to}`);
    if (time_from && time_to && time_from > time_to) throw new Error(`Başlangıç saati bitiş saatinden sonra olamaz (${time_from} > ${time_to})`);
  }

  return {
    id: existingId || undefined,
    name: values.name,
    enabled: values.enabled,
    branch: values.branch,
    facility,
    halls,
    level_prefix,
    days: days.map((d) => WEEKDAYS_EN[d]),
    time_from, time_to,
    starts,
  };
}

async function saveDrawer() {
  let target;
  try {
    target = formToTarget(collectDrawerForm(), drawerTarget?.id);
  } catch (e) {
    $("#drawer-error").textContent = e.message;
    return;
  }
  $("#drawer-error").textContent = "";
  const targets = [...(state.settings.targets || [])];
  if (drawerTarget) {
    const idx = targets.findIndex((t) => t.id === drawerTarget.id);
    if (idx >= 0) targets[idx] = target;
  } else {
    targets.push(target);
  }
  const full = { ...state.settings, targets };
  try {
    const res = await api.putSettings(full);
    if (res && res.ok) {
      state.settings = res.settings;
      render();
      closeDrawer();
      toast("success", "Hedef kaydedildi");
    } else {
      $("#drawer-error").textContent = (res && res.error) || "Kaydedilemedi";
    }
  } catch {
    $("#drawer-error").textContent = "Sunucuya bağlanılamadı";
  }
}

function wireDrawer() {
  $("#btn-drawer-close").innerHTML = icon("x");
  $("#btn-drawer-close").addEventListener("click", closeDrawer);
  $("#btn-drawer-cancel").addEventListener("click", closeDrawer);
  $("#btn-drawer-save").addEventListener("click", saveDrawer);
  $("#drawer-scrim").addEventListener("click", closeDrawer);
  $("#btn-add-target").addEventListener("click", () => openDrawer(null));
  $("#btn-add-target-empty").addEventListener("click", () => openDrawer(null));
  $("#btn-add-target").innerHTML = `${icon("plus")}<span>Hedef ekle</span>`;
  $("#btn-add-target-empty").innerHTML = `${icon("plus")}<span>Hedef ekle</span>`;

  for (const btn of $$("#time-mode-segmented button")) {
    btn.addEventListener("click", () => setTimeMode(btn.dataset.mode));
  }
  $("#btn-add-custom-start").addEventListener("click", () => {
    const input = $("#field-custom-start");
    const v = input.value.trim();
    if (v && HHMM_RE.test(v)) {
      if (!drawerState.starts.includes(v)) drawerState.starts.push(v);
      input.value = "";
      populateStartChips();
    } else if (v) {
      $("#drawer-error").textContent = `Geçersiz saat: ${v} (HH:MM biçiminde olmalı)`;
    }
  });
  $("#btn-scan-branches").addEventListener("click", () => runScan("branches", null));
  $("#btn-scan-facilities").addEventListener("click", () => {
    if (!drawerState.branch) { $("#drawer-error").textContent = "Önce bir branş seçin."; return; }
    runScan("facilities", [drawerState.branch]);
  });
  $("#btn-scan-grids").addEventListener("click", () => {
    if (!drawerState.branch) { $("#drawer-error").textContent = "Önce bir branş seçin."; return; }
    runScan("grids", [drawerState.branch]);
  });

  $("#btn-delete-target").addEventListener("click", async () => {
    if (!drawerTarget) return;
    const ok = await confirmDialog({ title: "Hedef silinsin mi?", text: `"${drawerTarget.name}" hedefi kalıcı olarak silinecek.`, okText: "Sil" });
    if (!ok) return;
    const targets = state.settings.targets.filter((x) => x.id !== drawerTarget.id);
    await saveSettingsPatch({ targets });
    closeDrawer();
  });

  document.addEventListener("keydown", (e) => {
    const drawer = $("#target-drawer");
    if (!drawer.classList.contains("show")) return;
    if (e.key === "Escape") closeDrawer();
    else if (e.key === "Tab") trapTabKey(e, drawer); // fix list item 6: focus trap
  });
}

async function runScan(depth, branches) {
  try {
    const res = await api.command("scan", { depth, branches });
    if (!res || !res.ok) toast("error", (res && res.error) || "Tarama başlatılamadı");
  } catch {
    toast("error", "Sunucuya bağlanılamadı");
  }
}

// --------------------------------------------------------------- catalog UI

function renderDrawerCatalogProgress() {
  const row = $("#scan-progress-row");
  const prog = state.catalog_progress;
  if (!prog) { row.hidden = true; return; }
  row.hidden = false;
  const pct = prog.total > 0 ? Math.round((prog.done / prog.total) * 100) : 0;
  $("#scan-progress-fill").style.width = `${pct}%`;
  $("#scan-progress-label").textContent = `${prog.label} (${prog.done}/${prog.total})`;
}

async function maybeRefreshCatalog() {
  if (state.catalog_version !== catalogVersion) {
    try {
      const res = await api.catalog();
      catalog = res.catalog;
      catalogVersion = res.version;
      if ($("#target-drawer").classList.contains("show")) {
        populateBranchSelect(); populateFacilitySelect(); populateLevelSelect();
        populateHallChips(); populateStartChips(); updateCatalogNote();
      }
    } catch { /* keep stale catalog, try again next poll */ }
  }
}

// --------------------------------------------------------------- reservations

async function renderReservations() {
  const items = state.reservations;
  const tbody = $("#reservations-tbody");
  const emptyEl = $("#reservations-empty");
  if (!items) {
    tbody.innerHTML = "";
    emptyEl.hidden = false;
    emptyEl.querySelector("p").textContent = "Rezervasyon listesi henüz alınmadı. \"Yenile\" ile getirin.";
    return;
  }
  emptyEl.hidden = items.length > 0;
  emptyEl.querySelector("p").textContent = "Kayıtlı rezervasyon bulunamadı. \"Yenile\" ile tekrar deneyin.";
  tbody.innerHTML = items.map((r) => {
    const muted = r.status === "Seçim İptal Edildi";
    return `<tr class="${muted ? "muted-row" : ""}" data-date="${r.date}" data-start="${r.start}">
      <td>${fmtTurkishDate(r.date)}</td>
      <td class="tabular">${r.start}–${r.end}</td>
      <td>${escapeHtml(r.facility)}</td>
      <td>${escapeHtml(r.hall)}</td>
      <td><span class="badge ${muted ? "muted" : ""}">${escapeHtml(r.status)}</span></td>
      <td>${r.cancellable ? `<button class="btn btn-danger-outline btn-sm" data-act="cancel">İptal et</button>` : ""}</td>
    </tr>`;
  }).join("");
  for (const btn of $$('[data-act="cancel"]', tbody)) {
    btn.addEventListener("click", (e) => {
      const tr = e.target.closest("tr");
      cancelReservation(tr.dataset.date, tr.dataset.start);
    });
  }
}

async function cancelReservation(date, start) {
  const ok = await confirmDialog({
    title: "Rezervasyon iptal edilsin mi?",
    text: `${fmtTurkishDate(date)} ${start} seansındaki rezervasyonunuz iptal edilecek.`,
    okText: "İptal et",
  });
  if (!ok) return;
  try {
    const res = await api.command("cancel", { date, start });
    if (res && res.ok) toast("success", "Rezervasyon iptal edildi");
    else toast("error", (res && res.error) || "İptal edilemedi");
  } catch {
    toast("error", "Sunucuya bağlanılamadı");
  }
}

// ------------------------------------------------------------------- account

function renderAccount() {
  const acc = state.account || {};
  const accounts = acc.accounts || [];
  const running = !!(state.engine && state.engine.running);

  // --- "Giriş yapılı hesap" card ---------------------------------------
  const nameEl = $("#active-account-name");
  const activeStored = accounts.find((a) => a.id === acc.active_id);
  if (acc.name) {
    nameEl.textContent = acc.name;
    nameEl.classList.remove("muted");
  } else if (activeStored && activeStored.name) {
    nameEl.textContent = acc.logged_in ? activeStored.name : `${activeStored.name} (oturum kapalı)`;
    nameEl.classList.toggle("muted", !acc.logged_in);
  } else {
    nameEl.textContent = acc.logged_in ? "Ad soyad henüz okunmadı" : "Henüz giriş yapılmadı";
    nameEl.classList.add("muted");
  }
  const tcEl = $("#active-account-tc");
  tcEl.textContent = acc.tc_masked || "";
  tcEl.hidden = !acc.tc_masked;

  const pill = $("#account-status-pill");
  const pillText = $("#account-status-pill-text");
  if (!acc.has_credentials) {
    pill.dataset.state = "off";
    pillText.textContent = "Hesap yok";
  } else if (acc.logged_in) {
    pill.dataset.state = "ok";
    pillText.textContent = "Giriş yapılı";
  } else {
    pill.dataset.state = "warn";
    pillText.textContent = "Oturum kapalı — yenileniyor";
  }

  const row = $("#account-status-row");
  row.innerHTML = (acc.has_credentials && acc.keyring_ok === false)
    ? `<span class="status-badge warn">${icon("alert", "sm")} Sistem anahtarlığı kullanılamıyor</span>` : "";

  $("#login-result-text").textContent = acc.login_text
    ? `${acc.login_text}${acc.attempts_left != null && acc.attempts_left < 3 ? ` · Otomatik giriş hakkı: ${acc.attempts_left}/3 (hesabınızı korumak için 15 dakikada en fazla 3 otomatik giriş yapılır)` : ""}` : "";

  // --- "Kayıtlı hesaplar" list ------------------------------------------
  const list = $("#accounts-list");
  if (!accounts.length) {
    list.innerHTML = `<div class="account-empty">Henüz kayıtlı hesap yok. Aşağıdan bir hesap ekleyin.</div>`;
  } else {
    list.innerHTML = accounts.map((a) => {
      const isActive = a.id === acc.active_id;
      const nameHtml = a.name ? escapeHtml(a.name) : `<span class="muted-text">İsim henüz alınmadı</span>`;
      const switchBtn = isActive ? "" : `<button class="btn btn-secondary btn-sm" data-act="switch" type="button"${running ? ` disabled title="Önce botu durdurun"` : ""}>Bu hesaba geç</button>`;
      return `
        <div class="account-row ${isActive ? "active-row" : ""}" data-id="${escapeHtml(a.id)}">
          <div class="account-avatar" aria-hidden="true">${escapeHtml(nameInitials(a.name))}</div>
          <div class="account-row-main">
            <div class="account-row-name">${nameHtml}${isActive ? `<span class="account-badge-active">Aktif</span>` : ""}</div>
            <div class="account-row-tc">${escapeHtml(a.tc_masked || "")}</div>
          </div>
          <div class="account-row-actions">
            ${switchBtn}
            <button class="btn btn-ghost-danger btn-icon btn-sm" data-act="delete" type="button" aria-label="${escapeHtml(a.name || a.tc_masked || "Hesabı")} sil">${icon("trash", "sm")}</button>
          </div>
        </div>`;
    }).join("");
    for (const rowEl of $$(".account-row", list)) {
      const acct = accounts.find((a) => a.id === rowEl.dataset.id);
      if (!acct) continue;
      rowEl.querySelector('[data-act="switch"]')?.addEventListener("click", () => switchAccount(acct));
      rowEl.querySelector('[data-act="delete"]')?.addEventListener("click", () => deleteAccount(acct));
    }
  }

  // --- "Hesap ekle" collapsible form ------------------------------------
  const toggleBtn = $("#btn-toggle-add-account");
  const form = $("#add-account-form");
  if (!accounts.length) {
    toggleBtn.hidden = true;
    form.hidden = false;
  } else {
    toggleBtn.hidden = false;
    toggleBtn.setAttribute("aria-expanded", String(addAccountOpen));
    form.hidden = !addAccountOpen;
  }

  const membSel = $("#select-membership");
  const memberships = state.memberships || [];
  const current = state.settings?.membership_text || "";
  const options = ["Otomatik (ilk uygun üyelik)", ...memberships];
  membSel.innerHTML = options.map((m, i) => `<option value="${i === 0 ? "" : escapeHtml(m)}">${escapeHtml(m)}</option>`).join("");
  membSel.value = current;
}

const TC_RE = /^\d{11}$/;

async function switchAccount(account) {
  toast("success", "Hesap değiştiriliyor…");
  try {
    const res = await api.switchAccount(account.id);
    if (!res || !res.ok) toast("error", (res && res.error) || "Hesap değiştirilemedi");
  } catch { toast("error", "Sunucuya bağlanılamadı"); }
}

async function deleteAccount(account) {
  const label = account.name || account.tc_masked || "Bu hesap";
  const ok = await confirmDialog({
    title: "Hesap silinsin mi?",
    text: `"${label}" hesabı bu bilgisayardan silinsin mi?`,
    okText: "Sil",
  });
  if (!ok) return;
  try {
    const res = await api.clearCredentials(account.id);
    if (res && res.ok) toast("success", "Hesap silindi");
    else toast("error", (res && res.error) || "Silinemedi");
  } catch { toast("error", "Sunucuya bağlanılamadı"); }
}

function wireAccount() {
  $("#btn-toggle-password").innerHTML = icon("eye");
  $("#btn-toggle-password").addEventListener("click", () => {
    const input = $("#input-password");
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    $("#btn-toggle-password").innerHTML = icon(show ? "eyeOff" : "eye");
    $("#btn-toggle-password").setAttribute("aria-label", show ? "Şifreyi gizle" : "Şifreyi göster");
  });

  $("#btn-toggle-add-account").addEventListener("click", () => {
    addAccountOpen = !addAccountOpen;
    render();
    if (addAccountOpen) setTimeout(() => $("#input-tc").focus(), 60);
  });

  $("#btn-save-credentials").addEventListener("click", async () => {
    const tc = $("#input-tc").value.trim();
    const password = $("#input-password").value;
    const errorEl = $("#add-account-error");
    errorEl.textContent = "";
    if (!TC_RE.test(tc)) { errorEl.textContent = "TC Kimlik No 11 haneli rakamdan oluşmalı"; return; }
    if (!password) { errorEl.textContent = "Şifre gerekli"; return; }
    try {
      const res = await api.saveCredentials(tc, password);
      if (res && res.ok) {
        await api.command("check_login").catch(() => {});
        toast("success", "Hesap bilgileri kaydedildi");
        $("#input-password").value = "";
        $("#input-tc").value = "";
        addAccountOpen = false;
        render();
      } else {
        errorEl.textContent = (res && res.error) || "Kaydedilemedi";
      }
    } catch { errorEl.textContent = "Sunucuya bağlanılamadı"; }
  });

  $("#btn-check-login").addEventListener("click", async () => {
    try {
      const res = await api.command("check_login");
      if (!res || !res.ok) toast("error", (res && res.error) || "Bağlantı denetlenemedi");
    } catch { toast("error", "Sunucuya bağlanılamadı"); }
  });

  $("#btn-manual-login").addEventListener("click", async () => {
    try { await api.command("manual_login"); } catch { toast("error", "Sunucuya bağlanılamadı"); }
  });

  $("#btn-list-memberships").addEventListener("click", async () => {
    try {
      const res = await api.command("list_memberships");
      if (!res || !res.ok) toast("error", (res && res.error) || "Üyelikler getirilemedi");
    } catch { toast("error", "Sunucuya bağlanılamadı"); }
  });

  $("#select-membership").addEventListener("change", (e) => {
    saveSettingsPatch({ membership_text: e.target.value });
  });

  // Fix list item 7: "Tanı paketi oluştur" -- builds a redacted diagnostics zip on the
  // Desktop via src.diagnostics.create_diagnostics_zip (packaging engineer's module;
  // the server returns a Turkish error if it isn't available yet).
  $("#btn-export-diagnostics").addEventListener("click", async () => {
    const btn = $("#btn-export-diagnostics");
    btn.disabled = true;
    try {
      const res = await api.command("export_diagnostics");
      if (res && res.ok && res.path) {
        const fileName = String(res.path).split(/[\\/]/).pop();
        toast("success", `Masaüstüne kaydedildi: ${fileName} — bu dosyayı geliştiriciye gönderin`);
      } else {
        toast("error", (res && res.error) || "Tanı paketi oluşturulamadı");
      }
    } catch {
      toast("error", "Sunucuya bağlanılamadı");
    } finally {
      btn.disabled = false;
    }
  });
}

// ----------------------------------------------------------------- SMS modal

let smsAudioCtx = null;
let lastSmsDeadline = null;
let smsOpen = false;
let smsTotalMs = null;       // deadline - time-at-open (fix list item 1); not a hardcoded 180s
let smsBeepTimer = null;     // re-beep every 15s while the modal stays open
let smsTitleFlashTimer = null;
let smsTitleFlashOn = false;

const SMS_BEEP_INTERVAL_MS = 15000;
const SMS_TITLE_FLASH_MS = 1000;
const SMS_TITLE_ALERT = "🔔 SMS kodu bekleniyor!";
const SMS_TITLE_IDLE = "Spor İstanbul Rezervasyon Botu";

function beep() {
  try {
    smsAudioCtx = smsAudioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const ctx = smsAudioCtx;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    osc.type = "sine";
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
    osc.connect(gain); gain.connect(ctx.destination);
    osc.start(); osc.stop(ctx.currentTime + 0.4);
  } catch { /* audio unavailable, ignore */ }
}

function _stopSmsAttentionTimers() {
  if (smsBeepTimer) { clearInterval(smsBeepTimer); smsBeepTimer = null; }
  if (smsTitleFlashTimer) { clearInterval(smsTitleFlashTimer); smsTitleFlashTimer = null; }
  smsTitleFlashOn = false;
}

function renderSms() {
  const sms = state.sms;
  const scrim = $("#sms-scrim");
  if (!sms) {
    if (smsOpen) {
      scrim.classList.remove("show");
      document.title = SMS_TITLE_IDLE;
      smsOpen = false;
      lastSmsDeadline = null;
      smsTotalMs = null;
      _stopSmsAttentionTimers();
    }
    return;
  }
  $("#sms-card-text").textContent = sms.card || "";
  $("#sms-error").textContent = sms.error || "";
  if (!smsOpen) {
    smsOpen = true;
    scrim.classList.add("show");
    // Fix list item 1: total = deadline - time at open, not a hardcoded 180s, so the
    // countdown ring reflects however long the engine actually gave for this SMS.
    smsTotalMs = sms.deadline
      ? Math.max(1000, new Date(sms.deadline).getTime() - serverNow().getTime())
      : 180000;
    document.title = SMS_TITLE_ALERT;
    beep();
    smsBeepTimer = setInterval(beep, SMS_BEEP_INTERVAL_MS);
    smsTitleFlashTimer = setInterval(() => {
      smsTitleFlashOn = !smsTitleFlashOn;
      document.title = smsTitleFlashOn ? SMS_TITLE_ALERT : SMS_TITLE_IDLE;
    }, SMS_TITLE_FLASH_MS);
    $("#sms-input").value = "";
    setTimeout(() => $("#sms-input").focus(), 60);
  }
  if (sms.deadline !== lastSmsDeadline) lastSmsDeadline = sms.deadline;
  updateSmsCountdown();
}

function updateSmsCountdown() {
  if (!state.sms || !state.sms.deadline) return;
  const totalMs = smsTotalMs || 180000;
  const remainMs = Math.max(0, new Date(state.sms.deadline).getTime() - serverNow().getTime());
  const remainS = Math.round(remainMs / 1000);
  $("#sms-ring-text").textContent = remainS;
  const frac = Math.min(1, remainMs / totalMs);
  const circumference = 175.9;
  $("#sms-ring-fg").style.strokeDashoffset = String(circumference * (1 - frac));
}

function wireSms() {
  $("#sms-input").addEventListener("keydown", (e) => { if (e.key === "Enter") submitSms(); });
  $("#btn-sms-submit").addEventListener("click", submitSms);
  // Fix list item 6: focus trap while open -- Tab cycles inside, but (deliberately,
  // unlike the confirm dialog and drawer) Escape does NOT close this modal.
  document.addEventListener("keydown", (e) => {
    if (!smsOpen) return;
    if (e.key === "Tab") trapTabKey(e, $(".sms-dialog", $("#sms-scrim")));
  });
}

async function submitSms() {
  const code = $("#sms-input").value.trim();
  if (!/^\d{4,8}$/.test(code)) { $("#sms-error").textContent = "Kod 4-8 haneli rakam olmalı"; return; }
  try {
    const res = await api.command("sms", { code });
    if (!res || !res.ok) $("#sms-error").textContent = (res && res.error) || "Kod doğrulanamadı";
  } catch {
    $("#sms-error").textContent = "Sunucuya bağlanılamadı";
  }
}

// ---------------------------------------------------------------- hero actions

async function onStartClick() {
  try {
    const res = await api.command("start", { dry_run: pendingDryRun });
    if (!res || !res.ok) toast("error", (res && res.error) || "Başlatılamadı");
  } catch { toast("error", "Sunucuya bağlanılamadı"); }
}
async function onStopClick() {
  try { await api.command("stop"); } catch { toast("error", "Sunucuya bağlanılamadı"); }
}

function wireHero() {
  $("#toggle-dry-run").addEventListener("change", (e) => { pendingDryRun = e.target.checked; });
  $("#toggle-stop-after-success").addEventListener("change", (e) => {
    saveSettingsPatch({ stop_after_success: e.target.checked });
  });
  $("#toggle-reminders-enabled").addEventListener("change", (e) => {
    saveSettingsPatch({ reminders_enabled: e.target.checked });
  });
  $("#btn-toggle-browser").addEventListener("click", async () => {
    const next = !browserShownLocal;
    try {
      const res = await api.command(next ? "show_browser" : "hide_browser");
      if (res && res.ok) { browserShownLocal = next; render(); }
      else toast("error", (res && res.error) || "Komut başarısız oldu");
    } catch { toast("error", "Sunucuya bağlanılamadı"); }
  });
}

function wireReservations() {
  $("#btn-refresh-reservations").innerHTML = `${icon("refresh", "sm")}<span>Yenile</span>`;
  $("#btn-refresh-reservations").addEventListener("click", async () => {
    try {
      const res = await api.command("list_reservations");
      if (!res || !res.ok) toast("error", (res && res.error) || "Liste alınamadı");
    } catch { toast("error", "Sunucuya bağlanılamadı"); }
  });
}

function wireSidebar() {
  $("#btn-quit-browser").innerHTML = `${icon("x", "sm")}<span class="label">Uygulamayı kapat</span>`;
  $("#btn-quit-browser").addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Uygulama kapatılsın mı?", text: "Bot ve bu pencere kapatılacak.", okText: "Kapat" });
    if (!ok) return;
    try { await api.command("quit"); } catch { /* window will close regardless */ }
  });
}

// --------------------------------------------------------------- connection

function setConnLost(lost) {
  if (lost === connLost) return;
  connLost = lost;
  const bar = $("#conn-bar");
  bar.hidden = !lost;
  if (lost) bar.innerHTML = `${icon("wifi", "sm")}<span>Bağlantı kesildi, yeniden deneniyor…</span>`;
  announce(lost ? "Bağlantı kesildi, yeniden deneniyor" : "Bağlantı sağlandı");
}

// -------------------------------------------------------------------- loop

async function pollLoop() {
  // -1: the server treats this as "no previous version", returning immediately with
  // whatever it currently has. Starting at 0 would collide with a freshly started
  // server (whose version starts at 0 too) and stall the very first paint for up to
  // the full long-poll timeout -- confirmed against the real src/webapp.py.
  let since = -1;
  let backoff = 1000;
  while (true) {
    // Network/fetch failures and render failures are DIFFERENT problems and must not
    // share a catch block: a render() exception is not "the connection dropped", and
    // treating it as one used to flip the "Bağlantı kesildi" bar, silently re-fetch the
    // same already-known version forever (since `since` only advances on success), and
    // never surface what actually broke -- so a page that fails to render once would
    // appear to hang at its last-rendered (or, on the very first render, placeholder)
    // state forever, with no visible error.
    let data;
    try {
      data = await api.state(since);
      setConnLost(false);
      backoff = 1000;
    } catch (e) {
      setConnLost(true);
      await sleep(backoff);
      backoff = Math.min(backoff * 1.6, 15000);
      continue;
    }

    try {
      const clientNow = Date.now();
      if (data.now) serverNowOffsetMs = new Date(data.now).getTime() - clientNow;
      since = data.version; // advance regardless of render outcome -- see note above
      state = data;
      if (!pendingDryRunInitialized && typeof data.dry_run_ui === "boolean") {
        pendingDryRun = data.dry_run_ui;
        pendingDryRunInitialized = true;
      }
      await maybeRefreshCatalog();
      renderDrawerCatalogProgress();
      render();
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("Arayüz güncellenemedi (render hatası):", e);
      toast("error", `Arayüz güncellenirken bir sorun oluştu: ${e && e.message ? e.message : e}`);
    }
  }
}
let pendingDryRunInitialized = false;

function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// ---------------------------------------------------------------------- tick

function tickCountdowns() {
  if (!state) return;
  if (state.next_window) {
    const node = $("#nw-countdown");
    if (node) node.textContent = fmtCountdown(state.next_window.open_at);
  }
  if (state.sms) updateSmsCountdown();
  if (state.engine && state.engine.running) renderLastPoll();
}
setInterval(tickCountdowns, 1000);

// --------------------------------------------------------------------- init

function init() {
  initNav();
  wireDrawer();
  wireSms();
  wireHero();
  wireAccount();
  wireReservations();
  wireSidebar();
  pollLoop();
}

document.addEventListener("DOMContentLoaded", init);
