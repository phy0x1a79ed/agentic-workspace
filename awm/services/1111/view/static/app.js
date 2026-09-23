"use strict";

const PAGE = 200;
const IMAGES_MIME = "application/x-view-images";
const FOLDER_MIME = "application/x-view-folder";

const $ = (sel) => document.querySelector(sel);
const el = (tag, attrs = {}, ...children) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
};

const state = {
  source: "dir:",
  items: [],
  total: 0,
  loading: false,
  done: false,
  generation: 0,
  selected: new Set(),
  anchor: null,
  clipboard: [],
  dirs: [],
  folders: [],
  viewerIndex: -1,
  infoOpen: true,
  expanded: loadExpanded(),
};

function loadExpanded() {
  try { return new Set(JSON.parse(localStorage.getItem("view.expanded") || '[""]')); }
  catch { return new Set([""]); }
}
function saveExpanded() {
  try { localStorage.setItem("view.expanded", JSON.stringify([...state.expanded])); } catch {}
}

async function api(path, body, method) {
  const opts = { method: method || (body === undefined ? "GET" : "POST"), headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch("api/" + path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 2500);
}
async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message); }
}

// ---- sidebar ------------------------------------------------------------

async function refreshTree() {
  const t = await api("tree");
  state.dirs = t.dirs;
  state.folders = t.folders;
  $("#trash-count").textContent = t.trash || "";
  $("#scan-status").textContent = t.scanning ? "Scanning…" : "";
  renderDirs();
  renderFolders();
  markActive();
  return t;
}

function sourceTotal(t) {
  const [kind, arg] = splitSource(state.source);
  if (kind === "dir") return (t.dirs.find((d) => d.path === arg) || { total: 0 }).total;
  if (kind === "folder") return (t.folders.find((f) => String(f.id) === arg) || { count: 0 }).count;
  return t.trash;
}

function splitSource(source) {
  const i = source.indexOf(":");
  return i < 0 ? [source, ""] : [source.slice(0, i), source.slice(i + 1)];
}

function renderDirs() {
  const byParent = new Map();
  for (const d of state.dirs) {
    if (d.path === "") continue;
    const parent = d.path.includes("/") ? d.path.slice(0, d.path.lastIndexOf("/")) : "";
    if (!byParent.has(parent)) byParent.set(parent, []);
    byParent.get(parent).push(d);
  }
  for (const list of byParent.values()) list.sort((a, b) => b.path.localeCompare(a.path));
  const root = state.dirs.find((d) => d.path === "") || { path: "", total: 0 };
  const build = (d, label) => {
    const kids = byParent.get(d.path) || [];
    const open = state.expanded.has(d.path);
    const twisty = el("span", {
      class: "twisty" + (kids.length ? "" : " leaf"), text: kids.length ? (open ? "▾" : "▸") : "",
      onclick: (e) => {
        e.stopPropagation();
        if (open) state.expanded.delete(d.path); else state.expanded.add(d.path);
        saveExpanded();
        renderDirs();
        markActive();
      },
    });
    const node = el("div", { class: "node", "data-source": "dir:" + d.path },
      twisty, el("span", { class: "label", text: label }), el("span", { class: "count", text: d.total }));
    const li = el("li", {}, node);
    if (kids.length && open) li.append(el("ul", {}, ...kids.map((k) => build(k, k.path.split("/").pop()))));
    return li;
  };
  $("#dirs").replaceChildren(build(root, "All images"));
}

function folderPath(id) {
  const byId = new Map(state.folders.map((f) => [f.id, f]));
  const parts = [];
  for (let f = byId.get(id); f; f = byId.get(f.parent_id)) parts.unshift(f.name);
  return parts.join(" / ");
}

