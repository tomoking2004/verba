"use strict";

/* The UI only mirrors the server's state. The source of truth for a session is session.json on disk. */

const $ = (id) => document.getElementById(id);
const ME = "自分", OTHER = "相手";  // SPEAKER_ME and SPEAKER_OTHER in verba/config.py
const WEEK = "日月火水木金土";
const UNTITLED = "無題の記録";  // UNTITLED in verba/store.py
const FADE = 200;  // Time for a toast to fade out (a little longer than --dur in style.css)

/* The icons are one set: drawn on a 16 grid with a 1.5 stroke, round caps and joins. In HTML, write <svg data-icon="name">. */
/* The only icons outside the set are the two glyphs inside the round play button (index.html): filled, on a 12 grid, because a stroke is too thin on a filled disc. */
const MIC = '<rect x="6" y="2" width="4" height="7.5" rx="2"/><path d="M3.5 7.5a4.5 4.5 0 0 0 9 0M8 12v2"/>';
const SPEAKER = '<path d="M2.5 6.2h2.3L8 3.5v9L4.8 9.8H2.5z"/><path d="M10.5 6a2.8 2.8 0 0 1 0 4M12.3 4.2a5.4 5.4 0 0 1 0 7.6"/>';
const SLASH = '<path d="M2.5 2.5l11 11"/>';  // Drawn over a track's icon while the track is muted
const ICONS = {
  plus: '<path d="M8 3v10M3 8h10"/>',
  gear: '<path d="M12.78 6.52 L14.50 6.88 L14.50 9.12 L12.78 9.48 L12.42 10.33 L13.39 11.81 L11.81 13.39 L10.33 12.42 L9.48 12.78 L9.12 14.50 L6.88 14.50 L6.52 12.78 L5.67 12.42 L4.19 13.39 L2.61 11.81 L3.58 10.33 L3.22 9.48 L1.50 9.12 L1.50 6.88 L3.22 6.52 L3.58 5.67 L2.61 4.19 L4.19 2.61 L5.67 3.58 L6.52 3.22 L6.88 1.50 L9.12 1.50 L9.48 3.22 L10.33 3.58 L11.81 2.61 L13.39 4.19 L12.42 5.67Z"/><circle cx="8" cy="8" r="2"/>',
  book: '<path d="M8 4.5C6.5 3.5 4.5 3.2 2.5 3.5v8.8c2-.3 4 0 5.5 1 1.5-1 3.5-1.3 5.5-1V3.5c-2-.3-4 0-5.5 1zM8 4.5v8.8"/>',
  help: '<circle cx="8" cy="8" r="6.25"/><path d="M6.3 6.4a1.75 1.75 0 1 1 2.45 1.6c-.45.2-.75.6-.75 1.1v.3M8 11.3v.2"/>',
  copy: '<rect x="5.5" y="5.5" width="8" height="8" rx="1.5"/><path d="M10.5 5.5V4A1.5 1.5 0 0 0 9 2.5H4A1.5 1.5 0 0 0 2.5 4v5A1.5 1.5 0 0 0 4 10.5h1.5"/>',
  folder: '<path d="M2.5 4.5A1.5 1.5 0 0 1 4 3h2.4l1.5 1.5H12A1.5 1.5 0 0 1 13.5 6v5.5A1.5 1.5 0 0 1 12 13H4a1.5 1.5 0 0 1-1.5-1.5z"/>',
  trash: '<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8a1 1 0 0 0 1 .9h3.8a1 1 0 0 0 1-.9l.6-8M6.8 7v4M9.2 7v4"/>',
  redo: '<path d="M12.5 8A4.5 4.5 0 1 1 11 4.6M11.5 2.2V5H8.7"/>',
  arrowLeft: '<path d="M13 8H3M7.5 3.5 3 8l4.5 4.5"/>',
  arrowRight: '<path d="M3 8h10M8.5 3.5 13 8l-4.5 4.5"/>',
  cross: '<path d="M4 4l8 8M12 4l-8 8"/>',
  chevron: '<path d="M4.5 6.5 8 10l3.5-3.5"/>',
  mic: MIC, micOff: MIC + SLASH,
  speaker: SPEAKER, speakerOff: SPEAKER + SLASH,
  pause: '<path d="M6 3.5v9M10 3.5v9"/>',
  play: '<path d="M5 3.3v9.4L12.5 8z"/>',
  stop: '<rect x="4" y="4" width="8" height="8" rx="1"/>',
  check: '<path pathLength="1" d="M3.5 8.5l3 3 6-7"/>',
  chat: '<path d="M4 3h8A1.5 1.5 0 0 1 13.5 4.5v5A1.5 1.5 0 0 1 12 11H7.5L4.5 13.5V11H4A1.5 1.5 0 0 1 2.5 9.5v-5A1.5 1.5 0 0 1 4 3z"/>',
};
const icon = (name) => `<svg class="icon" viewBox="0 0 16 16" aria-hidden="true">${ICONS[name]}</svg>`;
for (const el of document.querySelectorAll("svg[data-icon]")) el.outerHTML = icon(el.dataset.icon);

let config = null;          // /api/config
let devices = null;         // /api/devices (asked again while the new-recording view is shown: devices come and go)
let sessions = [];          // The list
let current = null;         // The session on screen (/api/sessions/<id>)
let live = null;            // The live recording (recording in /api/state)
let model = null;           // The model (model in /api/state): the chosen one, the one in effect now, the ones out of quota, and the last switch
let heard = { session: null };  // The other side in the live recording: its loudest level, and for how long it was listened to (for the "not arriving" warning)
let hits = {};              // Sessions the search matched by an utterance (id -> the hit)

/* ---------- Common ---------- */
async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  }).catch(() => { throw new Error("アプリの内部と通信できませんでした。Verba を開き直してください"); });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "処理に失敗しました。もう一度試してください");
  return data;
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function clock(sec) {
  const s = Math.max(0, Math.floor(sec)), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(r)}` : `${p(m)}:${p(r)}`;
}
function durationLabel(sec) {
  const s = Math.floor(sec), h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
  if (s < 60) return `${s} 秒`;
  return h ? `${h} 時間${m ? ` ${m} 分` : ""}` : `${m} 分`;
}
const timeLabel = (d) => `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
function dateLabel(iso) {
  const d = new Date(iso);
  return `${d.getMonth() + 1}/${d.getDate()}(${WEEK[d.getDay()]}) ${timeLabel(d)}`;
}
const modelName = (id) => config.models.find((m) => m.id === id)?.name || id;
/* When an artifact was made, and by which model (HTML): the time, and apart from it the model that made most of it.
   ids holds the model of each part (a segment), in order. When others made some parts, "ほか" follows, and hovering it lists the models with their counts. */
