"use strict";

/* ================= Zustand ================= */
const state = {
  videoPath: null,
  ecgFolder: null,
  recordNo: null,
  records: [],
  data: null,          // /api/ecg Payload
  offset: 0,           // EKG-Zeit = Videozeit + offset
  windowSec: 10,
  avgSec: 10,
  tab: "ecg",
  showBeats: true,
  showDevice: false,
  showGuides: true,
  derived: null,       // gecachte abgeleitete Reihen je avgSec
  sigScale: null,
};

const $ = (id) => document.getElementById(id);
const video = $("video");
const canvas = $("chart");
const ctx = canvas.getContext("2d");
if (typeof window !== "undefined") window.ekgState = state; // Debug-Zugriff

/* ================= REST-Helfer ================= */
async function api(path) {
  const res = await fetch(path);
  if (!res.ok) {
    let msg = `${res.status}`;
    try { msg = (await res.json()).detail ?? msg; } catch (_e) { /* ignore */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ================= Datei-Browser-Modal ================= */
const browser = { mode: null, path: "", entries: [], selected: null };

function openBrowser(mode) {
  browser.mode = mode;
  browser.path = "";
  browser.selected = null;
  $("modal-title").textContent = mode === "video" ? "Video auswählen" : "EKG-Ordner auswählen";
  $("modal-folder-confirm").hidden = mode === "folder";
  $("modal").hidden = false;
  doBrowse("");
}

function closeBrowser() {
  $("modal").hidden = true;
}

async function doBrowse(path) {
  $("modal-status").textContent = "";
  try {
    const res = await api(`/api/browse?path=${encodeURIComponent(path)}`);
    browser.path = res.path;
    browser.entries = res.entries;
    renderBrowser();
  } catch (e) {
    $("modal-status").textContent = `Fehler: ${e.message}`;
  }
}

function renderBrowser() {
  const bc = $("breadcrumb");
  bc.replaceChildren();
  const root = el("span", "crumb", "/ Datenordner");
  root.onclick = () => doBrowse("");
  bc.append(root);
  const parts = browser.path ? browser.path.split("/") : [];
  let acc = "";
  parts.forEach((p) => {
    acc = acc ? `${acc}/${p}` : p;
    bc.append(" › ");
    const s = el("span", "crumb", p);
    const target = acc;
    s.onclick = () => doBrowse(target);
    bc.append(s);
  });

  const list = $("browser-list");
  list.replaceChildren();
  if (!browser.entries.length) {
    list.append(el("div", "modal-empty", "Ordner ist leer"));
  }
  browser.entries.forEach((entry) => {
    if (entry.type === "dir") {
      const item = el("div", "browser-item");
      const ico = el("span", "ico", "📁");
      const name = el("span", "", entry.name);
      item.append(ico, name);
      item.onclick = () => doBrowse(entry.path);
      list.append(item);
    } else if (browser.mode === "video" && entry.type === "video") {
      const item = el("div", "browser-item");
      item.append(el("span", "ico", "🎬"), el("span", "", entry.name), el("span", "size", fmtSize(entry.size)));
      item.onclick = () => {
        state.videoPath = entry.path;
        $("pick-video-label").textContent = entry.path;
        $("pick-video-label").classList.remove("empty");
        closeBrowser();
        refreshStart();
      };
      list.append(item);
    }
  });
}

$("modal-close").onclick = closeBrowser;

$("btn-pick-video").onclick = () => openBrowser("video");
$("btn-pick-ecg").onclick = () => openBrowser("folder");
$("modal-folder-confirm").onclick = () => {
  state.ecgFolder = browser.path;
  $("pick-ecg-label").textContent = state.ecgFolder || "/ (Datenordner)";
  $("pick-ecg-label").classList.remove("empty");
  closeBrowser();
  loadRecords();
};

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$("modal").hidden) closeBrowser();
  if (!$("modal").hidden) return;
  if (ev.target.tagName === "INPUT" || ev.target.tagName === "SELECT") return;
  if ($("view-player").hidden) return;
  if (ev.code === "Space") { ev.preventDefault(); video.paused ? video.play() : video.pause(); }
  if (ev.key === "ArrowLeft") video.currentTime = Math.max(0, video.currentTime - 2);
  if (ev.key === "ArrowRight") video.currentTime += 2;
});