function renderFolders() {
  const byParent = new Map();
  for (const f of state.folders) {
    const k = f.parent_id ?? null;
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k).push(f);
  }
  const build = (f) => {
    const kids = byParent.get(f.id) || [];
    const node = el("div", { class: "node folder", "data-source": "folder:" + f.id, "data-folder": f.id, draggable: "true" },
      el("span", { class: "twisty leaf" }),
      el("span", { class: "label", text: f.name }),
      el("span", { class: "count", text: f.count }),
      el("span", { class: "actions" },
        el("button", { title: "New subfolder", text: "+", onclick: (e) => { e.stopPropagation(); newFolder(f.id); } }),
        el("button", { title: "Rename", text: "✎", onclick: (e) => { e.stopPropagation(); startRename(node, f); } }),
        el("button", { title: "Delete folder", text: "×", onclick: (e) => { e.stopPropagation(); deleteFolder(f); } })));
    node.addEventListener("dblclick", (e) => { e.stopPropagation(); startRename(node, f); });
    node.addEventListener("dragstart", (e) => {
      e.dataTransfer.setData(FOLDER_MIME, String(f.id));
      e.dataTransfer.effectAllowed = "move";
    });
    wireDrop(node, f.id);
    const li = el("li", {}, node);
    if (kids.length) li.append(el("ul", {}, ...kids.map(build)));
    return li;
  };
  const roots = byParent.get(null) || [];
  $("#folders").replaceChildren(...(roots.length ? roots.map(build)
    : [el("li", { class: "hint", text: "Drag images onto a folder here to organize them." })]));
}

function markActive() {
  for (const n of document.querySelectorAll("#sidebar .node")) {
    n.classList.toggle("active", n.dataset.source === state.source);
  }
}

function wireDrop(target, folderId) {
  target.addEventListener("dragover", (e) => {
    const types = e.dataTransfer.types;
    if (!types.includes(IMAGES_MIME) && !types.includes(FOLDER_MIME)) return;
    e.preventDefault();
    const moving = types.includes(IMAGES_MIME) && state.source.startsWith("folder:") && !(e.ctrlKey || e.metaKey);
    e.dataTransfer.dropEffect = types.includes(FOLDER_MIME) || moving ? "move" : "copy";
    target.classList.add("drop");
  });
  target.addEventListener("dragleave", () => target.classList.remove("drop"));
  target.addEventListener("drop", (e) => {
    e.preventDefault();
    target.classList.remove("drop");
    const folder = e.dataTransfer.getData(FOLDER_MIME);
    if (folder) return guarded(() => reparent(Number(folder), folderId));
    const raw = e.dataTransfer.getData(IMAGES_MIME);
    if (!raw || folderId == null) return;
    const { ids, from } = JSON.parse(raw);
    const [kind, arg] = splitSource(from);
    const copy = e.ctrlKey || e.metaKey || kind !== "folder";
    guarded(() => (copy ? addTo(folderId, ids) : moveTo(Number(arg), folderId, ids)));
  });
}

async function reparent(id, parentId) {
  if (id === parentId) return;
  await api(`folders/${id}`, { parent_id: parentId }, "PATCH");
  await refreshTree();
}

async function newFolder(parentId) {
  const name = prompt(parentId ? `New folder inside "${folderPath(parentId)}"` : "New folder name");
  if (!name) return;
  await guarded(async () => {
    await api("folders", { name, parent_id: parentId ?? null });
    await refreshTree();
  });
}

function startRename(node, f) {
  const label = node.querySelector(".label");
  const input = el("input", { class: "rename", value: f.name });
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    if (commit && input.value.trim() && input.value !== f.name) {
      await guarded(() => api(`folders/${f.id}`, { name: input.value }, "PATCH"));
    }
    await refreshTree();
  };
  input.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
  label.replaceWith(input);
  input.focus();
  input.select();
}

async function deleteFolder(f) {
  if (!confirm(`Delete folder "${folderPath(f.id)}" and its subfolders?\nThe images themselves are not deleted.`)) return;
  await guarded(async () => {
    await api(`folders/${f.id}`, undefined, "DELETE");
    await refreshTree();
    if (state.source.startsWith("folder:") && !state.folders.some((x) => `folder:${x.id}` === state.source)) {
      setSource("dir:");
    }
  });
}

async function addTo(folderId, ids) {
  const r = await api(`folders/${folderId}/add`, { ids });
  toast(`Added ${r.added} to ${folderPath(folderId)}`);
  await refreshTree();
}

async function moveTo(fromId, folderId, ids) {
  if (fromId === folderId) return;
  await api(`folders/${folderId}/move`, { from: fromId, ids });
  toast(`Moved ${ids.length} to ${folderPath(folderId)}`);
  if (state.source === `folder:${fromId}`) dropItems(ids);
  await refreshTree();
}