function madeLabel(iso, ids) {
  const counts = new Map();
  for (const id of ids) counts.set(id, (counts.get(id) || 0) + 1);
  const ranked = [...counts].sort((a, b) => b[1] - a[1]);  // A stable sort: on a tie, the one used first leads.
  let by = "";
  if (ranked.length === 1) by = `<span>${esc(modelName(ranked[0][0]))}</span>`;
  else if (ranked.length > 1) {
    const list = ranked.map(([id, n]) => `<span class="nowrap">${esc(modelName(id))}　${n} 区間</span>`).join("<br>");
    by = `<span>${esc(modelName(ranked[0][0]))} <span class="why end"><span class="others" tabindex="0">ほか</span><span class="tip" role="tooltip">${list}</span></span></span>`;
  }
  return `<span>${dateLabel(iso)} 作成</span>${by}`;
}
/* When a model out of quota comes back: a time of day (with the day, when that is not today). */
function backLabel(iso) {
  const d = new Date(iso), days = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date().setHours(0, 0, 0, 0)) / 86400000);
  return `${days <= 0 ? "" : days === 1 ? "明日 " : `${d.getMonth() + 1}/${d.getDate()} `}${timeLabel(d)}`;
}
let toastTimer;
function toast(msg, error) {
  const t = $("toast");
  t.textContent = msg;  // A failure looks the same and stays longer. Its text says what happened and why, in one line.
  t.classList.remove("leaving"); t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.classList.add("leaving"); toastTimer = setTimeout(() => (t.hidden = true), FADE); }, error ? 6000 : 2600);
}
function fail(e) { toast(e.message, true); }
/* After a successful copy, the button's icon turns into a check mark for a while. */
function confirmCopied(button) {
  const svg = button.querySelector("svg");
  if (!svg || svg.classList.contains("drawn")) return;
  svg.outerHTML = icon("check").replace('class="icon"', 'class="icon drawn"');
  setTimeout(() => { const now = button.querySelector("svg"); if (now) now.outerHTML = icon("copy"); }, 1600);
}
/* A disabled button tells why (HTML) when its wrapper (.why) is hovered: a disabled button itself gets no mouse events. An empty reason enables the button. */
function setWhy(button, reason) {
  const tip = button.parentElement.querySelector(".tip");
  button.disabled = !!reason; tip.innerHTML = reason; tip.hidden = !reason;
}
/* The same for a button drawn from a string. end: the button sits at the right end of its row, so the reason opens leftwards. */
const withWhy = (reason, button, end) => `<span class="why${end ? " end" : ""}">${reason ? button.replace("<button", "<button disabled") : button}<span class="tip" role="tooltip"${reason ? "" : " hidden"}>${reason}</span></span>`;
/* The form compared in a search (same as _fold in verba/store.py): full/half width and upper/lower case are not distinguished. */
const fold = (s) => s.normalize("NFKC").toLowerCase();

/* ---------- Menus ---------- */
/* A menu opens from its button (which names it in aria-controls): under it, or, for a floating one (.float), wherever the window has room above or below it, clipped by no scrolling pane. The arrows move between its rows. A choice, Escape, and the focus leaving the menu and its button (a click elsewhere, Tab) close it. */
let menuOpen = null;    // The button of the open menu
let menuKept = false;   // The rows of an open menu are being redrawn: the focus they lose does not close it.
const menuOf = (button) => $(button.getAttribute("aria-controls"));
function showMenu(button, open) {
  const box = menuOf(button);
  box.hidden = !open;
  button.setAttribute("aria-expanded", open);
  if (open) { menuOpen = button; placeMenu(); }
  else if (menuOpen === button) menuOpen = null;
}
/* Put the open floating menu at its button: its right edge at the button's, below it (or above it, where below is short of room and above has more), and inside the window. */
function placeMenu() {
  const box = menuOpen && menuOf(menuOpen);
  if (!box || !box.classList.contains("float")) return;
  const at = menuOpen.getBoundingClientRect(), gap = parseFloat(getComputedStyle(box).getPropertyValue("--s1"));
  const w = box.offsetWidth, h = box.offsetHeight, below = innerHeight - at.bottom;
  const within = (v, size, room) => Math.max(gap, Math.min(v, room - size - gap));
  box.style.left = `${within(at.right - w, w, innerWidth)}px`;
  box.style.top = `${within(below < h + 2 * gap && at.top > below ? at.top - gap - h : at.bottom + gap, h, innerHeight)}px`;
}
for (const ev of ["scroll", "resize"]) addEventListener(ev, placeMenu, true);
function bindMenu(button) {
  const box = menuOf(button), rows = () => [...box.querySelectorAll("button")];
  button.addEventListener("click", () => {
    const open = box.hidden;
    showMenu(button, open);
    if (open) (box.querySelector('[aria-checked="true"]') || rows()[0]).focus();
  });
  box.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const all = rows(), i = all.indexOf(document.activeElement);
    all[(i + (e.key === "ArrowDown" ? 1 : all.length - 1)) % all.length].focus();
  });
  button.parentElement.addEventListener("focusout", (e) => { if (!menuKept && !e.currentTarget.contains(e.relatedTarget)) showMenu(button, false); });
}

/* The confirmation dialog. Resolves to true or false by the button pressed. */
/* In every two-way choice the side that backs out is a plain button and the side that goes ahead is gold (the primary action there). */
function ask({ title, body, ok, cancel = "やめる" }) {
  $("askHead").textContent = title; $("askBody").textContent = body;
  $("askOk").textContent = ok; $("askCancel").textContent = cancel;
  $("askOk").disabled = false;
  $("askModal").hidden = false;
  $("askCancel").focus();
  return new Promise((resolve) => {
    const done = (v) => { $("askModal").hidden = true; $("askOk").onclick = $("askCancel").onclick = null; resolve(v); };
    $("askOk").onclick = () => done(true);
    $("askCancel").onclick = () => done(false);
  });
}

/* ---------- List ---------- */
async function loadList() {
  sessions = await api("GET", "/api/sessions");
  renderList();
}
function renderList() {
  const q = fold($("search").value.trim());
  const box = $("list"); box.innerHTML = "";
  showRecordingNew();
  const shown = sessions.filter((s) => !q || fold(s.title || UNTITLED).includes(q) || hits[s.id]);
  if (!shown.length) { box.innerHTML = `<div class="empty">${q ? "見つかりません" : "まだ記録がありません"}</div>`; return; }
  let lastMonth = "";
  for (const s of shown) {
    const d = new Date(s.created_at), month = `${d.getFullYear()}年${d.getMonth() + 1}月`;
    if (month !== lastMonth) { box.insertAdjacentHTML("beforeend", `<div class="group">${month}</div>`); lastMonth = month; }
    const rec = live && live.session === s.id;
    const el = document.createElement("button");
    el.className = "item" + (current && current.id === s.id ? " on" : "");
    el.title = s.title || UNTITLED;
    const state = rec ? `<span class="live">${live.paused ? "一時停止中" : "録音中"}</span>` : `<span>${durationLabel(s.duration)}</span>`;
    const hit = q && !fold(s.title || UNTITLED).includes(q) ? hits[s.id] : null;
    el.innerHTML = `${icon("chat")}<span class="title"><span>${esc(s.title || UNTITLED)}</span></span>
      <span class="sub"><span>${dateLabel(s.created_at)}</span>${state}${s.failed ? `<span class="failed">失敗 ${s.failed}</span>` : ""}</span>`
      + (hit ? `<span class="hit">${esc(hit.before)}<mark>${esc(hit.match)}</mark>${esc(hit.after)}</span>` : "");
    el.addEventListener("click", () => openSession(s.id, hit ? hit.start : null));
    box.appendChild(el);
  }
}
/* The button above the list starts a recording or, while one runs, leads to it. The way back carries the recording mark; with that session on screen, the mark is in its recording bar and the button rests. */
function showRecordingNew() {
  const button = $("recordingNew"), here = !!(live && current && current.id === live.session), mode = !live ? "new" : here ? "here" : "away";
  setWhy(button, here ? "録音中の記録を、いま開いています" : "");
  button.classList.toggle("paused", !!(live && live.paused));
  if (button.dataset.mode === mode) return;
  button.dataset.mode = mode;
  button.innerHTML = `${{ new: icon("plus"), away: '<i class="dot"></i>', here: "" }[mode]}<span>${live ? "録音中の記録へ" : "新しく録音"}</span>`;
}
/* The search looks in titles and utterances. Titles are filtered on the spot; utterances are searched by the server and added when the answer arrives. */
let searchSeq = 0;
$("search").addEventListener("input", async () => {
  const q = $("search").value.trim(), seq = ++searchSeq;
  hits = {};  // The hits of the previous term do not apply to this one.
  renderList(); markFound();
  if (!q) return;
  try {
    const found = await api("GET", `/api/sessions?q=${encodeURIComponent(q)}`);
    if (seq !== searchSeq) return;
    hits = Object.fromEntries(found.filter((s) => s.hit).map((s) => [s.id, s.hit]));
    renderList();
  } catch (e) { /* The list is already filtered by title. */ }
});
$("search").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("list").querySelector(".item")?.click();
  if (e.key === "Escape" && $("search").value) { $("search").value = ""; $("search").dispatchEvent(new Event("input")); }
});
/* Mark the search term in the open transcript (the text is left untouched; only ranges are highlighted). */
function markFound() {
  if (!CSS.highlights) return;
  const q = fold($("search").value.trim()), ranges = [];
  if (q) for (const el of document.querySelectorAll("#transcriptRows .text")) {
    const node = el.firstChild;
    if (!node || node.nodeType !== Node.TEXT_NODE) continue;
    const text = fold(node.data);
    if (text.length !== node.data.length) continue;  // Folding changed the length, so positions are off: no highlight.
    for (let i = text.indexOf(q); i >= 0; i = text.indexOf(q, i + q.length)) {
      const range = new Range(); range.setStart(node, i); range.setEnd(node, i + q.length); ranges.push(range);
    }
  }
  if (ranges.length) CSS.highlights.set("found", new Highlight(...ranges)); else CSS.highlights.delete("found");
}