/* ================= Messungen ================= */
async function loadRecords() {
  const row = $("record-row");
  const note = $("record-note");
  const select = $("record-select");
  row.hidden = true;
  note.hidden = true;
  state.records = [];
  state.recordNo = null;
  try {
    const res = await api(`/api/measurements?folder=${encodeURIComponent(state.ecgFolder)}`);
    state.records = res.records;
    if (!res.records.length) {
      note.hidden = false;
      note.textContent = "In diesem Ordner wurden keine SCP-Dateien gefunden.";
      refreshStart();
      return;
    }
    select.replaceChildren();
    res.records.forEach((r) => {
      const opt = document.createElement("option");
      opt.value = r.no;
      opt.textContent = `${r.label} · ${r.files.length} Datei${r.files.length > 1 ? "en" : ""}`;
      select.append(opt);
    });
    state.recordNo = res.records[0].no;
    row.hidden = false;
  } catch (e) {
    note.hidden = false;
    note.textContent = `Fehler: ${e.message}`;
  }
  refreshStart();
}

$("record-select").onchange = (ev) => {
  state.recordNo = Number(ev.target.value);
  refreshStart();
};

/* ================= Start ================= */
function refreshStart() {
  $("btn-start").disabled =
    !(state.videoPath && state.ecgFolder !== null && state.recordNo && state.records.length);
}

$("btn-start").onclick = startPlayback;

async function startPlayback() {
  $("btn-start").disabled = true;
  $("btn-start").textContent = "Lade EKG-Analyse…";
  try {
    const res = await api(
      `/api/ecg?folder=${encodeURIComponent(state.ecgFolder)}&record=${state.recordNo}`
    );
    state.data = res;
    state.derived = null;
    state.sigScale = computeScale(res.signal);
    $("view-select").hidden = true;
    $("view-player").hidden = false;
    $("btn-reset").hidden = false;
    video.src = `/api/video?path=${encodeURIComponent(state.videoPath)}`;
    video.play().catch(() => { /* Autoplay evtl. blockiert */ });
    $("footnote").textContent =
      `${res.files.join(", ")} · ${res.fs} Hz · ${res.duration.toFixed(1)} s · ` +
      `${res.beats_idx.length} Herzschläge (Pan-Tompkins), ` +
      `${res.device_beats_idx.length} Geräte-Marken`;
    renderLegend();
  } catch (e) {
    alert(`EKG konnte nicht geladen werden: ${e.message}`);
  }
  $("btn-start").textContent = "Wiedergabe starten";
  refreshStart();
}

$("btn-reset").onclick = () => {
  state.data = null;
  $("view-player").hidden = true;
  $("view-select").hidden = false;
  $("btn-reset").hidden = true;
  video.pause();
};

/* ================= Abgeleitete Reihen ================= */
function computeScale(signal) {
  const n = signal.length;
  if (!n) return { min: -100, max: 100 };
  const s = Float64Array.from(signal);
  s.sort();
  const p = (q) => s[Math.min(n - 1, Math.floor(q * n))];
  let lo = p(0.02), hi = p(0.98);
  const pad = Math.max((hi - lo) * 0.18, 8);
  return { min: lo - pad, max: hi + pad };
}

