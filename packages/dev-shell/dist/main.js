// dev-shell main bundle (vanilla ESM). Lists registered stripes from
// /hub/stripes, mounts the selected one in an iframe at its prefix.
// Same-origin to the hub, so the hub's session cookie applies — no
// auth dance.

const listEl = document.getElementById("stripe-list");
const frameEl = document.getElementById("frame");
const emptyEl = document.getElementById("empty");
const refreshEl = document.getElementById("refresh");

// Dev-shell IS one of the stripes. Skip itself in the list so the iframe
// can't recurse. Read its own prefix from window.location so a custom
// `--prefix` registration still works.
const selfPrefix = window.location.pathname.replace(/\/$/, "").replace(/\/[^/]*$/, "")
  || window.location.pathname.replace(/\/$/, "");

let currentName = null;

async function fetchStripes() {
  const resp = await fetch("/hub/stripes", { credentials: "include" });
  if (!resp.ok) {
    throw new Error(`/hub/stripes ${resp.status}: ${await resp.text()}`);
  }
  return (await resp.json()).stripes ?? {};
}

function render(stripes) {
  listEl.innerHTML = "";
  const entries = Object.entries(stripes)
    .filter(([_, s]) => s.prefix !== selfPrefix)
    .sort(([a], [b]) => a.localeCompare(b));
  if (entries.length === 0) {
    const li = document.createElement("li");
    li.className = "placeholder";
    li.textContent = "no stripes registered";
    listEl.appendChild(li);
    return;
  }
  for (const [name, info] of entries) {
    const li = document.createElement("li");
    li.dataset.name = name;
    if (name === currentName) li.classList.add("active");
    const nm = document.createElement("span");
    nm.className = "name";
    nm.textContent = name;
    const st = document.createElement("span");
    st.className = `status ${info.status}`;
    st.textContent = info.status;
    li.appendChild(nm);
    li.appendChild(st);
    li.addEventListener("click", () => mount(name, info));
    listEl.appendChild(li);
  }
}

function mount(name, info) {
  currentName = name;
  for (const li of listEl.children) {
    li.classList.toggle("active", li.dataset.name === name);
  }
  emptyEl.hidden = true;
  frameEl.hidden = false;
  // Trailing slash so the stripe's static index.html is the canonical landing.
  const url = info.prefix.endsWith("/") ? info.prefix : info.prefix + "/";
  if (frameEl.src.endsWith(url)) return;   // already mounted
  frameEl.src = url;
}

async function reload() {
  try {
    const stripes = await fetchStripes();
    render(stripes);
  } catch (err) {
    listEl.innerHTML = "";
    const li = document.createElement("li");
    li.className = "placeholder";
    li.textContent = String(err);
    listEl.appendChild(li);
  }
}

refreshEl.addEventListener("click", reload);
reload();
