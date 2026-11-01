"use strict";

const $ = (s) => document.querySelector(s);
const IMG = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg", ".ico", ".jfif"]);
const VID = new Set([".mp4", ".webm", ".mov", ".m4v", ".mkv", ".ogv"]);
const AUD = new Set([".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".weba"]);
const BIN_DELAY = 4000;

const S = {
  result: null,
  cache: new Map(),
  mode: "children",
  sel: 0,
  showKept: false,
  pending: new Map(),          // key -> {item, timer}
  kept: new Set(JSON.parse(localStorage.getItem("declutter.kept") || "[]")),
  undo: [],
  freed: { pc: 0, phone: 0 },
  polling: null,
  previewToken: 0,
};

const isPhonePath = (p) => p.toLowerCase().startsWith("mtp:");
const onPhone = () => !!(S.result && isPhonePath(S.result.root));

const key = (p) => p.toLowerCase().replace(/[\\/]+$/, "");
const under = (child, parent) => key(child).startsWith(key(parent) + "\\");
const fileUrl = (p) => "/api/file?path=" + encodeURIComponent(p);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmt(n) {
  if (!n || n < 0) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n < 10 ? n.toFixed(2) : n < 100 ? n.toFixed(1) : n.toFixed(0)) + " " + u[i];
}
const fmtDate = (t) => t ? new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) : "—";
const ext = (it) => it.isDir ? "" : it.ext || "";

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Declutter": "1" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg, { err = false, ms = 3200, timer = 0 } = {}) {
  const el = document.createElement("div");
  el.className = "toast" + (err ? " err" : "");
  el.innerHTML = `<span>${msg}</span>` + (timer ? `<i class="timer" style="animation-duration:${timer}ms"></i>` : "");
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
  return el;
}

/* ---------------- header ---------------- */

function renderDrives(drives, phones) {
  S.drives = drives;
  if (phones) S.phones = phones;
  const phoneStores = (S.phones || []).flatMap((p) => p.storages.map((s) => ({ ...s, label: `${p.name} · ${s.name}`, phone: true })));
  $("#drives").innerHTML = [...drives.map((d) => ({ ...d, label: d.path })), ...phoneStores].map((d) => {
    const pct = d.total ? (d.used / d.total) * 100 : 0;
    return `<div class="drive ${d.free / d.total < 0.1 ? "tight" : ""} ${d.phone ? "phone" : ""}">
      <div class="row"><b>${esc(d.label)}</b><span>${fmt(d.free)} free</span></div>
      <div class="gauge"><i style="width:${pct.toFixed(1)}%"></i></div>
      <div class="row dim"><span>${pct.toFixed(0)}% used</span><span>${fmt(d.total)}</span></div>
    </div>`;
  }).join("");
}

async function loadPhones() {
  const box = $("#phoneSection");
  if (!S.phones) {
    box.classList.remove("hidden");
    box.innerHTML = `<p>Looking for connected phones…</p>`;
  }
  const res = await api("/api/phones").catch(() => ({ phones: [] }));
  renderDrives(S.drives || [], res.phones);
  renderPhones(res.phones);
}

function renderPhones(phones) {
  const box = $("#phoneSection");
  box.classList.toggle("hidden", !phones.length);
  let n = 0;
  const btn = (label, sub, path) =>
    `<button class="spot phone" style="animation-delay:${n++ * 35}ms" data-path="${esc(path)}">${esc(label)}<small>${sub}</small></button>`;
  box.innerHTML = phones.map((p) => `
    <h2>${esc(p.name)} <span class="dim">/ phone</span></h2>
    <p>Phone storage is read over USB, so weighing is slower — sizes fill in live and you can start judging straight away.<br><b>Deleting from a phone is permanent: there's no Recycle Bin.</b></p>
    ${p.storages.map((s) => `
      <div class="spots-label">${esc(s.name)} <span class="dim">· ${fmt(s.free)} free of ${fmt(s.total)}</span></div>
      <div class="spots">
        ${s.spots.map((sp) => btn(sp.label, esc(s.name), sp.path)).join("")}
        ${btn("Everything", "whole " + esc(s.name.toLowerCase()) + " (slow)", s.path)}
      </div>`).join("")}`).join("");
}