/* ---------- Switching views ---------- */
function show(view) {
  for (const id of ["newView", "sessionView", "emptyView"]) $(id).hidden = id !== view;
}
function showNew() {
  opening++; current = null; releaseAudio(); renderList(); show("newView");
  fillDevices(); refreshDevices();
  $("liveOn").checked = config.settings.live; $("liveOff").checked = !config.settings.live;
  showNewMute();
  $("recordingStart").disabled = !!live;
  $("newTitle").focus();
}
$("recordingNew").addEventListener("click", () => (live ? openSession(live.session) : showNew()));

let devicesShown = "";  // The device list the two selects were filled from
function fillDevices() {
  const { settings } = config;
  const fill = (sel, items, chosen, fallback) => {
    sel.innerHTML = `<option value="">Windows の既定（${esc(items.find((d) => d.id === fallback)?.name || "なし")}）</option>`
      + items.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("");
    sel.value = items.some((d) => d.id === chosen) ? chosen : "";
  };
  fill($("micSelect"), devices.mics, settings.mic, devices.default_mic);
  fill($("speakerSelect"), devices.speakers, settings.speaker, devices.default_speaker);
  $("echoCheck").checked = settings.echo_removal;
  devicesShown = JSON.stringify(devices);
}
/* Ask for the devices again, and refill the selects if they changed (not while one of them has focus: it may be open). */
async function refreshDevices() {
  try { devices = await api("GET", "/api/devices"); } catch (e) { return; }
  if (JSON.stringify(devices) !== devicesShown && !document.activeElement?.matches("#micSelect, #speakerSelect")) fillDevices();
}
for (const id of ["micSelect", "speakerSelect"]) $(id).addEventListener("blur", refreshDevices);
$("echoCheck").addEventListener("change", () => saveSettings({ echo_removal: $("echoCheck").checked }));
$("micSelect").addEventListener("change", () => saveSettings({ mic: $("micSelect").value || null }));
$("speakerSelect").addEventListener("change", () => saveSettings({ speaker: $("speakerSelect").value || null }));
for (const id of ["liveOn", "liveOff"]) $(id).addEventListener("change", () => saveSettings({ live: $("liveOn").checked }));
async function saveSettings(changes) {
  try { config.settings = await api("POST", "/api/settings", changes); } catch (e) { fail(e); }
}

/* ---------- Model ---------- */
/* The two fields (new recording, session) show the one setting, as the name of the model in effect: the chosen one, or the one that stands in for it while it is out of quota. Their menus list every model. One out of quota cannot be chosen, and tells why when hovered or focused. */
let noticeSeen;  // seq of the last switch of model told. Unset until the first poll, whose notice is old news.
function showModel(m) {
  const was = model && noModel() + noModel(true);
  model = m;
  const seq = m.notice ? m.notice.seq : null;
  if (noticeSeen !== undefined && m.notice && seq !== noticeSeen) toast(m.notice.text, true);
  noticeSeen = seq;
  fillModels();
  if (current && was !== null && noModel() + noModel(true) !== was) redrawSession();  // What asks Gemini can be pressed, or no longer.
}
const backSentence = (iso) => `<span class="nowrap">${backLabel(iso)} に回復します</span>`;
/* Why nothing can be asked of Gemini now (HTML): every model is out of today's quota. Empty while one can answer.
   careful: for the proper-noun fix, which only the models marked careful in MODELS (verba/config.py) may do. */
function noModel(careful = false) {
  const until = model ? config.models.filter((m) => !careful || m.careful).map((m) => model.blocked[m.id]) : [];
  const which = careful ? "固有名詞の直しに使えるモデルが、どれも" : "すべてのモデルが";
  return until.length && until.every(Boolean) ? `${which}今日の利用上限に達しました。${backSentence(until.sort()[0])}` : "";
}
function fillModels() {
  const shown = JSON.stringify([model.active, model.blocked]);
  for (const button of document.querySelectorAll("button[data-model]")) {
    if (button.dataset.shown === shown) continue;
    button.dataset.shown = shown;
    button.firstElementChild.textContent = modelName(model.active);
    button.setAttribute("aria-label", `モデル：${modelName(model.active)}`);
    const box = menuOf(button), focused = [...box.querySelectorAll("button")].indexOf(document.activeElement);
    menuKept = true;
    // The reason of the last row opens upwards: below it is the edge of the menu, and maybe of the window.
    box.innerHTML = config.models.map((m, i) => {
      const until = model.blocked[m.id];
      return `<span class="why"><button role="menuitemradio" aria-checked="${m.id === model.active}"${until ? ' aria-disabled="true"' : ""} data-id="${esc(m.id)}">${icon("check")}<span><b>${esc(m.name)}</b><small>${esc(m.note)}</small></span></button>`
        + `<span class="tip${i && i === config.models.length - 1 ? " up" : ""}" role="tooltip"${until ? "" : " hidden"}>${until ? `今日の利用上限に達しました。${backSentence(until)}` : ""}</span></span>`;
    }).join("");
    if (focused >= 0) box.querySelectorAll("button")[focused].focus();
    menuKept = false;
  }
  placeMenu();
}
for (const button of document.querySelectorAll("button[data-model]")) {
  bindMenu(button);
  menuOf(button).classList.add("float");
  menuOf(button).addEventListener("click", async (e) => {
    const row = e.target.closest("button");
    if (!row || row.hasAttribute("aria-disabled")) return;
    button.focus(); showMenu(button, false);
    await saveSettings({ model: row.dataset.id });
    pollState();
  });
}