// ---- grid ---------------------------------------------------------------

function setSource(source) {
  state.source = source;
  state.items = [];
  state.total = 0;
  state.done = false;
  state.generation++;
  state.selected.clear();
  state.anchor = null;
  $("#grid").replaceChildren();
  document.querySelector("main").scrollTop = 0;
  markActive();
  updateToolbar();
  loadMore();
}

function sourceTitle() {
  const [kind, arg] = splitSource(state.source);
  if (kind === "dir") return arg || "All images";
  if (kind === "folder") return folderPath(Number(arg)) || "Folder";
  return "Trash";
}

async function loadMore() {
  if (state.loading || state.done) return;
  state.loading = true;
  const gen = state.generation;
  try {
    const r = await api(`images?source=${encodeURIComponent(state.source)}&offset=${state.items.length}&limit=${PAGE}`);
    if (gen !== state.generation) return;
    state.total = r.total;
    const start = state.items.length;
    state.items.push(...r.items);
    state.done = state.items.length >= r.total || r.items.length === 0;
    $("#grid").append(...r.items.map((it, i) => cell(it, start + i)));
    updateToolbar();
  } catch (e) {
    toast(e.message);
  } finally {
    if (gen === state.generation) state.loading = false;
  }
  if (gen === state.generation && !state.done && sentinelVisible()) loadMore();
}

async function loadAll() {
  while (!state.done) {
    if (state.loading) await new Promise((r) => setTimeout(r, 50));
    else await loadMore();
  }
}

function sentinelVisible() {
  const r = $("#sentinel").getBoundingClientRect();
  return r.top < window.innerHeight + 800;
}

function cell(it, index) {
  const img = el("img", { src: `thumb/${it.id}?b=400`, loading: "lazy", alt: "", draggable: "false" });
  img.addEventListener("error", () => c.classList.add("broken"));
  const check = el("span", { class: "check", title: "Select" });
  const c = el("div", { class: "cell", "data-id": it.id, draggable: "true", title: it.relpath }, img, check);
  c.style.setProperty("--r", it.width && it.height ? it.width / it.height : 1);
  if (it.broken) c.classList.add("broken");
  if (state.selected.has(it.id)) c.classList.add("selected");
  check.addEventListener("click", (e) => { e.stopPropagation(); toggle(it.id, indexOf(it.id)); });
  c.addEventListener("click", (e) => onCellClick(e, it.id));
  c.addEventListener("dragstart", (e) => {
    const ids = state.selected.has(it.id) ? [...state.selected] : [it.id];
    e.dataTransfer.setData(IMAGES_MIME, JSON.stringify({ ids, from: state.source }));
    e.dataTransfer.setData("text/plain", `${ids.length} image(s)`);
    e.dataTransfer.effectAllowed = "copyMove";
  });
  return c;
}

function indexOf(id) {
  return state.items.findIndex((x) => x.id === id);
}

function onCellClick(e, id) {
  const index = indexOf(id);
  if (e.shiftKey && state.anchor != null) {
    const a = indexOf(state.anchor);
    const [lo, hi] = a < index ? [a, index] : [index, a];
    if (!(e.ctrlKey || e.metaKey)) state.selected.clear();
    for (let i = lo; i <= hi; i++) state.selected.add(state.items[i].id);
    syncSelection();
  } else if (e.ctrlKey || e.metaKey || state.selected.size) {
    toggle(id, index);
  } else {
    openViewer(index);
  }
}

function toggle(id) {
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  state.anchor = id;
  syncSelection();
}

function syncSelection() {
  for (const c of $("#grid").children) c.classList.toggle("selected", state.selected.has(Number(c.dataset.id)));
  updateToolbar();
}

function dropItems(ids) {
  const gone = new Set(ids);
  state.items = state.items.filter((x) => !gone.has(x.id));
  state.total -= ids.length;
  for (const c of [...$("#grid").children]) if (gone.has(Number(c.dataset.id))) c.remove();
  for (const id of ids) state.selected.delete(id);
  updateToolbar();
  if (sentinelVisible()) loadMore();
}