function renderBin(bin) {
  $("#binSize").textContent = `${fmt(bin.size)} · ${bin.items} items`;
  $("#binBox").classList.toggle("full", bin.size > 1024 ** 3);
}

async function loadInfo() {
  const info = await api("/api/info");
  S.drives = info.drives;
  renderDrives(info.drives);
  renderBin(info.bin);
  loadPhones();
  $("#pathInput").placeholder = info.home + "\\Downloads";
  $("#spots").innerHTML = info.spots.map((s, i) =>
    `<button class="spot" style="animation-delay:${i * 35}ms" data-path="${esc(s.path)}">${esc(s.label)}<small>${esc(s.path)}</small></button>`).join("");
  $("#driveSpots").innerHTML = info.drives.map((d, i) =>
    `<button class="spot" style="animation-delay:${(info.spots.length + i) * 35}ms" data-path="${esc(d.path)}">${esc(d.path)}<small>${fmt(d.used)} used</small></button>`).join("");
}

/* ---------------- scanning ---------------- */

function show(view) {
  for (const v of ["start", "scanning", "work"]) $("#" + v).classList.toggle("hidden", v !== view);
}

async function scan(path, force = false) {
  const cached = S.cache.get(key(path));
  if (cached && !force) return openResult(cached);
  try {
    await api("/api/scan", { path });
  } catch (e) {
    return toast(esc(e.message), { err: true });
  }
  pauseMedia();
  show("scanning");
  $("#scanRoot").textContent = path;
  $("#scanBytes").textContent = "0 B";
  $("#scanFiles").textContent = isPhonePath(path) ? "reading the phone…" : "0 files";
  $("#scanCurrent").textContent = "";
  clearInterval(S.polling);
  let live = false;
  const tick = async () => {
    const st = await api("/api/scan/status").catch(() => null);
    if (!st || st.idle) return;
    $("#scanBytes").textContent = fmt(st.bytes);
    $("#scanFiles").textContent = st.files.toLocaleString() + " files · " + st.elapsed.toFixed(1) + "s";
    $("#scanCurrent").textContent = st.current;
    S.scanStatus = st;
    if (st.result && !st.error) {
      if (!live) { live = true; openResult(st.result); } else mergeResult(st.result);
    }
    if (!st.done) return;
    clearInterval(S.polling);
    S.polling = null;
    S.scanStatus = null;
    if (st.error) {
      if (!/cancelled/i.test(st.error)) toast(esc(st.error), { err: true });
      if (!live) show(S.result ? "work" : "start");
      else renderLedger();
      return;
    }
    S.cache.set(key(st.result.root), st.result);
  };
  S.polling = setInterval(tick, isPhonePath(path) ? 700 : 250);
}

function mergeResult(r) {
  const cur = current();
  S.result = r;
  const list = visible();
  const idx = cur ? list.findIndex((x) => x.path === cur.path) : -1;
  S.sel = idx >= 0 && S.touched ? idx : nextUndecided(list, 0);
  renderLedger();
  const now = current();
  if (!cur || !now || now.path !== cur.path) return renderCard();
  const sizeEl = $("#deck .card:not(.flying) .size");
  if (sizeEl) sizeEl.innerHTML = cardSizeHtml(now);
  const factsEl = $("#deck .card:not(.flying) .facts");
  if (factsEl) factsEl.innerHTML = cardFacts(now).map((f) => `<span>${esc(f)}</span>`).join("");
}

function cancelScan() {
  if (!S.polling) return;
  api("/api/scan/cancel", {});
}