/* ---------- Muting a track ---------- */
/* A muted track records silence. It can be muted before the recording starts (the new-recording view keeps the choice until the start) and at any moment while recording. */
const TRACK_NAMES = { me: "自分の声", other: "相手の声" };
function showMute(button, muted, text = "") {
  if (button.dataset.muted === String(muted)) return;
  const track = button.dataset.track, glyph = track === "me" ? "mic" : "speaker";
  button.dataset.muted = muted;
  button.innerHTML = icon(muted ? `${glyph}Off` : glyph) + text;
  button.classList.toggle("muted", muted);
  button.setAttribute("aria-pressed", muted);
  button.title = muted ? `${TRACK_NAMES[track]}のミュートを解除` : `${TRACK_NAMES[track]}をミュート`;
  button.setAttribute("aria-label", button.title);
}
const newMute = { me: false, other: false };
function showNewMute() { for (const b of [$("newMuteMe"), $("newMuteOther")]) showMute(b, newMute[b.dataset.track]); }
for (const b of [$("newMuteMe"), $("newMuteOther")]) b.addEventListener("click", () => { newMute[b.dataset.track] = !newMute[b.dataset.track]; showNewMute(); });
const muteAsked = {};  // Mutes asked of the server and not answered yet. Until then they stand over what a poll says.
const muteOpen = {};  // How many mute requests of each track are unanswered: the last one asked stands until all are answered.
const muteSent = {};  // The last mute request of each track. The next waits for it, so the server ends with the last one asked.
for (const b of [$("muteMe"), $("muteOther")]) b.addEventListener("click", async () => {
  const track = b.dataset.track, muted = !live.muted[track];
  muteAsked[track] = live.muted[track] = muted; renderRecordingBar();
  muteOpen[track] = (muteOpen[track] || 0) + 1;
  muteSent[track] = (muteSent[track] || Promise.resolve()).then(() => api("POST", "/api/recording/mute", { [track]: muted })).catch(fail);
  await muteSent[track];
  if (!--muteOpen[track]) delete muteAsked[track];
  pollState();
});

/* ---------- Recording ---------- */
$("recordingStart").addEventListener("click", async () => {
  $("recordingStart").disabled = true;
  try {
    const s = await api("POST", "/api/recording/start", { title: $("newTitle").value, memo: $("newMemo").value, muted: newMute });
    $("newTitle").value = ""; $("newMemo").value = "";
    newMute.me = newMute.other = false;  // A mute is chosen for one recording: the next one starts with both tracks on.
    await pollState();
    await loadList();
    await openSession(s.id);
  } catch (e) { fail(e); $("recordingStart").disabled = false; }
});
$("recordingPause").addEventListener("click", () => api("POST", live && live.paused ? "/api/recording/resume" : "/api/recording/pause").then(pollState).catch(fail));
$("recordingStop").addEventListener("click", async () => {
  $("recordingStop").disabled = true;
  // A stop that fails has still ended the session, so the screen is brought up to date either way.
  const redraw = async () => { await pollState(); await loadList(); await refreshSession(true); };
  try { await api("POST", "/api/recording/stop"); await redraw(); toast("録音を保存しました"); }
  catch (e) { fail(e); redraw().catch(() => {}); }
  finally { $("recordingStop").disabled = false; }
});

function renderRecordingBar() {
  const mine = live && current && live.session === current.id;
  $("recordingBar").hidden = !mine;
  if (!mine) { $("recordingWarning").hidden = true; return; }
  $("recordingState").className = "recording-state" + (live.paused ? " paused" : "");
  $("recordingLabel").textContent = live.paused ? "一時停止中" : "録音中";
  $("recordingClock").textContent = clock(live.seconds);
  const pause = $("recordingPause");
  if (pause.dataset.paused !== String(live.paused)) {
    pause.dataset.paused = live.paused;
    pause.innerHTML = live.paused ? `${icon("play")}<span>再開</span>` : `${icon("pause")}<span>一時停止</span>`;
    pause.className = live.paused ? "pearl" : "";
  }
  showLiveSegment();
  showMute($("muteMe"), live.muted.me, ME);
  showMute($("muteOther"), live.muted.other, OTHER);
  const level = (v, muted) => `scaleX(${live.paused || muted ? 0 : Math.min(1, Math.sqrt(v) * 2.6)})`;
  $("levelMe").style.transform = level(live.levels.me, live.muted.me);
  $("levelOther").style.transform = level(live.levels.other, live.muted.other);
  const errs = live.levels.errors || {};
  let warn = "";
  // Why an input stopped (the exception text) is in app.log. The UI says what happened and what is still being recorded.
  if (errs.mic) warn = "マイクから録音できなくなりました。相手の声だけを録り続けています";
  else if (errs.speaker) warn = "パソコンの音声を録音できなくなりました。自分の声だけを録り続けています";
  else if (!live.muted.other && heard.seconds > 45 && heard.peak < 0.002) warn = `相手の声がまだ届いていません。通話の音が「${esc(live.devices.speaker)}」から出ているか<span class="nowrap">確かめてください</span>`;
  const box = $("recordingWarning"), appeared = box.hidden && !!warn;
  box.innerHTML = warn && `<p>${warn}</p>`; box.hidden = !warn;
  if (appeared) box.scrollIntoView({ block: "nearest" });  // In a short window it hides below the bar, so scroll it into view when it appears.
}

/* ---------- Showing a session ---------- */
/* at: the start (seconds) of the utterance a search hit. If given, the transcript scrolls to that row. */
let opening = 0;  // Counts the changes of view. An answer that arrives after a later change is dropped.
async function openSession(id, at = null) {
  const turn = ++opening;
  let s;
  try { s = await api("GET", `/api/sessions/${id}`); }
  catch (e) {
    if (turn !== opening) return;
    fail(e); await loadList();
    if (turn === opening) showEmpty();
    return;
  }
  if (turn !== opening) return;
  current = s;
  show("sessionView"); renderList();
  if (at !== null) selectTab("transcript");
  moveTabLine(false);
  $("title").value = $("title").title = current.title; $("memo").value = current.memo;
  const audio = $("audio");
  const hasAudio = current.status === "done" && current.duration > 0;
  $("player").hidden = !hasAudio;
  // The query only makes the address new when the recording has grown (the server does not read it).
  releaseAudio();
  if (hasAudio) audio.src = `/api/sessions/${id}/audio?length=${current.duration}`;
  $("playerSeek").max = current.duration || 1; $("playerEnd").textContent = clock(current.duration);
  showPosition(0);
  renderSession(true);
  if (at !== null) [...document.querySelectorAll("#transcriptRows .utterance")].find((el) => +el.dataset.start === at)?.scrollIntoView({ block: "center" });
}
function showEmpty() { opening++; current = null; releaseAudio(); renderList(); show("emptyView"); }

async function refreshSession(force) {
  if (!current) return;
  const id = current.id;
  try {
    const s = await api("GET", `/api/sessions/${id}`);
    if (!current || current.id !== id) return;  // Another session was opened meanwhile.
    if (!force && s.rev === current.rev && s.pending === current.pending && s.summarizing === current.summarizing && s.fixing_names === current.fixing_names && s.preparing === current.preparing && s.can_move === current.can_move) return;
    const wasRecording = current.status === "recording";
    const retitled = s.title !== current.title;
    current = s;
    if (wasRecording && s.status === "done") return openSession(s.id);
    if (retitled && document.activeElement !== $("title")) { $("title").value = $("title").title = s.title; loadList(); }  // The summary gave the session a title.
    redrawSession();
  } catch (e) { /* Deleted, for example. The next list refresh reflects it. */ }
}