function updateToolbar() {
  const [kind] = splitSource(state.source);
  const n = state.selected.size;
  $("#title").textContent = `${sourceTitle()} · ${state.total}`;
  $("#selection-info").textContent = n ? `${n} selected` : "";
  for (const b of document.querySelectorAll("[data-needs-selection]")) b.disabled = !n;
  $("#btn-add").hidden = kind === "trash";
  $("#btn-trash").hidden = kind === "trash";
  $("#btn-remove").hidden = kind !== "folder";
  $("#btn-restore").hidden = kind !== "trash";
  $("#btn-purge-sel").hidden = kind !== "trash";
  $("#btn-empty").hidden = kind !== "trash";
}

async function trashSelected(ids) {
  if (!ids.length) return;
  const r = await api("trash", { ids });
  toast(`Moved ${r.trashed} to trash`);
  dropItems(ids);
  await refreshTree();
}

// ---- actions ------------------------------------------------------------

async function pickFolder() {
  const dlg = $("#picker");
  const list = $("#picker-list");
  const input = $("#picker-new");
  input.value = "";
  const byParent = new Map();
  for (const f of state.folders) {
    const k = f.parent_id ?? null;
    if (!byParent.has(k)) byParent.set(k, []);
    byParent.get(k).push(f);
  }
  return new Promise((resolve) => {
    const choose = (id) => { resolve(id); dlg.close(); };
    const rows = [];
    const walk = (parent, depth) => {
      for (const f of byParent.get(parent) || []) {
        rows.push(el("li", {}, el("div", {
          class: "node", style: `padding-left:${depth * 14 + 6}px`, onclick: () => choose(f.id),
        }, el("span", { class: "label", text: f.name }), el("span", { class: "count", text: f.count }))));
        walk(f.id, depth + 1);
      }
    };
    walk(null, 0);
    list.replaceChildren(...rows);
    input.onkeydown = async (e) => {
      e.stopPropagation();
      if (e.key !== "Enter") return;
      e.preventDefault();
      const name = input.value.trim();
      if (!name) return;
      const r = await guarded(() => api("folders", { name, parent_id: null }));
      if (r) choose(r.id);
    };
    dlg.onclose = () => resolve(null);
    dlg.showModal();
    input.focus();
  });
}

function wireToolbar() {
  $("#btn-add").onclick = () => guarded(async () => {
    const ids = [...state.selected];
    const folder = await pickFolder();
    if (folder != null) await addTo(folder, ids);
  });
  $("#btn-remove").onclick = () => guarded(async () => {
    const [, arg] = splitSource(state.source);
    const ids = [...state.selected];
    await api(`folders/${arg}/remove`, { ids });
    dropItems(ids);
    await refreshTree();
  });
  $("#btn-trash").onclick = () => guarded(() => trashSelected([...state.selected]));
  $("#btn-restore").onclick = () => guarded(async () => {
    const ids = [...state.selected];
    const r = await api("restore", { ids });
    toast(`Restored ${r.restored}` + (r.skipped.length ? `, ${r.skipped.length} skipped (file exists)` : ""));
    dropItems(ids.filter((i) => !r.skipped.includes(i)));
    await refreshTree();
  });
  $("#btn-purge-sel").onclick = () => guarded(async () => {
    const ids = [...state.selected];
    if (!confirm(`Permanently delete ${ids.length} image(s)? This cannot be undone.`)) return;
    await api("purge", { ids });
    dropItems(ids);
    await refreshTree();
  });
  $("#btn-empty").onclick = () => guarded(async () => {
    if (!confirm(`Permanently delete all ${state.total} image(s) in the trash? This cannot be undone.`)) return;
    const r = await api("purge", { ids: null });
    toast(`Deleted ${r.purged}`);
    await refreshTree();
    setSource("trash");
  });
  $("#new-folder").onclick = () => newFolder(null);
  $("#zoom").oninput = (e) => {
    document.documentElement.style.setProperty("--cell", e.target.value + "px");
    try { localStorage.setItem("view.zoom", e.target.value); } catch {}
  };
  try {
    const z = localStorage.getItem("view.zoom");
    if (z) { $("#zoom").value = z; document.documentElement.style.setProperty("--cell", z + "px"); }
  } catch {}
  $("#sidebar").addEventListener("click", (e) => {
    const node = e.target.closest(".node[data-source]");
    if (node) setSource(node.dataset.source);
  });
  // Dropping a folder on the section header moves it back to the top level.
  const header = $("#new-folder").parentElement;
  header.addEventListener("dragover", (e) => {
    if (!e.dataTransfer.types.includes(FOLDER_MIME)) return;
    e.preventDefault();
    header.classList.add("drop");
  });
  header.addEventListener("dragleave", () => header.classList.remove("drop"));
  header.addEventListener("drop", (e) => {
    e.preventDefault();
    header.classList.remove("drop");
    const folder = e.dataTransfer.getData(FOLDER_MIME);
    if (folder) guarded(() => reparent(Number(folder), null));
  });
}