function openResult(r) {
  S.result = r;
  S.sel = 0;
  S.touched = false;
  show("work");
  S.sel = nextUndecided(visible(), 0);
  render();
}

/* ---------------- list model ---------------- */

const isKept = (it) => S.kept.has(key(it.path));
const isPending = (it) => S.pending.has(key(it.path));

function visible() {
  if (!S.result) return [];
  return S.result[S.mode].filter((it) => S.showKept || !isKept(it));
}

function nextUndecided(list, start) {
  const ok = (it) => !isPending(it) && !isKept(it) && (it.size > 0 || it.pending);
  for (let i = start; i < list.length; i++) if (ok(list[i])) return i;
  for (let i = 0; i < Math.min(start, list.length); i++) if (ok(list[i])) return i;
  return list.length;
}

function current() {
  return visible()[S.sel] || null;
}

function persistKept() {
  localStorage.setItem("declutter.kept", JSON.stringify([...S.kept]));
}

/* ---------------- render ---------------- */

function render() {
  renderCrumbs();
  renderLedger();
  renderCard();
}

function renderCrumbs() {
  const root = S.result.root;
  const parts = root.split("\\").filter(Boolean);
  let acc = "";
  const out = parts.map((p, i) => {
    acc = i === 0 ? p + "\\" : acc.replace(/\\$/, "") + "\\" + p;
    const label = i === 0 && isPhonePath(p) ? p.slice(4) + " (phone)" : p;
    return `<button data-path="${esc(acc)}">${esc(label)}</button>`;
  });
  $("#crumbs").innerHTML = out.join('<span class="sep">\\</span>');
}

function renderLedger() {
  const r = S.result;
  const list = visible();
  $("#total").textContent = fmt(r.total);
  const where = onPhone() ? `on the phone in ${r.root.slice(4)}` : `on disk in ${r.root}`;
  const weighing = r.partial ? ` · still weighing ${S.scanStatus ? S.scanStatus.files.toLocaleString() + " files so far" : ""}` : "";
  $("#totalSub").textContent = where + weighing + (r.cloud ? ` · +${fmt(r.cloud)} online-only` : "") + (r.errors ? ` · ${r.errors} unreadable` : "");
  $("#btnBin").innerHTML = onPhone() ? "<kbd>&larr;</kbd> Delete" : "<kbd>&larr;</kbd> Bin";
  $("#freed").textContent = fmt(S.freed[onPhone() ? "phone" : "pc"]);
  $(".freed .label").textContent = onPhone() ? "Freed on phone" : "Freed this session";
  document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("on", b.dataset.mode === S.mode));
  const left = list.filter((it) => !isPending(it) && !isKept(it) && (it.size > 0 || it.pending)).length;
  $("#remaining").textContent = `${left} to judge`;
  const max = Math.max(1, ...list.map((i) => i.size));
  if (!list.length) {
    $("#rows").innerHTML = `<li class="empty-list">Nothing here.</li>`;
    return;
  }
  $("#rows").innerHTML = list.map((it, i) => {
    const cls = ["r", i === S.sel && "sel", isKept(it) && "kept", isPending(it) && "pending binned", it.protected && "locked", it.pending && "weighing"].filter(Boolean).join(" ");
    const rel = S.mode === "topFiles" ? `<div class="p">${esc(it.path.slice(r.root.length).replace(/^\\/, ""))}</div>` : "";
    return `<li class="${cls}" data-i="${i}">
      <span class="n">${i + 1}</span>
      <span class="nm">${it.isDir ? '<span class="dir">[DIR]</span>' : ""}${esc(it.name)}</span>
      <span class="sz">${it.cloud && !it.size ? "cloud" : fmt(it.size)}</span>
      <span class="bar"><i style="width:${((it.size / max) * 100).toFixed(1)}%"></i></span>
      ${rel}
    </li>`;
  }).join("");
  const sel = $("#rows .sel");
  if (sel) sel.scrollIntoView({ block: "nearest" });
}