/* Redraw the session, unless an utterance is being edited: its text and caret would be lost. The redraw is then made when the editing ends. */
/* (Focus merely left on a pressed button does not hold a redraw back.) */
let redrawOwed = false;
function redrawSession() {
  if (document.activeElement?.matches("#transcriptRows .text")) redrawOwed = true;
  else renderSession(false);
}

function renderSession(scrollTop) {
  const s = current;
  const recording = s.status === "recording";
  // Progress and failures of the transcription are told by rows inside the transcript pane.
  $("meta").innerHTML = `<span>${dateLabel(s.created_at)}</span>` + (recording ? "" : `<span>${durationLabel(s.duration)}</span>`);
  $("sessionDelete").hidden = recording;
  renderRecordingBar();
  renderTranscript(scrollTop);
  renderSummary();
}

let shownUtterances = { session: null, ids: new Set() };  // The utterances drawn last time. Only newly arrived ones get the entrance animation.
let transcribing = { session: null, busy: false };  // Whether an ended recording was being transcribed at the last draw
function renderTranscript(scrollTop) {
  const pane = $("transcriptPane"), box = $("transcriptRows");
  redrawOwed = false;
  const known = shownUtterances.session === current.id ? shownUtterances.ids : null;
  shownUtterances = { session: current.id, ids: new Set(current.utterances.map((u) => u.id)) };
  const nearBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
  const items = current.utterances.map((u) => ({ t: u.start, u }));
  let run = null;  // Consecutive queued segments are merged into one row, and so are consecutive stopped ones.
  for (const seg of current.segments) {
    if (run && run.status === seg.status && run.end === seg.start) { run.end = seg.end; run.count++; continue; }
    run = null;
    if (!["queued", "working", "error", "stopped"].includes(seg.status)) continue;
    const row = { ...seg, count: 1 };
    if (seg.status === "queued" || seg.status === "stopped") run = row;
    items.push({ t: seg.start + 0.001, seg: row });
  }
  items.sort((a, b) => a.t - b.t);
  box.innerHTML = "";
  if (current.preparing) box.insertAdjacentHTML("beforeend", `<div class="segment"><span class="spinner"></span><p>${current.redoing ? "作り直し" : "文字起こし"}を準備しています</p></div>`);
  if (current.fixing_names || current.names_error) box.appendChild(namesRow());
  let prev = null;
  for (const it of items) {
    if (it.seg) { box.appendChild(segmentRow(it.seg)); prev = null; continue; }
    const u = it.u, el = document.createElement("div");
    el.className = "utterance" + (prev === u.speaker ? " continued" : "") + (known && !known.has(u.id) ? " arrive" : "");
    el.dataset.start = u.start;
    el.innerHTML = `<button class="time" title="ここから再生">${clock(u.start)}</button>`
      + `<button class="speaker ${u.speaker === ME ? "me" : "other"}" title="話者を入れ替え">${u.speaker}</button>`
      + `<div class="text" contenteditable="plaintext-only" spellcheck="false"></div>`;
    const tx = el.querySelector(".text"); tx.textContent = u.text;
    el.querySelector(".time").addEventListener("click", () => seek(u.start));
    el.querySelector(".speaker").addEventListener("click", () => track(editUtterance({ id: u.id, speaker: u.speaker === ME ? OTHER : ME })));
    tx.addEventListener("blur", (e) => {
      const v = tx.textContent.trim();
      if (v !== u.text) track(editUtterance({ id: u.id, text: v }));
      else if (redrawOwed && !e.relatedTarget?.matches("#transcriptRows .text")) renderSession(false);
    });
    tx.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); tx.blur(); } if (e.key === "Escape") { tx.textContent = u.text; tx.blur(); } });
    box.appendChild(el);
    prev = u.speaker;
  }
  // While a finished recording is being transcribed, the place of "redo" holds "stop".
  const recording = current.status === "recording", busy = !recording && (current.pending > 0 || current.preparing);
  $("transcriptRedo").parentElement.hidden = busy; $("transcriptCancel").hidden = !busy;
  setWhy($("transcriptRedo"), recording ? "録音の停止後に作り直せます" : current.fixing_names ? "固有名詞を直している途中です" : current.segments.length ? noModel() : "録音がありません");
  setWhy($("transcriptCopy"), current.utterances.length ? "" : "発話がありません");
  if (!current.utterances.length) showMenu($("transcriptCopy"), false);
  renderVersions(busy);
  const models = current.segments.map((seg) => seg.model).filter(Boolean);
  $("transcriptMade").innerHTML = current.transcribed_at && !current.pending ? madeLabel(current.transcribed_at, models) : "";
  // The transcript of an ended recording has just become complete (not called off, nothing failed).
  const completed = transcribing.session === current.id && transcribing.busy && !busy && !current.segments.some((seg) => seg.status === "error" || seg.status === "stopped");
  transcribing = { session: current.id, busy };
  const rec = live && live.session === current.id;
  if (rec) {
    const last = current.segments.length ? current.segments[current.segments.length - 1].end : 0;
    // The stretch recorded since the last segment waits like a queued one. A recording transcribed afterwards has none: its row is a plain sentence.
    box.insertAdjacentHTML("beforeend", `<div class="segment" id="liveSegment" data-from="${last}">${current.live ? '<span class="spinner idle"></span>' : ""}<p></p></div>`);
    showLiveSegment();
  }
  if (!box.children.length) box.innerHTML = `<p class="empty">発話はありません</p>`;
  if (scrollTop) pane.scrollTop = rec ? pane.scrollHeight : 0;
  else if (nearBottom) pane.scrollTop = completed ? 0 : pane.scrollHeight;  // A pane that followed the arriving rows shows the complete transcript from its start, with the buttons above it.
  markFound();
}

/* Every redo keeps the transcript it replaced as a version: the arrows step between them, once there are two. */
function renderVersions(busy) {
  const n = current.versions, at = current.version;
  $("transcriptVersions").hidden = n < 2;
  if (n < 2) return;
  const held = current.can_move && !busy ? "" : current.fixing_names ? "固有名詞を直している途中です" : "文字起こしの途中です";
  setStep($("versionPrev"), held || (at === 0 ? "これより前の版はありません" : ""));
  setStep($("versionNext"), held || (at === n - 1 ? "これが最新の版です" : ""));
  $("versionAt").textContent = `${at + 1} / ${n} 版`;
}
/* An arrow names itself on hover while it can be pressed; a disabled one tells why instead (only one of the two shows). */
function setStep(button, reason) {
  setWhy(button, reason);
  button.title = reason ? "" : button.getAttribute("aria-label");
}

function showLiveSegment() {
  const row = $("liveSegment");
  if (!row || !live) return;
  // A recording transcribed afterwards has no segment in the making: the row says when its transcript comes.
  row.querySelector("p").textContent = current.live ? `${clock(+row.dataset.from)}–${clock(live.seconds)} ${live.paused ? "一時停止中" : "録音中"}` : "文字起こしは録音の停止後に作ります";
}

function segmentRow(seg) {
  const el = document.createElement("div");
  const range = `${clock(seg.start)}–${clock(seg.end)}`, many = seg.count > 1 ? `（${seg.count} 区間）` : "";
  if (seg.status === "error") {
    el.className = "segment error";
    el.innerHTML = `<p><b>${range} 失敗</b><span>${esc(seg.error)}</span></p>${withWhy(noModel(), `<button>${icon("redo")}再試行</button>`, true)}`;
    el.querySelector("button").addEventListener("click", retry);
  } else if (seg.status === "stopped") {
    el.className = "segment";
    el.innerHTML = `<p>${range} 文字起こしを中止しました${many}</p>${withWhy(noModel(), `<button>${icon("play")}続ける</button>`, true)}`;
    el.querySelector("button").addEventListener("click", retry);
  } else {
    el.className = "segment";
    el.innerHTML = `<span class="spinner${seg.status === "working" ? "" : " idle"}"></span><p>${range} ${seg.status === "working" ? "文字起こし中" : `待機中${many}`}</p>`;
  }
  return el;
}