// ---- viewer -------------------------------------------------------------

function openViewer(index) {
  if (index < 0 || index >= state.items.length) return;
  state.viewerIndex = index;
  $("#viewer").hidden = false;
  $("#viewer").classList.toggle("no-info", !state.infoOpen);
  document.body.classList.add("viewing");
  showCurrent();
}

function closeViewer() {
  $("#viewer").hidden = true;
  document.body.classList.remove("viewing");
  const it = state.items[state.viewerIndex];
  state.viewerIndex = -1;
  const c = it && $(`.cell[data-id="${it.id}"]`);
  if (c) c.scrollIntoView({ block: "nearest" });
}

async function step(delta) {
  let i = state.viewerIndex + delta;
  if (i >= state.items.length && !state.done) await loadMore();
  if (i < 0 || i >= state.items.length) return;
  state.viewerIndex = i;
  showCurrent();
}

function showCurrent() {
  const it = state.items[state.viewerIndex];
  if (!it) return closeViewer();
  resetZoom();
  $("#full").src = `file/${it.id}`;
  $("#counter").textContent = `${state.viewerIndex + 1} / ${state.total}`;
  for (const d of [1, -1]) {
    const n = state.items[state.viewerIndex + d];
    if (n) new Image().src = `file/${n.id}`;
  }
  renderInfo(it.id);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = el("textarea", {}, text);
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast("Copied");
}

const SHOWN = [
  ["Model", "Model"], ["Sampler", "Sampler"], ["Schedule type", "Schedule"], ["Steps", "Steps"],
  ["CFG scale", "CFG"], ["Seed", "Seed"], ["Size", "Size"], ["Denoising strength", "Denoising"],
  ["Clip skip", "Clip skip"], ["Hires upscale", "Hires upscale"], ["Hires upscaler", "Hires upscaler"],
  ["Hires steps", "Hires steps"],
];

async function renderInfo(id) {
  const box = $("#info");
  const img = await guarded(() => api(`image/${id}`));
  if (!img || state.items[state.viewerIndex]?.id !== id) return;
  const m = img.meta;
  const parts = [];
  const section = (title, ...body) => el("section", {}, el("h4", { text: title }), ...body);
  if (m) {
    parts.push(section("Prompt",
      el("pre", { class: "prompt", text: m.prompt || "—" }),
      el("div", { class: "row" },
        el("button", { text: "Copy prompt", onclick: () => copyText(m.prompt) }),
        el("button", { text: "Copy all", onclick: () => copyText(rawParameters(m)) }))));
    if (m.negative) parts.push(section("Negative prompt", el("pre", { class: "prompt", text: m.negative })));
    const rows = [["Mode", m.mode || "—"]];
    for (const [key, label] of SHOWN) if (m.params[key] != null) rows.push([label, m.params[key]]);
    parts.push(section("Settings", table(rows)));
    if (m.loras.length) {
      parts.push(section("LoRAs", table(m.loras.map((l) => [l.name, l.weight == null ? "—" : String(l.weight)]))));
    }
    const shownKeys = new Set(SHOWN.map(([k]) => k));
    const rest = Object.entries(m.params).filter(([k]) => !shownKeys.has(k));
    if (rest.length) parts.push(section("Other fields", table(rest)));
  } else {
    parts.push(section("Parameters", el("p", { class: "hint", text: "No generation parameters found in this file." })));
  }
  const folders = img.folders.map((fid) => el("button", {
    class: "chip", text: folderPath(fid), onclick: () => { closeViewer(); setSource(`folder:${fid}`); },
  }));
  parts.push(section("Folders", folders.length ? el("div", { class: "chips" }, ...folders)
    : el("p", { class: "hint", text: "Not in any folder." })));
  parts.push(section("File", table([
    ["Path", img.relpath],
    ["Pixels", img.width ? `${img.width} × ${img.height}` : "—"],
    ["Bytes", (img.size / 1024 / 1024).toFixed(2) + " MB"],
    ["Modified", new Date(img.mtime * 1000).toLocaleString()],
    ...(img.error ? [["Error", img.error]] : []),
  ]), el("a", { href: `file/${img.id}`, target: "_blank", rel: "noopener", text: "Open original" })));
  box.replaceChildren(...parts);
}