function pauseMedia(el = $("#deck")) {
  el.querySelectorAll("video, audio").forEach((m) => { m.pause(); m.removeAttribute("src"); m.load(); });
}

function renderCard() {
  const deck = $("#deck");
  deck.querySelectorAll(".card:not(.flying), .done-card").forEach((c) => { pauseMedia(c); c.remove(); });
  const it = current();
  if (!it) {
    const d = document.createElement("div");
    d.className = "done-card";
    d.innerHTML = `<h2>Cleared</h2><p>Nothing left to judge here.</p><p><kbd>Backspace</kbd> go up a level &nbsp; <kbd>Tab</kbd> switch list &nbsp; <kbd>R</kbd> rescan</p>`;
    deck.appendChild(d);
    return;
  }
  const facts = cardFacts(it);
  const warns = [];
  if (onPhone() && !it.protected) warns.push(`<div class="warn perm">On the phone — deleting is permanent, there's no Recycle Bin for phones.</div>`);
  if (it.protected) warns.push(`<div class="warn lock">Locked — ${esc(it.protected)}</div>`);
  else if (it.caution) warns.push(`<div class="warn caution">${esc(it.caution)}</div>`);
  if (it.cloud) warns.push(`<div class="warn cloud">Online-only OneDrive file — not using space on this PC.</div>`);
  else if (it.cloudBytes) warns.push(`<div class="warn cloud">Also contains ${fmt(it.cloudBytes)} of online-only files (not counted).</div>`);

  const card = document.createElement("article");
  card.className = "card";
  card.innerHTML = `
    <span class="stamp bin">BIN</span><span class="stamp keep">KEEP</span>
    <div class="preview"></div>
    <div class="meta">
      <div>
        <div class="name">${esc(it.name)}</div>
        <div class="path">${esc(it.path)}</div>
        <div class="facts">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</div>
      </div>
      <div class="size">${cardSizeHtml(it)}</div>
      ${warns.join("")}
    </div>`;
  deck.appendChild(card);
  fillPreview(card.querySelector(".preview"), it);
}

function cardFacts(it) {
  return [
    it.isDir ? "Folder" : (ext(it).replace(".", "").toUpperCase() || "File"),
    it.isDir ? `${(it.files || 0).toLocaleString()} files${it.pending ? " so far" : ""}` : null,
    it.mtime ? `modified ${fmtDate(it.mtime)}` : null,
    isKept(it) ? "kept earlier" : null,
  ].filter(Boolean);
}

function cardSizeHtml(it) {
  const total = S.result.total;
  const pct = total ? (it.size / total) * 100 : 0;
  const size = it.cloud && !it.size ? "0 B" : fmt(it.size) + (it.pending ? "+" : "");
  const note = it.pending ? "still weighing…" : pct >= 0.1 ? pct.toFixed(1) + "% of this folder" : "&lt;0.1% of this folder";
  return `${size}<small>${note}</small>`;
}

function bigExt(label, note) {
  return `<div class="bigext">${esc(label)}<small>${note}</small></div>`;
}