/* The proper-noun fix applies to the whole session, so its row goes at the top of the utterances. When it was skipped, the row says why and offers to redo just the fix. */
function namesRow() {
  const el = document.createElement("div");
  if (current.fixing_names) {
    el.className = "segment";
    el.innerHTML = `<span class="spinner"></span><p>固有名詞を直しています</p>`;
  } else {
    el.className = "segment error";
    el.innerHTML = `<p><b>固有名詞を直せませんでした</b><span>${esc(current.names_error)}</span></p>${withWhy(current.pending ? "文字起こしの途中です" : noModel(true), `<button>${icon("redo")}再試行</button>`, true)}`;
    el.querySelector("button").addEventListener("click", async () => {
      const s = current;
      s.fixing_names = true; renderTranscript(false);  // Switch to the in-progress row without waiting for the server.
      try { await saving; await api("POST", `/api/sessions/${s.id}/fix-names`); await refreshSession(true); }
      catch (e) { s.fixing_names = false; if (current === s) renderTranscript(false); fail(e); }
    });
  }
  return el;
}

async function editUtterance(edit) {
  const id = current.id;
  try {
    const s = await api("PATCH", `/api/sessions/${id}`, { utterances: [edit] });
    if (!current || current.id !== id) return;  // Another session was opened meanwhile.
    current = s; redrawSession();
  } catch (e) { fail(e); }
}
async function retry() {
  // Failed and stopped segments are queued again alike.
  try { const r = await api("POST", `/api/sessions/${current.id}/retry`); toast(`${r.queued} 区間の文字起こしを続けます`); refreshSession(true); }
  catch (e) { fail(e); }
}

/* The title, the memo and an utterance are saved when the field loses focus, a speaker when it is switched. What works from the saved session (re-transcribing, summarizing, fixing names), and quitting, wait for those saves to finish. */
let saving = Promise.resolve();
function track(save) { saving = Promise.all([saving, save]).then(() => {}, () => {}); }
function saveOnChange(key, el) {
  el.addEventListener("change", () => {
    if (!current) return;
    track(api("PATCH", `/api/sessions/${current.id}`, { [key]: el.value }).then((s) => { if (current && current.id === s.id) { current = s; redrawSession(); } loadList(); }, fail));
  });
}
let redoSent = Promise.resolve();  // The last request for a redo. A stop asked right after it waits for it to reach the server.
$("transcriptRedo").addEventListener("click", async () => {
  if (!await ask({ title: "文字起こしを作り直しますか", body: "いまのメモを前提に、録音の全体を書き起こし直します。いまの文字起こしは編集ごと版として残り、「←」「→」で戻せます。", ok: "作り直す" })) return;
  // Switch to the "preparing" row without waiting for the server.
  const s = current;
  s.preparing = s.redoing = true; renderTranscript(false);
  redoSent = saving.then(() => api("POST", `/api/sessions/${s.id}/retranscribe`));
  try { await redoSent; await refreshSession(true); }
  catch (e) { fail(e); }
  finally { s.preparing = s.redoing = false; if (current === s) renderTranscript(false); }
});
/* Stopping a redo shows again the version that was shown before it; stopping a first transcription leaves the rest as "stopped" rows that can be continued. */
$("transcriptCancel").addEventListener("click", async () => {
  const btn = $("transcriptCancel"), id = current.id;
  btn.disabled = true;
  try {
    await redoSent.catch(() => {});
    const r = await api("POST", `/api/sessions/${id}/cancel`);
    await refreshSession(true);
    if (r.restored) $("transcriptPane").scrollTop = 0;  // Another transcript is on screen: show it from its start.
    toast(r.restored ? "作り直しを中止しました。元の版に戻しました" : "文字起こしを中止しました");
  } catch (e) { fail(e); }
  finally { btn.disabled = false; }
});
/* Moving to another version loses nothing (the one left keeps its edits), so it asks no confirmation; the count between the arrows tells where it went. */
for (const [id, step] of [["versionPrev", -1], ["versionNext", 1]]) $(id).addEventListener("click", async () => {
  const sid = current.id, index = current.version + step;
  try {
    await saving;
    await api("POST", `/api/sessions/${sid}/version`, { index });
    await refreshSession(true);
    if (current?.id === sid) $("transcriptPane").scrollTop = 0;  // Another transcript is on screen: show it from its start.
  } catch (e) { fail(e); }
});
saveOnChange("title", $("title"));
saveOnChange("memo", $("memo"));
$("title").addEventListener("change", () => ($("title").title = $("title").value));
$("title").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });

/* ---------- Tabs ---------- */
function selectTab(name) {
  for (const x of document.querySelectorAll(".tab")) { x.classList.toggle("on", x.dataset.tab === name); x.setAttribute("aria-selected", x.dataset.tab === name); }
  for (const pane of ["transcript", "summary"]) $(`${pane}Pane`).hidden = name !== pane;
  moveTabLine(true);
}
for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => selectTab(b.dataset.tab));
/* Put the line under the selected tab. If slide is false it is placed without sliding (right after a view appears, and when the window is resized). */
function moveTabLine(slide) {
  const tabs = document.querySelector(".tabs"), on = tabs.querySelector(".tab.on");
  if (!on.offsetWidth) return;
  tabs.classList.toggle("still", !slide);
  tabs.style.setProperty("--tab-x", `${on.offsetLeft}px`);
  tabs.style.setProperty("--tab-w", `${on.offsetWidth}px`);
  if (!slide) { void tabs.offsetWidth; tabs.classList.remove("still"); }
}
addEventListener("resize", () => moveTabLine(false));