function rawParameters(m) {
  const fields = Object.entries(m.params)
    .map(([k, v]) => `${k}: ${/[,:"]/.test(v) ? JSON.stringify(v) : v}`).join(", ");
  return [m.prompt, m.negative ? `Negative prompt: ${m.negative}` : null, fields].filter(Boolean).join("\n");
}

function table(rows) {
  return el("table", {}, ...rows.map(([k, v]) => el("tr", {}, el("th", { text: k }), el("td", { text: v }))));
}

function wireViewer() {
  $("#prev").onclick = () => step(-1);
  $("#next").onclick = () => step(1);
  $("#close").onclick = closeViewer;
  $("#toggle-info").onclick = toggleInfo;
  $("#stage").addEventListener("click", (e) => {
    if (e.target.id === "stage" && !zoom.panned) closeViewer();
  });
  wireZoom();
}

function toggleInfo() {
  state.infoOpen = !state.infoOpen;
  $("#viewer").classList.toggle("no-info", !state.infoOpen);
  applyZoom();
}

// ---- zoom & pan ---------------------------------------------------------

// Scale is relative to the fitted image. The slider is logarithmic so each
// notch feels like the same step at every magnification.
const ZOOM_MAX = 8;
const zoom = { s: 1, x: 0, y: 0, drag: null, panned: false };

function applyZoom() {
  const img = $("#full");
  const stage = $("#stage");
  const w = img.offsetWidth;
  const h = img.offsetHeight;
  const mx = Math.max(0, (w * zoom.s - stage.clientWidth) / 2);
  const my = Math.max(0, (h * zoom.s - stage.clientHeight) / 2);
  zoom.x = Math.min(mx, Math.max(-mx, zoom.x));
  zoom.y = Math.min(my, Math.max(-my, zoom.y));
  img.style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  $("#zoom-range").value = Math.round((Math.log(zoom.s) / Math.log(ZOOM_MAX)) * 100);
  $("#zoom-level").textContent = img.naturalWidth && w ? `${Math.round((w * zoom.s * 100) / img.naturalWidth)}%` : "";
  stage.classList.toggle("zoomed", zoom.s > 1.001);
}

// Zoom to scale s, keeping the image point under (clientX, clientY) fixed.
function setZoom(s, clientX, clientY) {
  const r = $("#stage").getBoundingClientRect();
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  const qx = (clientX ?? cx) - cx;
  const qy = (clientY ?? cy) - cy;
  s = Math.min(ZOOM_MAX, Math.max(1, s));
  zoom.x = qx - ((qx - zoom.x) * s) / zoom.s;
  zoom.y = qy - ((qy - zoom.y) * s) / zoom.s;
  zoom.s = s;
  applyZoom();
}

function resetZoom() {
  zoom.s = 1;
  zoom.x = zoom.y = 0;
  applyZoom();
}

function toggleActualPixels() {
  const img = $("#full");
  if (zoom.s > 1.001 || !img.offsetWidth) return resetZoom();
  setZoom(Math.max(2, img.naturalWidth / img.offsetWidth));
}