async function fillPreview(box, it) {
  const token = ++S.previewToken;
  const e = ext(it);
  if (it.cloud) { box.innerHTML = bigExt("cloud", "Preview skipped so it doesn't download"); return; }
  if (!it.isDir && IMG.has(e)) {
    box.innerHTML = `<img alt="" src="${fileUrl(it.path)}">`;
    box.querySelector("img").onerror = () => { box.innerHTML = bigExt(e.slice(1), "Can't render this image"); };
    return;
  }
  if (!it.isDir && VID.has(e)) {
    box.innerHTML = `<video src="${fileUrl(it.path)}" autoplay muted loop controls playsinline preload="metadata"></video>`;
    box.querySelector("video").onerror = () => { box.innerHTML = bigExt(e.slice(1), "Browser can't play this codec — press <kbd>O</kbd> to open it"); };
    return;
  }
  if (!it.isDir && AUD.has(e)) {
    box.innerHTML = `<div class="audio-wrap">${bigExt(e.slice(1), "<kbd>Space</kbd> to play")}<audio src="${fileUrl(it.path)}" controls preload="metadata"></audio></div>`;
    return;
  }
  if (!it.isDir && e === ".pdf") {
    box.innerHTML = `<iframe src="${fileUrl(it.path)}#toolbar=0&view=FitH"></iframe>`;
    return;
  }
  box.innerHTML = bigExt(it.isDir ? "…" : (e.slice(1) || "file"), "looking inside");
  const p = await api("/api/peek?path=" + encodeURIComponent(it.path)).catch((x) => ({ kind: "error", error: x.message }));
  if (token !== S.previewToken) return;
  if (p.kind === "dir" || p.kind === "zip") {
    if (p.error) { box.innerHTML = bigExt(p.kind === "dir" ? "locked" : e.slice(1), esc(p.error)); return; }
    if (!p.items.length) { box.innerHTML = bigExt("empty", "Nothing inside"); return; }
    const imgs = (p.images || []).map((src) => `<img alt="" loading="lazy" src="${fileUrl(src)}">`).join("");
    const rows = p.items.map((c) => `<li><span class="${c.isDir ? "d" : ""}">${c.isDir ? "[DIR] " : ""}${esc(c.name)}</span><span class="s">${c.isDir ? "" : fmt(c.size)}</span></li>`).join("");
    const more = (p.count > p.items.length ? `<li class="more">+ ${p.count - p.items.length} more</li>` : "")
      + (p.kind === "dir" ? `<li class="more">Press <kbd>Enter</kbd> to go inside and see what each of these weighs</li>` : "");
    box.innerHTML = `<div class="peek ${imgs ? "has-img" : ""}">${imgs ? `<div class="thumbs">${imgs}</div>` : ""}<ul>${rows}${more}</ul></div>`;
    box.querySelectorAll(".thumbs img").forEach((im) => (im.onerror = () => im.remove()));
    return;
  }
  if (p.kind === "text") { box.innerHTML = `<pre></pre>`; box.querySelector("pre").textContent = p.text; return; }
  box.innerHTML = bigExt(e.slice(1) || "file", p.error ? esc(p.error) : "No preview for this type — <kbd>O</kbd> opens it, <kbd>E</kbd> shows it in Explorer");
}

function flyOut(dir) {
  const card = $("#deck .card:not(.flying)");
  if (!card) return;
  card.classList.add("flying", dir === "left" ? "fly-left" : "fly-right");
  pauseMedia(card);
  setTimeout(() => card.remove(), 400);
}

function flash(btn) {
  btn.classList.add("hit");
  setTimeout(() => btn.classList.remove("hit"), 140);
}

/* ---------------- actions ---------------- */

function advanceAfter(it, oldIdx) {
  const list = visible();
  const idx = list.findIndex((x) => x.path === it.path);
  S.sel = nextUndecided(list, idx >= 0 ? idx + 1 : oldIdx);
}

function binCurrent() {
  flash($("#btnBin"));
  const it = current();
  if (!it) return;
  if (it.protected || isPending(it) || it.pending) {
    const c = $("#deck .card:not(.flying)");
    if (c) { c.classList.remove("shake"); void c.offsetWidth; c.classList.add("shake"); }
    if (it.protected) toast(esc(it.protected), { err: true });
    else if (it.pending) toast("Still weighing this folder — give it a moment, or judge something else first");
    return;
  }
  const k = key(it.path);
  const wasKept = S.kept.delete(k);
  if (wasKept) persistKept();
  const verb = isPhonePath(it.path) ? "Deleting from phone (permanent)" : "Binning";
  const t = toast(`${verb}: <b>${esc(it.name)}</b> (${fmt(it.size)}) — <kbd>Z</kbd> to undo`, { ms: BIN_DELAY, timer: BIN_DELAY });
  const timer = setTimeout(() => commitBin(k), BIN_DELAY);
  S.pending.set(k, { item: it, timer, toast: t, wasKept });
  S.undo.push({ type: "bin", k, item: it });
  flyOut("left");
  advanceAfter(it, S.sel);
  render();
}