/* geglättete HR + Ableitung auf 0,25-s-Raster, gecacht je avgSec */
function derived() {
  const d = state.data;
  if (!d) return null;
  if (state.derived && state.derived.avgSec === state.avgSec) return state.derived;

  const step = 0.25;
  const gridT = [];
  for (let t = 0; t <= d.duration + 1e-9; t += step) gridT.push(+t.toFixed(3));

  const hrT = d.hr_t, hr = d.hr;
  const half = state.avgSec / 2;

  // geglättete HR: Mittel über Schläge in [t-half, t+half]
  const n = hrT.length;
  let j0 = 0, j1 = 0, sum = 0;
  const hrAvg = new Array(gridT.length);
  for (let i = 0; i < gridT.length; i++) {
    const t = gridT[i];
    if (n === 0) { hrAvg[i] = null; continue; }
    while (j1 < n && hrT[j1] <= t + half) { sum += hr[j1]; j1++; }
    while (j0 < j1 && hrT[j0] < t - half) { sum -= hr[j0]; j0++; }
    hrAvg[i] = j1 > j0 ? sum / (j1 - j0) : null;
  }

  // Ableitung der geglätteten HR (bpm/min), zentrierte Differenzen,
  // anschließend leicht nachgeglättet
  const rawDhr = new Array(gridT.length).fill(null);
  for (let i = 1; i < gridT.length - 1; i++) {
    if (hrAvg[i - 1] === null || hrAvg[i + 1] === null) continue;
    rawDhr[i] = (hrAvg[i + 1] - hrAvg[i - 1]) / (2 * step) * 60;
  }
  const dhr = new Array(gridT.length).fill(null);
  for (let i = 0; i < gridT.length; i++) {
    let acc = 0, cnt = 0;
    for (let k = Math.max(0, i - 2); k <= Math.min(gridT.length - 1, i + 2); k++) {
      if (rawDhr[k] !== null) { acc += rawDhr[k]; cnt++; }
    }
    dhr[i] = cnt ? acc / cnt : null;
  }

  // momentane (Schritt-)HR am Rasterpunkt
  const hrStep = new Array(gridT.length).fill(null);
  let b = 0;
  for (let i = 0; i < gridT.length; i++) {
    while (b < hrT.length && hrT[b] <= gridT[i]) b++;
    hrStep[i] = b > 0 ? hr[b - 1] : null;
  }

  state.derived = { avgSec: state.avgSec, step, gridT, hrAvg, dhr, hrStep };
  return state.derived;
}

function currentEcgTime() {
  return (video.currentTime || 0) + state.offset;
}

function seriesAt(series, t) {
  const d = derived();
  if (!d || !series) return null;
  const i = Math.round(t / d.step);
  if (i < 0 || i >= series.length) return null;
  return series[i];
}

function hrAt(t) {
  const d = state.data;
  if (!d) return null;
  const ts = d.hr_t;
  let lo = 0, hi = ts.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] <= t) { best = mid; lo = mid + 1; } else hi = mid - 1;
  }
  return best >= 0 ? d.hr[best] : null;
}

function rrAt(t) {
  const d = state.data;
  if (!d || !d.hr_t.length) return null;
  let lo = 0, hi = d.hr_t.length - 1, best = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (Math.abs(d.hr_t[mid] - t) < Math.abs(d.hr_t[best] - t)) best = mid;
    if (d.hr_t[mid] < t) lo = mid + 1; else hi = mid - 1;
  }
  return d.rr[best];
}

/* ================= Statistiken ================= */
function updateStats() {
  const d = state.data;
  if (!d) return;
  const t = currentEcgTime();
  const inRange = t >= 0 && t <= d.duration;
  const inst = inRange ? hrAt(t) : null;
  const sm = inRange ? seriesAt(derived()?.hrAvg, t) : null;
  const rr = inRange ? rrAt(t) : null;

  $("bpm-now").textContent = inst != null ? Math.round(inst) : "–";
  $("bpm-now").style.color =
    inst == null ? "var(--muted)" :
    inst > 130 ? "var(--red)" :
    inst > 100 ? "var(--amber)" : "var(--green)";
  $("bpm-avg").textContent = sm != null ? Math.round(sm) : "–";
  $("rr-now").textContent = rr != null ? Math.round(rr * 1000) : "–";

  if (!d.hr.length) { $("bpm-mean").textContent = "–"; $("bpm-range").textContent = "–"; }
  else {
    const min = Math.min(...d.hr), max = Math.max(...d.hr);
    $("bpm-mean").textContent = Math.round(d.hr.reduce((a, b) => a + b, 0) / d.hr.length);
    $("bpm-range").textContent = `${Math.round(min)} / ${Math.round(max)}`;
  }
}

/* ================= Canvas ================= */
function fitCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function renderLegend() {
  const legend = $("chart-legend");
  legend.replaceChildren();
  const item = (txt, color) => {
    const s = document.createElement("span");
    s.textContent = txt;
    s.style.color = color || "var(--muted)";
    return s;
  };
  if (state.tab === "ecg") {
    legend.append(item("EKG (ADC, 12 bit)", "var(--green)"));
    if (state.showDevice) legend.append(item("Geräte-R-Marke", "var(--amber)"));
  } else if (state.tab === "hr") {
    legend.append(item("momentane HR", "rgba(124,196,255,0.8)"));
    legend.append(item(`Mittelwert ${state.avgSec} s`, "var(--accent)"));
  } else {
    legend.append(item("ΔHR/Δt (bpm/min)", "#b48cff"));
  }
}