/* ---------- Summary ---------- */
let summaryWas = { session: null, summarizing: false };  // Whether it was being made at the last draw. A finished summary gets the entrance animation.
function renderSummary() {
  const box = $("summary"), s = current, summary = s.summary;
  const recording = s.status === "recording";
  const arrived = summaryWas.session === s.id && summaryWas.summarizing && !s.summarizing;
  summaryWas = { session: s.id, summarizing: !!s.summarizing };
  if (s.summarizing) {
    box.innerHTML = `<div class="actions"><span class="spinner"></span><span>要約を作成中</span></div>`;
    return;
  }
  const err = summary && summary.error ? `<p class="error"><b>失敗</b>${esc(summary.error)}</p>` : "";
  if (!summary || !summary.text) {
    const midway = s.pending || s.preparing ? "文字起こしの途中です" : "";
    const why = recording ? "録音の停止後に作成できます" : s.utterances.length ? noModel() : midway || "発話がありません";
    // A summary can be made of the part transcribed so far: the button stays enabled, and the state is told beside it.
    box.innerHTML = `<div class="actions">${withWhy(why, `<button class="primary" id="summaryMake">要約を作成</button>`)}
      <span class="sub">${why ? "" : midway}</span></div>${err}`;
  } else {
    box.innerHTML = `<div class="actions">${withWhy(recording ? "録音の停止後に作り直せます" : noModel(), `<button class="redo" id="summaryMake">${icon("redo")}作り直す</button>`)}<button id="summaryCopy">${icon("copy")}Markdown でコピー</button>
      <span class="sub made">${summary.created_at ? madeLabel(summary.created_at, summary.model ? [summary.model] : []) : ""}</span></div>${err}<div class="markdown${arrived ? " arrive" : ""}">${markdown(summary.text)}</div>`;
    $("summaryCopy").addEventListener("click", () => navigator.clipboard.writeText(summary.text).then(() => { confirmCopied($("summaryCopy")); toast("要約を Markdown でコピーしました"); }, () => toast("コピーできませんでした。ほかのアプリがクリップボードを使っています", true)));
  }
  const btn = $("summaryMake");
  if (btn) btn.addEventListener("click", async () => {
    if (summary && summary.text && !await ask({ title: "要約を作り直しますか", body: "いまのメモを前提に、要約を作り直します。いまの要約は置き換わります。", ok: "作り直す" })) return;
    current.summarizing = true; renderSummary();  // Switch to the in-progress view without waiting for the server.
    try { await saving; await api("POST", `/api/sessions/${s.id}/summarize`); await refreshSession(true); }
    catch (e) { if (current && current.id === s.id) { current.summarizing = false; renderSummary(); } fail(e); }
  });
}
/* The minimal Markdown a summary needs (headings, lists, bold, paragraphs). */
function markdown(src) {
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  const out = [], indents = []; let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; } };
  const closeLists = (indent) => { while (indents.length && indents[indents.length - 1] > indent) { indents.pop(); out.push("</ul>"); } };
  for (const line of src.split(/\r?\n/)) {
    const h = line.match(/^(#{1,4})\s+(.*)$/), li = line.match(/^(\s*)[-*]\s+(.*)$/);
    if (h) { flushPara(); closeLists(-1); const n = Math.min(4, Math.max(2, h[1].length)); out.push(`<h${n}>${inline(h[2])}</h${n}>`); }
    else if (li) {
      flushPara();
      const indent = li[1].replace(/\t/g, "    ").length;  // Whatever the indent width, a deeper item nests one level.
      closeLists(indent);
      if (!indents.length || indents[indents.length - 1] < indent) { indents.push(indent); out.push("<ul>"); }
      out.push(`<li>${inline(li[2])}</li>`);
    }
    else if (!line.trim()) { flushPara(); }
    else { closeLists(-1); para.push(line.trim()); }
  }
  flushPara(); closeLists(-1);
  return out.join("");
}

/* ---------- Playback (the stereo me/other tracks are mixed to mono, so both are heard in both ears) ---------- */
let audioCtx = null;
function ensureMono() {
  if (audioCtx) return;
  try {
    audioCtx = new AudioContext();
    const src = audioCtx.createMediaElementSource($("audio"));
    const mono = audioCtx.createGain();
    mono.channelCount = 1; mono.channelCountMode = "explicit"; mono.channelInterpretation = "speakers";
    mono.gain.value = 1.4;
    src.connect(mono).connect(audioCtx.destination);
  } catch (e) { audioCtx = null; }
}
$("audio").addEventListener("play", () => { ensureMono(); if (audioCtx && audioCtx.state === "suspended") audioCtx.resume(); });
/* Start playing. A failure is told in the app's own words (the browser gives its reason in English). Being interrupted by a pause is not a failure. */
function play() {
  $("audio").play().catch((e) => { if (e.name !== "AbortError") toast("再生できませんでした。録音のファイルが見つからないか、読めません", true); });
}
/* Stop playing and let go of the recording. Leaving a session must not leave its sound playing with nothing on screen to stop it. */
function releaseAudio() {
  const audio = $("audio");
  audio.pause(); audio.removeAttribute("src"); audio.load();
  showPlaying();  // Unloading drops the pause event.
}
function seek(t) {
  if ($("player").hidden) return;
  $("audio").currentTime = Math.max(0, t - 0.5); play();
}
function showPosition(t) {
  const bar = $("playerSeek");
  bar.value = t; bar.style.setProperty("--p", `${Math.min(100, t / bar.max * 100)}%`);
  $("playerNow").textContent = clock(t);
}
$("playerPlay").addEventListener("click", () => { if ($("audio").paused) play(); else $("audio").pause(); });
function showPlaying() {
  const playing = !$("audio").paused;
  $("player").classList.toggle("playing", playing);
  $("playerPlay").setAttribute("aria-label", playing ? "一時停止" : "再生");
  $("playerPlay").title = playing ? "一時停止（Space）" : "再生（Space）";
}
for (const ev of ["play", "pause"]) $("audio").addEventListener(ev, showPlaying);
$("playerSeek").addEventListener("input", () => { $("audio").currentTime = +$("playerSeek").value; showPosition(+$("playerSeek").value); });
const calm = matchMedia("(prefers-reduced-motion: reduce)");  // The PC asks for less motion.
$("audio").addEventListener("timeupdate", () => {
  const t = $("audio").currentTime;
  showPosition(t);
  let hit = null;
  for (const el of document.querySelectorAll("#transcriptRows .utterance")) { if (+el.dataset.start <= t + 0.3) hit = el; else break; }
  for (const el of document.querySelectorAll("#transcriptRows .utterance.now")) if (el !== hit) el.classList.remove("now");
  if (hit && !hit.classList.contains("now")) { hit.classList.add("now"); if (!$("audio").paused) hit.scrollIntoView({ block: "nearest", behavior: calm.matches ? "auto" : "smooth" }); }
});

/* ---------- Actions on a session ---------- */
/* The transcript is copied in one of two forms, chosen from a menu that opens under the button. */
const COPY_FORMS = {
  labelled: { line: (u) => `[${clock(u.start)}] ${u.speaker}: ${u.text}`, done: "文字起こしを、時刻と話者を付けてコピーしました" },
  plain: { line: (u) => u.text, done: "文字起こしの本文だけをコピーしました" },
};
bindMenu($("transcriptCopy"));
for (const item of $("copyMenu").children) item.addEventListener("click", async () => {
  const form = COPY_FORMS[item.dataset.form];
  $("transcriptCopy").focus(); showMenu($("transcriptCopy"), false);
  try { await navigator.clipboard.writeText(current.utterances.map(form.line).join("\n")); confirmCopied($("transcriptCopy")); toast(form.done); } catch (e) { toast("コピーできませんでした。ほかのアプリがクリップボードを使っています", true); }
});
$("sessionReveal").addEventListener("click", () => api("POST", `/api/sessions/${current.id}/reveal`).catch(fail));
$("sessionDelete").addEventListener("click", async () => {
  if (!await ask({ title: "この記録を削除しますか", body: `「${current.title || UNTITLED}」の録音・文字起こし・要約をすべて削除します。元に戻せません。`, ok: "削除する" })) return;
  const id = current.id;
  releaseAudio();  // Let go of the recording opened for playback before deleting.
  // If another view was opened while the deletion ran, it is left as it is.
  const here = () => current && current.id === id;
  try { await api("DELETE", `/api/sessions/${id}`); toast("削除しました"); await loadList(); if (here()) showEmpty(); }
  catch (e) { fail(e); await loadList(); if (!here()) return; if (sessions.some((s) => s.id === id)) openSession(id); else showEmpty(); }
});

/* ---------- Settings ---------- */
function openSettings() {
  $("segmentSeconds").value = config.settings.segment_seconds;
  // The rows are laid out at once and the counts filled in when they arrive, so the dialog does not grow after it has appeared.
  const usageRows = (count) => config.models.map((m) => `<span>${esc(m.name)}</span>${count(m)}`).join("");
  $("usage").innerHTML = usageRows(() => "<span></span>");
  // The limit is known only once Google has reported it; until then the count stands alone.
  api("GET", "/api/usage").then(({ used, limits, blocked }) => {
    $("usage").innerHTML = usageRows((m) => {
      if (blocked[m.id]) return `<span class="full">上限に到達（${backLabel(blocked[m.id])} に回復）</span>`;
      const count = `${used[m.id] || 0} 回`, max = limits[m.id];
      return `<span>${max ? `${count} ／ 上限 ${max} 回` : count}</span>`;
    });
  }).catch(fail);
  $("sessionsDir").innerHTML = `<bdi>${esc(config.sessions_dir)}</bdi>`; $("sessionsDir").title = config.sessions_dir;
  $("appVersion").textContent = `Verba ${config.version}`;
  $("apiKey").value = ""; showKeySave();
  $("settingsModal").hidden = false;
  $("apiKey").focus();
}
$("settingsOpen").addEventListener("click", openSettings);
/* The key is replaced only by a new one: the button waits for it, and tells so. While a key is being checked, the button rests without a reason. */
let keySending = false;
function showKeySave() {
  if (keySending) $("apiKeySave").disabled = true;
  else setWhy($("apiKeySave"), $("apiKey").value.trim() ? "" : "新しいキーを入れると押せます");
}
$("apiKeySave").addEventListener("click", async () => {
  keySending = true; showKeySave();
  try {
    const r = await api("POST", "/api/key", { key: $("apiKey").value });
    $("apiKey").value = "";
    toast(keySavedMessage(r));
  } catch (e) { fail(e); $("apiKey").focus(); }  // The field, so that the key can be mended and sent with Enter
  keySending = false; showKeySave();
});
$("apiKey").addEventListener("input", showKeySave);
$("apiKey").addEventListener("keydown", (e) => { if (e.key === "Enter" && !$("apiKeySave").disabled) $("apiKeySave").click(); });
$("segmentSeconds").addEventListener("change", async () => {
  const el = $("segmentSeconds");
  if (el.value) await saveSettings({ segment_seconds: +el.value });
  el.value = config.settings.segment_seconds;  // The server keeps the value within its range: show what was saved.
});
const closeSettings = () => { $("settingsModal").hidden = true; $("settingsOpen").focus(); };
$("settingsClose").addEventListener("click", closeSettings);
$("settingsModal").addEventListener("click", (e) => { if (e.target.id === "settingsModal") closeSettings(); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("askModal").hidden) $("askCancel").click();
  else if (!$("settingsModal").hidden) closeSettings();
  else if (menuOpen) { const button = menuOpen; showMenu(button, false); button.focus(); }
});