async function commitBin(k) {
  const p = S.pending.get(k);
  if (!p) return;
  try {
    const res = await api("/api/trash", { path: p.item.path });
    S.pending.delete(k);
    S.freed[isPhonePath(p.item.path) ? "phone" : "pc"] += p.item.size;
    renderBin(res.bin);
    renderDrives(res.drives, res.phones);
    const cur = current();
    removeFromCaches(p.item);
    const list = visible();
    S.sel = cur ? Math.max(0, list.findIndex((x) => x.path === cur.path)) : list.length;
    if (cur && list.findIndex((x) => x.path === cur.path) < 0) S.sel = nextUndecided(list, 0);
    renderLedger();
    renderCrumbs();
  } catch (e) {
    S.pending.delete(k);
    toast(`Couldn't ${isPhonePath(p.item.path) ? "delete" : "bin"} ${esc(p.item.name)}: ${esc(e.message)}`, { err: true, ms: 6000 });
    if (S.result) renderLedger();
    if (!current() && S.result) renderCard();
  }
}

function removeFromCaches(item) {
  const pk = key(item.path);
  for (const r of new Set([...S.cache.values(), S.result].filter(Boolean))) {
    if (!under(item.path, r.root)) continue;
    r.total -= item.size;
    r.children = r.children.filter((c) => {
      if (key(c.path) === pk) return false;
      if (c.isDir && under(item.path, c.path)) { c.size -= item.size; c.files -= item.isDir ? item.files || 0 : 1; }
      return true;
    });
    r.children.sort((a, b) => b.size - a.size);
    r.topFiles = r.topFiles.filter((f) => key(f.path) !== pk && !under(f.path, item.path));
  }
  S.cache.delete(pk);
}

function keepCurrent() {
  flash($("#btnKeep"));
  const it = current();
  if (!it || isPending(it)) return;
  const k = key(it.path);
  if (!S.kept.has(k)) {
    S.kept.add(k);
    persistKept();
    S.undo.push({ type: "keep", k, item: it });
  }
  flyOut("right");
  advanceAfter(it, S.sel);
  render();
}

function undo() {
  const a = S.undo.pop();
  if (!a) return toast("Nothing to undo");
  if (a.type === "keep") {
    S.kept.delete(a.k);
    persistKept();
  } else {
    const p = S.pending.get(a.k);
    if (!p) {
      toast(isPhonePath(a.item.path)
        ? `${esc(a.item.name)} was already deleted from the phone — that can't be undone`
        : `${esc(a.item.name)} is already in the Recycle Bin — restore it from there if needed`, { ms: 5000 });
      return;
    }
    clearTimeout(p.timer);
    p.toast.remove();
    S.pending.delete(a.k);
    if (p.wasKept) { S.kept.add(a.k); persistKept(); }
  }
  const list = visible();
  const idx = list.findIndex((x) => x.path === a.item.path);
  if (idx >= 0) S.sel = idx;
  toast(`Back: ${esc(a.item.name)}`, { ms: 1600 });
  render();
}

function move(d) {
  const n = visible().length;
  if (!n) return;
  S.sel = Math.min(n - 1, Math.max(0, S.sel + d));
  renderLedger();
  renderCard();
}

function openDir() {
  const it = current();
  if (it && it.isDir) scan(it.path);
}