function wireZoom() {
  const img = $("#full");
  const range = $("#zoom-range");
  range.addEventListener("input", () => setZoom(ZOOM_MAX ** (range.value / 100)));
  range.addEventListener("change", () => range.blur());
  $("#zoom-in").onclick = () => setZoom(zoom.s * 1.25);
  $("#zoom-out").onclick = () => setZoom(zoom.s / 1.25);
  $("#zoom-level").onclick = toggleActualPixels;
  img.addEventListener("load", applyZoom);
  window.addEventListener("resize", applyZoom);
  $("#stage").addEventListener("wheel", (e) => {
    e.preventDefault();
    setZoom(zoom.s * Math.exp(-e.deltaY * 0.002), e.clientX, e.clientY);
  }, { passive: false });
  img.addEventListener("pointerdown", (e) => {
    zoom.panned = false;
    if (zoom.s <= 1.001 || e.button !== 0) return;
    e.preventDefault();
    img.setPointerCapture(e.pointerId);
    img.classList.add("panning");
    zoom.drag = { px: e.clientX, py: e.clientY, x: zoom.x, y: zoom.y };
  });
  img.addEventListener("pointermove", (e) => {
    if (!zoom.drag) return;
    const dx = e.clientX - zoom.drag.px;
    const dy = e.clientY - zoom.drag.py;
    if (Math.abs(dx) + Math.abs(dy) > 3) zoom.panned = true;
    zoom.x = zoom.drag.x + dx;
    zoom.y = zoom.drag.y + dy;
    applyZoom();
  });
  const endPan = () => {
    zoom.drag = null;
    img.classList.remove("panning");
  };
  img.addEventListener("pointerup", endPan);
  img.addEventListener("pointercancel", endPan);
}

// ---- keyboard -----------------------------------------------------------

function typing(e) {
  const t = e.target;
  const text = t && ((t.tagName === "INPUT" && t.type !== "range") || t.tagName === "TEXTAREA" || t.isContentEditable);
  return text || $("#picker").open;
}

document.addEventListener("keydown", async (e) => {
  if (typing(e)) return;
  const mod = e.ctrlKey || e.metaKey;
  const viewing = !$("#viewer").hidden;
  if (viewing) {
    if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    else if (e.key === "Escape") closeViewer();
    else if (e.key === "i") toggleInfo();
    else if (e.key === "+" || e.key === "=") setZoom(zoom.s * 1.25);
    else if (e.key === "-") setZoom(zoom.s / 1.25);
    else if (e.key === "0") resetZoom();
    else if (e.key === "Delete" && !state.source.startsWith("trash")) {
      const it = state.items[state.viewerIndex];
      await guarded(() => trashSelected([it.id]));
      if (state.viewerIndex >= state.items.length) state.viewerIndex = state.items.length - 1;
      showCurrent();
    }
    return;
  }
  if (mod && e.key.toLowerCase() === "a") {
    e.preventDefault();
    await loadAll();
    for (const it of state.items) state.selected.add(it.id);
    syncSelection();
  } else if (mod && e.key.toLowerCase() === "c") {
    if (!state.selected.size) return;
    state.clipboard = [...state.selected];
    toast(`Copied ${state.clipboard.length} image(s) — open a folder and paste`);
  } else if (mod && e.key.toLowerCase() === "v") {
    const [kind, arg] = splitSource(state.source);
    if (!state.clipboard.length) return;
    if (kind !== "folder") return toast("Open one of your folders to paste into it");
    await guarded(async () => {
      await addTo(Number(arg), state.clipboard);
      setSource(state.source);
    });
  } else if (e.key === "Delete" && state.selected.size && !state.source.startsWith("trash")) {
    await guarded(() => trashSelected([...state.selected]));
  } else if (e.key === "Escape" && state.selected.size) {
    state.selected.clear();
    syncSelection();
  }
});

// ---- boot ---------------------------------------------------------------

async function poll() {
  const t = await refreshTree().catch(() => null);
  if (!t) return;
  const total = sourceTotal(t);
  const idle = $("#viewer").hidden && !state.selected.size && document.querySelector("main").scrollTop < 200;
  if (total !== state.total && idle && !state.loading) setSource(state.source);
}

wireToolbar();
wireViewer();
new IntersectionObserver((entries) => {
  if (entries.some((x) => x.isIntersecting)) loadMore();
}, { root: document.querySelector("main"), rootMargin: "800px" }).observe($("#sentinel"));
refreshTree().then(() => setSource("dir:")).catch((e) => toast(e.message));
setInterval(poll, 30000);