function draw() {
  fitCanvas();
  const w = canvas.clientWidth, h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  const d = state.data;
  if (!d) return;
  const t = currentEcgTime();
  if (state.tab === "ecg") drawEcg(t, w, h);
  else drawTimeline(d, t, w, h);
  updateStats();
}

/* ---------- EKG-Fenster ---------- */
function drawEcg(t, w, h) {
  const d = state.data;
  const fs = d.fs;
  const playFrac = 0.65;
  const win = state.windowSec;
  const t0 = t - win * playFrac;
  const padL = 44, padR = 8, padT = 8, padB = 22;
  const pw = w - padL - padR, ph = h - padT - padB;

  const scale = state.sigScale;
  const y = (v) => padT + ((scale.max - v) / (scale.max - scale.min)) * ph;
  const x = (tt) => padL + ((tt - t0) / win) * pw;

  // Raster: 0,2 s fein, 1 s grob
  ctx.lineWidth = 1;
  for (let tg = Math.floor(t0 / 0.2) * 0.2; tg <= t0 + win + 0.2; tg += 0.2) {
    ctx.strokeStyle = "#182230";
    if (Math.abs((tg / 1) % 1) < 0.001) ctx.strokeStyle = "#24354a";
    ctx.beginPath();
    ctx.moveTo(x(tg), padT);
    ctx.lineTo(x(tg), padT + ph);
    ctx.stroke();
  }
  for (let k = 0; k <= 4; k++) {
    const vy = scale.min + (k / 4) * (scale.max - scale.min);
    ctx.strokeStyle = k === 2 ? "#24354a" : "#182230";
    ctx.beginPath();
    ctx.moveTo(padL, y(vy));
    ctx.lineTo(w - padR, y(vy));
    ctx.stroke();
  }

  // Skalenbeschriftung
  ctx.fillStyle = "#8b98a8";
  ctx.font = "11px ui-monospace, Menlo, monospace";
  ctx.textAlign = "right";
  for (let k = 0; k <= 4; k++) {
    const vy = scale.min + (k / 4) * (scale.max - scale.min);
    ctx.fillText(Math.round(vy), padL - 6, y(vy) + 4);
  }
  ctx.textAlign = "center";
  for (let tg = Math.ceil(t0); tg <= t0 + win; tg++) {
    ctx.fillText(`${tg.toFixed(0)}s`, x(tg), h - 8);
  }

  // Signal: pro Pixelspalte min/max (Erhalt der R-Amplitude)
  const sig = d.signal;
  ctx.strokeStyle = "rgba(47,212,140,0.95)";
  ctx.lineWidth = 1.6;
  ctx.beginPath();
  let started = false;
  for (let px = 0; px <= pw; px++) {
    const tt = t0 + (px / pw) * win;
    const i0 = Math.max(0, Math.floor(tt * fs));
    const i1 = Math.min(sig.length, Math.ceil((tt + win / pw) * fs));
    if (i1 <= i0) continue;
    let mn = Infinity, mx = -Infinity;
    for (let i = i0; i < i1; i++) {
      const v = sig[i];
      if (v < mn) mn = v;
      if (v > mx) mx = v;
    }
    const cx = padL + px;
    if (!started) { ctx.moveTo(cx, y(mn)); started = true; }
    ctx.lineTo(cx, y(mn));
    ctx.lineTo(cx, y(mx));
  }
  if (started) ctx.stroke();

  // Marken
  const idxT = (idx) => idx / fs;
  if (state.showBeats) {
    ctx.strokeStyle = "rgba(47,212,140,0.5)";
    for (const bi of d.beats_idx) {
      const tt = idxT(bi);
      if (tt >= t0 && tt <= t0 + win) {
        ctx.beginPath();
        ctx.moveTo(x(tt), padT + ph - 6);
        ctx.lineTo(x(tt), padT + ph);
        ctx.stroke();
      }
    }
  }
  if (state.showDevice) {
    ctx.strokeStyle = "rgba(255,180,84,0.8)";
    for (const bi of d.device_beats_idx) {
      const tt = idxT(bi);
      if (tt >= t0 && tt <= t0 + win) {
        ctx.beginPath();
        ctx.moveTo(x(tt), padT + ph - 6);
        ctx.lineTo(x(tt), padT + ph);
        ctx.stroke();
      }
    }
  }

  // Playhead
  ctx.strokeStyle = "rgba(232,238,245,0.9)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(x(t), padT);
  ctx.lineTo(x(t), padT + ph);
  ctx.stroke();
}