function up() {
  if (!S.result) return;
  const root = S.result.root.replace(/\\$/, "");
  const i = root.lastIndexOf("\\");
  if (i < 0) return;
  let parent = root.slice(0, i);
  if (!parent.includes("\\")) parent += "\\";
  if (key(parent) !== key(S.result.root)) scan(parent);
}

function toggleMedia() {
  const m = $("#deck .card:not(.flying) video, #deck .card:not(.flying) audio");
  if (!m) return;
  if (m.paused) { m.muted = m.tagName === "VIDEO" ? false : m.muted; m.play(); } else m.pause();
}

/* ---------------- wiring ---------------- */

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, textarea")) return;
  if (e.ctrlKey && e.key.toLowerCase() !== "z") return;
  const scanning = !$("#scanning").classList.contains("hidden");
  if (scanning) { if (e.key === "Escape") cancelScan(); return; }
  if ($("#work").classList.contains("hidden")) return;
  const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const map = {
    ArrowLeft: binCurrent, a: binCurrent,
    ArrowRight: keepCurrent, d: keepCurrent,
    ArrowUp: () => move(-1), w: () => move(-1),
    ArrowDown: () => move(1), s: () => move(1),
    Enter: openDir, Backspace: up,
    z: undo, " ": toggleMedia,
    e: () => current() && api("/api/reveal", { path: current().path }).catch((x) => toast(esc(x.message), { err: true })),
    o: () => {
      const it = current();
      if (!it) return;
      if (isPhonePath(it.path)) toast(`Copying ${esc(it.name)} from the phone to open it…`);
      api("/api/open", { path: it.path }).catch((x) => toast(esc(x.message), { err: true }));
    },
    Escape: cancelScan,
    r: () => S.result && scan(S.result.root, true),
    Tab: () => { S.mode = S.mode === "children" ? "topFiles" : "children"; S.sel = nextUndecided(visible(), 0); render(); },
  };
  const fn = map[k];
  if (!fn) return;
  e.preventDefault();
  S.touched = true;
  if (e.repeat && (k === "ArrowLeft" || k === "a")) return;
  fn();
});

$("#pathForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("#pathInput").value.trim().replace(/^"|"$/g, "");
  if (v) { $("#pathInput").blur(); scan(v); }
});
document.addEventListener("click", (e) => {
  const spot = e.target.closest(".spot, .crumbs button");
  if (spot) return scan(spot.dataset.path);
  const row = e.target.closest(".r");
  if (row) { S.touched = true; S.sel = +row.dataset.i; renderLedger(); renderCard(); return; }
  const mode = e.target.closest(".mode");
  if (mode) { S.mode = mode.dataset.mode; S.sel = nextUndecided(visible(), 0); render(); mode.blur(); }
});
$("#btnBin").onclick = binCurrent;
$("#btnKeep").onclick = keepCurrent;
$("#btnPrev").onclick = () => move(-1);
$("#btnNext").onclick = () => move(1);
$("#cancelScan").onclick = cancelScan;
$("#showKept").onchange = (e) => {
  const cur = current();
  S.showKept = e.target.checked;
  const list = visible();
  const i = cur ? list.findIndex((x) => x.path === cur.path) : -1;
  S.sel = i >= 0 ? i : nextUndecided(list, 0);
  e.target.blur();
  render();
};
$("#homeBtn").onclick = () => {
  if (S.polling) { clearInterval(S.polling); S.polling = null; S.scanStatus = null; api("/api/scan/cancel", {}); }
  pauseMedia();
  show("start");
  loadInfo();
};
$("#emptyBin").onclick = async () => {
  toast("Confirm in the Windows dialog — it may pop up behind this window", { ms: 5000 });
  const res = await api("/api/empty-bin", {}).catch((x) => toast(esc(x.message), { err: true }));
  if (res && res.bin) { renderBin(res.bin); renderDrives(res.drives); }
};
document.querySelectorAll("button").forEach((b) => b.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") e.preventDefault(); }));

loadInfo();