/* ---------- Keyboard ---------- */
/* Ctrl+F: search, Ctrl+N: new recording. Space and the arrows control playback; while a text field or a control has focus, the keys keep their own meaning there. */
const SKIP = 5;  // Seconds the arrow keys skip
document.addEventListener("keydown", (e) => {
  if (!$("askModal").hidden || !$("settingsModal").hidden || $("shell").hidden || e.altKey) return;
  const key = e.key.toLowerCase();
  if (e.ctrlKey && key === "f") { e.preventDefault(); $("search").focus(); $("search").select(); return; }
  // Leaving the field first saves an edit in progress while its session is still the one on screen.
  if (e.ctrlKey && key === "n") { e.preventDefault(); document.activeElement?.blur(); $("recordingNew").click(); return; }
  if (e.ctrlKey || e.metaKey || $("sessionView").hidden || $("player").hidden) return;
  if (e.target.closest("input, textarea, select, button, a, [contenteditable]")) return;
  const audio = $("audio");
  if (e.key === " ") { e.preventDefault(); $("playerPlay").click(); }
  else if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
    e.preventDefault();
    audio.currentTime = Math.min(current.duration, Math.max(0, audio.currentTime + (e.key === "ArrowLeft" ? -SKIP : SKIP)));
    showPosition(audio.currentTime);
  }
});

/* ---------- First run: registering the API key ---------- */
const keySavedMessage = (r) => (r.checked ? "API キーを保存しました" : "API キーを保存しました（通信できず、使えるかは未確認です）");
$("keyForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("firstKeySave"), err = $("firstKeyError");
  btn.disabled = true; err.hidden = true;
  try {
    const r = await api("POST", "/api/key", { key: $("firstKey").value });
    $("firstKey").value = "";
    $("keyView").hidden = true;
    if (!r.checked) toast(keySavedMessage(r));
    await startApp("new");
  } catch (e) { err.textContent = e.message; err.hidden = false; $("firstKey").focus(); }
  finally { btn.disabled = false; }
});

/* ---------- Closing the window (called from app.py) ---------- */
/* Leaving the field saves an edit in progress; the app is told to quit only after the saves have reached it. */
window.saveAndQuit = async () => {
  document.activeElement?.blur();
  await saving;
  api("POST", "/api/quit").catch(fail);
};
/* While recording, closing asks first. A question already on screen is answered before a second one is asked. */
window.confirmQuit = async () => {
  if (!$("askModal").hidden) return;
  const ok = await ask({ title: "録音を停止して終了しますか", body: "ここまでの録音と文字起こしは保存されます。", ok: "停止して終了", cancel: "録音を続ける" });
  if (ok) saveAndQuit();
};

/* ---------- Polling ---------- */
async function pollState() {
  try {
    const st = await api("GET", "/api/state");
    const was = live && `${live.session}${live.paused}`;
    live = st.recording;
    if (live) {
      Object.assign(live.muted, muteAsked);
      // On first sight of a recording (the app may have been reopened in its middle), the other side counts as listened to all along. Muted time is not listening.
      if (heard.session !== live.session) heard = { session: live.session, peak: 0, seconds: live.seconds, at: live.seconds };
      else if (!live.muted.other) heard.seconds += live.seconds - heard.at;
      heard.at = live.seconds;
      heard.peak = Math.max(heard.peak, live.levels.other);
    }
    showModel(st.model);
    if ((live && `${live.session}${live.paused}`) !== was) { renderList(); if (current) renderTranscript(false); }
    if (current) renderRecordingBar();
  } catch (e) { /* The server is down. */ }
}
let tick = 0;
setInterval(async () => {
  await pollState();
  tick++;
  if (current && tick % 3 === 0 && (live?.session === current.id || current.pending || current.preparing || current.summarizing || current.fixing_names || current.status === "recording")) refreshSession(false);
  if (tick % 6 === 0 && !$("newView").hidden) refreshDevices();
  if (tick % 20 === 0) loadList().catch(() => {});
}, 500);

/* ---------- Startup ---------- */
(async function init() {
  let hasKey;
  try {
    config = await api("GET", "/api/config");
    devices = await api("GET", "/api/devices");
    hasKey = (await api("GET", "/api/state")).has_key;
  } catch (e) { document.body.innerHTML = `<p class="fatal">${esc(e.message)}</p>`; return; }
  // A session id after "#" in the address opens that session at startup, and "new" opens the new-recording view.
  if (hasKey) return startApp(decodeURIComponent(location.hash.slice(1)));
  $("keyView").hidden = false;
  $("firstKey").focus();
})();

async function startApp(linked) {
  $("shell").hidden = false;
  await pollState();
  await loadList();
  // Sessions written by a later Verba are left out of the list; this says why they are missing.
  if (config.newer) toast(`${config.newer} 件の記録を開けませんでした。新しい版の Verba で作られています`, true);
  if (live) openSession(live.session);
  else if (sessions.some((s) => s.id === linked)) openSession(linked);
  else if (sessions.length && linked !== "new") showEmpty();
  else showNew();
}