/* ---------- Zeitleisten (HR / Ableitung) ---------- */
function drawTimeline(d, t, w, h) {
  const padL = 44, padR = 8, padT = 10, padB = 24;
  const pw = w - padL - padR, ph = h - padT - padB;
  const dur = Math.max(d.duration, 0.001);
  const x = (tt) => padL + (tt / dur) * pw;

  const dv = derived();
  let lo = Infinity, hi = -Infinity;
  const series = state.tab === "hr" ? dv.hrAvg : dv.dhr;
  const stepSeries = dv.hrStep;
  for (const v of (state.tab === "hr" ? stepSeries : series) ?? []) {
    if (v != null && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
  }
  if (state.tab === "hr") {
    for (const v of series ?? []) {
      if (v != null && isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    }
  }
  if (!isFinite(lo)) { lo = 40; hi = 120; }
  if (lo === hi) { lo -= 10; hi += 10; }
  const pad = (hi - lo) * 0.12;
  lo -= pad; hi += pad;
  if (state.tab === "dhr") { lo = Math.min(lo, -5); hi = Math.max(hi, 5); }
  const y = (v) => padT + ((hi - v) / (hi - lo)) * ph;

  // Netz & Achsen
  ctx.font = "11px ui-monospace, Menlo, monospace";
  ctx.fillStyle = "#8b98a8";
  for (let k = 0; k <= 4; k++) {
    const vy = lo + (k / 4) * (hi - lo);
    ctx.strokeStyle = (state.tab === "dhr" && Math.abs(vy) < 1e-9) ? "#3a5170" : "#182230";
    ctx.beginPath();
    ctx.moveTo(padL, y(vy));
    ctx.lineTo(w - padR, y(vy));
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(state.tab === "dhr" ? vy.toFixed(0) : Math.round(vy), padL - 6, y(vy) + 4);
  }
  const tickStep = niceStep(dur / 8);
  ctx.textAlign = "center";
  for (let tt = 0; tt <= dur + 1e-9; tt += tickStep) {
    ctx.strokeStyle = "#24354a";
    ctx.beginPath();
    ctx.moveTo(x(tt), padT + ph);
    ctx.lineTo(x(tt), padT + ph + 4);
    ctx.stroke();
    ctx.fillText(fmtTime(tt), x(tt), h - 8);
  }

  if (state.tab === "hr") {
    // momentane HR als zarte Stufenlinie
    ctx.strokeStyle = "rgba(124,196,255,0.45)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    let pen = false;
    let prevY = null;
    for (let i = 0; i < dv.gridT.length; i++) {
      const v = stepSeries[i];
      if (v == null) { pen = false; continue; }
      const yy = y(v);
      if (pen) {
        ctx.lineTo(x(dv.gridT[i]), prevY);
        ctx.lineTo(x(dv.gridT[i]), yy);
      } else {
        ctx.moveTo(x(dv.gridT[i]), yy);
        pen = true;
      }
      prevY = yy;
    }
    ctx.stroke();
  }

  // Hauptreihe (geglättete HR bzw. Ableitung)
  const main = series ?? [];
  const yZero = y(0);
  if (state.tab === "dhr") {
    // getrennte Segmente Anstieg (grün) / Abfall (rot) mit Flächenfüllung
    for (const sign of [1, -1]) {
      ctx.strokeStyle = sign > 0 ? "rgba(47,212,140,0.95)" : "rgba(255,107,107,0.85)";
      ctx.lineWidth = 2;
      ctx.fillStyle = sign > 0 ? "rgba(47,212,140,0.16)" : "rgba(255,107,107,0.14)";
      let line = null, last = null;
      const finish = () => {
        if (!line) return;
        line.lineTo(last, yZero);
        line.closePath();
        ctx.fill(line);
        ctx.stroke(line);
        line = null;
      };
      for (let i = 0; i < dv.gridT.length; i++) {
        const v = main[i];
        if (v === null || sign * v <= 0) { finish(); continue; }
        const xx = x(dv.gridT[i]), yy = y(v);
        if (!line) {
          line = new Path2D();
          line.moveTo(xx, yZero);
          line.lineTo(xx, yy);
        } else {
          line.lineTo(xx, yy);
        }
        last = xx;
      }
      finish();
    }
  } else {
    ctx.lineWidth = 2.4;
    ctx.strokeStyle = "#4da3ff";
    ctx.beginPath();
    let pen = false;
    for (let i = 0; i < dv.gridT.length; i++) {
      const v = main[i];
      if (v == null) { pen = false; continue; }
      const xx = x(dv.gridT[i]), yy = y(v);
      if (!pen) { ctx.moveTo(xx, yy); pen = true; } else ctx.lineTo(xx, yy);
    }
    ctx.stroke();
  }

  // ±5-s-Führungslinien und Playhead
  const phX = x(t);
  if (state.showGuides) {
    ctx.strokeStyle = "rgba(232,238,245,0.18)";
    ctx.lineWidth = 1;
    for (const off of [-5, 5]) {
      const gx = x(t + off);
      if (gx > padL && gx < w - padR) {
        ctx.beginPath();
        ctx.moveTo(gx, padT);
        ctx.lineTo(gx, padT + ph);
        ctx.stroke();
      }
    }
  }
  ctx.strokeStyle = "rgba(232,238,245,0.9)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(phX, padT);
  ctx.lineTo(phX, padT + ph);
  ctx.stroke();
}

function niceStep(raw) {
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for (const s of steps) if (raw <= s) return s;
  return steps[steps.length - 1];
}

function fmtTime(t) {
  const m = Math.floor(t / 60), s = Math.round(t % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

function fmtSize(bytes) {
  if (bytes > 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes > 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${(bytes / 1e3).toFixed(0)} kB`;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

/* ---------- Klick zum Spulen ---------- */
canvas.addEventListener("click", (ev) => {
  const d = state.data;
  if (!d) return;
  const rect = canvas.getBoundingClientRect();
  const px = ev.clientX - rect.left;
  if (state.tab === "ecg") {
    const playFrac = 0.65, win = state.windowSec;
    const t0 = currentEcgTime() - win * playFrac;
    const tt = t0 + ((px - 44) / (rect.width - 52)) * win;
    if (tt >= 0) video.currentTime = Math.max(0, tt - state.offset);
  } else {
    const tt = ((px - 44) / (rect.width - 52)) * d.duration;
    video.currentTime = Math.max(0, tt - state.offset);
  }
});

/* ================= Steuerung ================= */
document.querySelectorAll(".tab").forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.tab = btn.dataset.tab;
    renderLegend();
    draw();
  };
});

$("chk-beats").onchange = (e) => { state.showBeats = e.target.checked; renderLegend(); };
$("chk-device").onchange = (e) => { state.showDevice = e.target.checked; renderLegend(); };
$("chk-guides").onchange = (e) => { state.showGuides = e.target.checked; };
$("offset-input").oninput = (e) => { state.offset = Number(e.target.value) || 0; };
$("window-input").oninput = (e) => { state.windowSec = Math.min(60, Math.max(2, Number(e.target.value) || 10)); };
$("avg-input").oninput = (e) => {
  state.avgSec = Math.min(120, Math.max(0, Number(e.target.value) || 0));
  renderLegend();
};

/* ================= Render-Schleife ================= */
function loop() {
  if (state.data) draw();
  requestAnimationFrame(loop);
}
new ResizeObserver(() => draw()).observe(canvas);
loop();

/* ================= Deep-Link (Autostart) =================
   Beispiel: /?video=videos/kampf.mp4&ecg=beispieldaten&record=1&offset=0 */
(async function autostart() {
  const p = new URLSearchParams(location.search);
  const v = p.get("video"), f = p.get("ecg");
  if (!v || !f) return;
  state.videoPath = v;
  state.ecgFolder = f;
  $("pick-video-label").textContent = v;
  $("pick-video-label").classList.remove("empty");
  $("pick-ecg-label").textContent = f;
  $("pick-ecg-label").classList.remove("empty");
  await loadRecords();
  const r = p.get("record");
  if (r && state.records.some((x) => x.no === Number(r))) {
    state.recordNo = Number(r);
    $("record-select").value = r;
  }
  const o = p.get("offset");
  if (o && isFinite(Number(o))) {
    state.offset = Number(o);
    $("offset-input").value = o;
  }
  refreshStart();
  if (!$("btn-start").disabled) startPlayback();
})();
