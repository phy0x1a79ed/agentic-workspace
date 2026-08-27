"""Self-contained HTML the edge serves itself (never proxied): the sign-in
screen shown to an unauthenticated browser, the landing page shown at ``/``
after sign-in, and the "not answering yet" page the vault gets while it starts.

All three are inline + dependency-free on purpose: the security edge must not
depend on the frontend build being present or on any external asset (a strict
rule for the one authenticated door into AWM). The landing page is *dynamic* —
it lists the live ``/ui/*`` pages the gateway currently has registered.

They share one palette, taken from Trilium rather than invented — see
:data:`_TOKENS`. On the public host the vault *is* the product, so a door drawn
in some other designer's colours would announce the seam this edge exists to
remove.
"""

from __future__ import annotations

import html
import json
from typing import Any

# Trilium's own theme, lifted from the build we serve — `theme-next-light.css`,
# `theme-next-dark.css`, `theme-next/forms.css` and `setup.css`. The edge and
# the knowledge base behind it are one product to the person signing in, so the
# door is drawn from the room's palette rather than from a second one.
#
# Two things the edge cannot do that Trilium can. It cannot read the theme
# setting, which lives inside the vault and does not exist before sign-in, so
# `prefers-color-scheme` is the only signal there is. And it cannot fetch
# Inter, which is bundled inside Trilium and unreachable from here — named
# first so an installed copy is used, with the system stack behind it.
_TOKENS = """
  :root {
    color-scheme: light dark;
    --main-font-family: "Inter", ui-sans-serif, system-ui, -apple-system,
        "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --main-background-color: white;
    --left-pane-background-color: #f2f2f2;
    --main-text-color: black;
    --muted-text-color: #666;
    --main-border-color: #dbdbdb;
    --input-background-color: #00000012;
    --input-text-color: black;
    --input-placeholder-color: #06060682;
    --input-hover-background: #00000020;
    --input-focus-background: #ffffff80;
    --input-focus-outline-color: #00000063;
    --cmd-button-background-color: #0000000f;
    --cmd-button-text-color: #000000ad;
    --cmd-button-hover-background-color: #00000016;
    --cmd-button-shadow-color: #00000040;
    --link-color: #0076af;
    --error-color: #c0392b;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --main-background-color: #242424;
      --left-pane-background-color: #1f1f1f;
      --main-text-color: #ccc;
      --muted-text-color: #bbb;
      --main-border-color: #454545;
      --input-background-color: #ffffff12;
      --input-text-color: #ffffffc7;
      --input-placeholder-color: #b7b7b782;
      --input-hover-background: #ffffff1f;
      --input-focus-background: #ffffff1f;
      --input-focus-outline-color: #ffffff57;
      --cmd-button-background-color: #ffffff28;
      --cmd-button-text-color: #ffffffc2;
      --cmd-button-hover-background-color: #ffffff37;
      --cmd-button-shadow-color: #00000080;
      --link-color: #95c3d9;
      --error-color: #e88;
    }
  }
  body { font-family: var(--main-font-family); color: var(--main-text-color);
         line-height: 1.55; }
  a { color: var(--link-color); }
  .muted { color: var(--muted-text-color); font-size: 0.92rem; }
"""

# The door: Trilium's setup screen, which is a card floating on three radial
# gradients. Under 700px the gradients drop and the card goes full-bleed —
# upstream's own breakpoint, so a phone gets a page rather than a letterbox.
_DOOR = """
  html, body { height: 100%; }
  body { margin: 0; background: var(--left-pane-background-color); }
  .backdrop { min-height: 100dvh; display: flex; align-items: center;
         justify-content: center; }
  .card { background: var(--main-background-color); padding: 2em;
         box-sizing: border-box; width: 100%; align-self: stretch;
         display: flex; flex-direction: column; gap: 1rem; }
  .card h1 { font-size: 1.4rem; margin: 0; }
  .card > header p { margin: 0.25rem 0 0; }
  form { display: flex; flex-direction: column; gap: 0.35rem; margin: 0; }
  label { font-size: 0.85rem; color: var(--muted-text-color); }
  input + label { margin-top: 0.6rem; }
  input { font: inherit; font-size: 1rem; padding: 10px 12px; border: unset;
         border-radius: 8px; background: var(--input-background-color);
         color: var(--input-text-color); outline: 3px solid transparent;
         outline-offset: 6px; }
  input::placeholder { color: var(--input-placeholder-color); }
  input:hover { background: var(--input-hover-background); }
  input:focus { outline: 3px solid var(--input-focus-outline-color);
         outline-offset: 0; background: var(--input-focus-background);
         transition: outline-color 50ms linear, outline-offset 200ms ease-out; }
  /* Reserved height: a failed attempt must not move the button out from under
     the pointer that just pressed it. */
  .err { color: var(--error-color); font-size: 0.85rem; min-height: 1.2em; }
  form footer { border-top: 1px solid var(--main-border-color);
         margin: 0.25rem -2em 0; padding: 1rem 2em 0; }
  button { font: inherit; width: 100%; padding: 8px 16px; border: unset;
         border-radius: 6px; background: var(--cmd-button-background-color);
         color: var(--cmd-button-text-color); cursor: pointer;
         box-shadow: 1px 1px 1px var(--cmd-button-shadow-color); }
  button:hover { background: var(--cmd-button-hover-background-color); }
  button:active { transform: scale(0.95); box-shadow: unset; }
  button:disabled { opacity: 0.5; cursor: default; transform: none; }
  .note { font-size: 0.85rem; color: var(--muted-text-color); margin: 0; }

  /* Upstream's own breakpoint. Below it the gradients drop and the card goes
     full-bleed, so a phone gets a page rather than a letterbox. These override
     the rules above, so they have to come after them. */
  @media (min-width: 700px) {
    .backdrop {
      padding: 2em; box-sizing: border-box;
      background:
        radial-gradient(ellipse at 20% 50%, rgba(99, 102, 241, 0.3) 0%, transparent 50%),
        radial-gradient(ellipse at 80% 20%, rgba(168, 85, 247, 0.25) 0%, transparent 50%),
        radial-gradient(ellipse at 60% 80%, rgba(59, 130, 246, 0.25) 0%, transparent 50%),
        var(--left-pane-background-color);
    }
    /* Narrower than the setup wizard's 750px: that frame is sized for a
       five-option chooser, and two fields would look lost in it. */
    .card { width: min(420px, 100%); align-self: auto; border-radius: 16px;
           box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1); }
  }
"""

_STYLE = _TOKENS + """
  body { max-width: 560px; margin: 64px auto; padding: 0 24px;
         background: var(--main-background-color); }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  ul.svc { list-style: none; padding: 0; margin-top: 24px; }
  ul.svc li { margin: 0; }
  ul.svc a { display: block; padding: 12px 14px; margin-top: 8px;
         border: 1px solid var(--main-border-color); border-radius: 8px;
         text-decoration: none; color: inherit; }
  ul.svc a:hover { border-color: var(--link-color); }
  .top { display: flex; justify-content: space-between; align-items: baseline; }
  a.logout { font-size: 0.85rem; }

  .filterbar { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 20px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px;
         border: 1px solid var(--link-color); border-radius: 999px;
         font-size: 0.82rem;
         background: color-mix(in srgb, var(--link-color) 12%, transparent); }
  .chip button { padding: 0; margin: 0; width: 16px; height: 16px; line-height: 1;
         font-size: 0.85rem; border-radius: 50%; background: transparent;
         color: inherit; opacity: 0.6; border: 0; cursor: pointer; }
  .chip button:hover { opacity: 1;
         background: color-mix(in srgb, var(--link-color) 20%, transparent); }
  .filteradd { margin-top: 8px; display: flex; align-items: center; gap: 8px; }
  select#addFilter { padding: 6px 8px; font-size: 0.85rem; border-radius: 6px;
         border: 1px solid var(--main-border-color);
         background: var(--main-background-color); color: var(--main-text-color); }
  select#addFilter option { background: var(--main-background-color);
         color: var(--main-text-color); }
  .togglechip { display: inline-flex; align-items: center; padding: 5px 10px;
         font-size: 0.82rem; border: 1px solid var(--main-border-color);
         border-radius: 999px; background: transparent; color: inherit;
         cursor: pointer; }
  .togglechip:hover { border-color: var(--link-color); }
  .togglechip.active { border-color: var(--link-color);
         background: var(--link-color); color: var(--main-background-color); }
  .status { margin-top: 6px; font-size: 0.8rem; color: var(--muted-text-color); }

  .cards { list-style: none; padding: 0; margin-top: 20px; display: flex;
         flex-direction: column; gap: 6px; }
  .card { border: 1px solid var(--main-border-color); border-radius: 8px;
         padding: 8px 10px; position: relative; }
  .card.hidden { display: none; }
  .card a.pagelink { text-decoration: none; color: inherit; font-weight: 500; }
  .card a.pagelink:hover { color: var(--link-color); }
  .card a.pagelink::before { content: ""; position: absolute; inset: 0; }
  .cardfoot { display: flex; justify-content: flex-end; align-items: center;
         gap: 6px; margin-top: 4px; position: relative; z-index: 1;
         pointer-events: none; }
  .cardtags { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }
  .tagchip { display: inline-flex; align-items: center; gap: 4px; font-size: 0.76rem;
         padding: 2px 4px 2px 7px; border-radius: 999px;
         border: 1px solid var(--main-border-color);
         opacity: 0.85; pointer-events: auto; }
  .tagchip button { padding: 0; margin: 0; width: 13px; height: 13px; line-height: 1;
         font-size: 0.75rem; border-radius: 50%; background: transparent;
         color: inherit; opacity: 0.6; border: 0; cursor: pointer; }
  .tagchip button:hover { opacity: 1; background: var(--input-hover-background); }
  .dots { padding: 2px 6px; background: transparent; color: inherit; opacity: 0.55;
         font-size: 0.95rem; border-radius: 6px; pointer-events: auto;
         border: 0; cursor: pointer; }
  .dots:hover { opacity: 1; background: var(--input-background-color); }
  .tagmenu { display: none; position: absolute; right: 12px; bottom: 44px;
         background: var(--main-background-color); color: var(--main-text-color);
         border: 1px solid var(--main-border-color);
         border-radius: 8px; padding: 10px; min-width: 180px; box-shadow:
         0 4px 16px rgba(0, 0, 0, 0.25); z-index: 10; pointer-events: auto; }
  .tagmenu.open { display: block; }
  .tagmenu .opt { display: block; width: 100%; text-align: left; padding: 6px 8px;
         background: transparent; color: inherit; border: 0; border-radius: 6px;
         font-size: 0.85rem; cursor: pointer; }
  .tagmenu .opt:hover {
         background: color-mix(in srgb, var(--link-color) 12%, transparent); }
  .tagmenu input[type=text] { width: 100%; margin-top: 8px; padding: 6px 8px;
         font-size: 0.85rem; border: 1px solid var(--main-border-color);
         border-radius: 6px; background: transparent; color: inherit;
         box-sizing: border-box; }
  .tagmenu .addbtn { margin-top: 6px; width: 100%; padding: 6px;
         font-size: 0.82rem; border: 0; border-radius: 6px;
         background: var(--cmd-button-background-color);
         color: var(--cmd-button-text-color); cursor: pointer; }
"""


def login_page(public: bool = False, name: str = "awm") -> str:
    """The sign-in screen: a modal in Trilium's theme, on Trilium's backdrop.

    It is drawn as the knowledge base behind it is drawn because it is the same
    product to the person in front of it — the door and the room should not
    look like two different builds. Everything is inline and dependency-free,
    which is not a style choice: the one authenticated door into awm must not
    need the frontend build, or any external asset, to render itself.

    This screen covers *every* service, not only the vault. The gate is in the
    catch-all proxy, before anything is forwarded, so any path that would answer
    HTML to an unauthenticated browser answers this instead. It cannot appear
    *over* the page it is guarding — the edge never renders a page it has
    refused to serve — which is why the card sits on a backdrop of its own.

    ``public`` drops the shared-password hint and the CA link: on the public
    host there is no shared password and a real certificate. ``name`` is the
    label at the top, so a person can see which node they are signing in to.
    """
    title = html.escape(name)
    if public:
        extras = ""
    else:
        extras = """<p class="note">Leave the username blank to use the current day's
shared password. Get it on the daemon host with <code>awm auth password</code>;
it is also posted to Discord <code>#notifications</code> when minted.</p>
<p class="note">A new device has to trust this node's CA once, or the browser
blocks pages and sockets alike: <a href="/ca.crt">install the certificate</a>.
(Served unauthenticated on purpose &mdash; a device that doesn't trust us yet
can't sign in to fetch it.)</p>"""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title} &mdash; sign in</title>
<style>{_TOKENS}{_DOOR}</style>
</head><body>
<div class="backdrop">
<main class="card" role="dialog" aria-labelledby="t" aria-modal="true">
  <header>
    <h1 id="t">{title}</h1>
    <p class="muted">Sign in to continue.</p>
  </header>
  <form id="f" autocomplete="on">
    <label for="u">Username</label>
    <input id="u" name="username" type="text" autofocus
           autocomplete="username" autocapitalize="none" />
    <label for="p">Password</label>
    <input id="p" name="password" type="password"
           autocomplete="current-password" />
    <div class="err" id="e" role="alert"></div>
    <footer><button id="b" type="submit">Sign in</button></footer>
  </form>
  {extras}
</main>
</div>
<script>
const f=document.getElementById('f'),u=document.getElementById('u'),
      p=document.getElementById('p'),b=document.getElementById('b'),
      e=document.getElementById('e');
f.addEventListener('submit',async(ev)=>{{
  ev.preventDefault(); e.textContent=''; b.disabled=true;
  try {{
    const r=await fetch('/__auth/login',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{username:u.value.trim(),password:p.value}})}});
    if(r.ok){{ location.replace(location.pathname==='/'?'/':location.href); return; }}
    if(r.status===429){{
      let s=60; try{{ s=(await r.json()).retry_after||s; }}catch(_){{}}
      e.textContent='Too many attempts. Try again in '+Math.ceil(s/60)+' min.';
    }} else e.textContent = r.status===401 ? 'Incorrect username or password.' : ('Error '+r.status);
  }} catch(_){{ e.textContent='Network error.'; }}
  b.disabled=false; p.select();
}});
</script>
</body></html>"""


def landing_page(
    services: list[dict[str, Any]],
    tags_by_page: dict[str, list[str]] | None = None,
    tag_counts: dict[str, int] | None = None,
    selected_tags: list[str] | None = None,
    peer_name: str = "awm",
    display_names: dict[str, str] | None = None,
) -> str:
    """Dynamic index of the registered ``/ui/*`` pages, taggable and
    filterable by tag. ``tags_by_page``/``tag_counts``/``selected_tags``/
    ``display_names`` come from ``store.LandingDAO`` and are persisted
    server-side. ``display_names`` is a purely cosmetic label override keyed
    by the same technical ``name`` used for tags — it never affects a card's
    ``href`` or ``data-page`` identity."""
    tags_by_page = tags_by_page or {}
    tag_counts = tag_counts or {}
    selected_tags = selected_tags or []
    display_names = display_names or {}

    pages = [
        s for s in services
        if s.get("kind") in ("page", "static", "url")
        and str(s.get("prefix", "")).startswith("/ui/")
    ]
    def _label(s: dict[str, Any]) -> str:
        name = str(s.get("name", s.get("prefix", "")))
        return display_names.get(name) or name

    pages.sort(key=_label)

    if pages:
        cards = []
        for s in pages:
            name = str(s.get("name", s.get("prefix", "")))
            label = display_names.get(name) or name
            href = html.escape(str(s["prefix"]).rstrip("/") + "/")
            page_tags = tags_by_page.get(name, [])
            data_tags = html.escape(",".join(page_tags))
            tag_chips = "\n".join(
                f'      <span class="tagchip">{html.escape(t)}'
                f'<button type="button" onclick="removeTag(\'{html.escape(name, quote=True)}\',\'{html.escape(t, quote=True)}\')">&times;</button></span>'
                for t in page_tags
            )
            cards.append(f"""  <li class="card" data-page="{html.escape(name)}" data-tags="{data_tags}">
    <a class="pagelink" href="{href}">{html.escape(label)}</a>
    <div class="cardfoot">
      <span class="cardtags">
{tag_chips}
      </span>
      <button class="dots" type="button" onclick="toggleTagMenu(this,'{html.escape(name, quote=True)}')">&#8942;</button>
      <div class="tagmenu"></div>
    </div>
  </li>""")
        body = '  <ul class="cards">\n' + "\n".join(cards) + "\n  </ul>"
    else:
        body = '  <p class="muted">No pages are registered.</p>'

    all_tags = sorted(tag_counts)
    filter_chips = "\n".join(
        f'  <span class="chip" data-tag="{html.escape(t, quote=True)}">'
        f'{html.escape(t)}:{tag_counts.get(t, 0)}'
        f'<button type="button" onclick="deselectFilter(\'{html.escape(t, quote=True)}\')">&times;</button></span>'
        for t in selected_tags
    )
    unselected = [t for t in all_tags if t not in selected_tags]
    filter_opts = "\n".join(
        f'    <option value="{html.escape(t, quote=True)}">{html.escape(t)}:{tag_counts.get(t, 0)}</option>'
        for t in unselected
    )

    tag_counts_json = json.dumps(tag_counts)
    selected_json = json.dumps(selected_tags)
    display_names_json = json.dumps(display_names)
    total_pages = len(pages)
    title = html.escape(peer_name)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{title}</title>
<style>{_STYLE}</style>
</head><body>
<div class="top"><h1>{title}</h1>
  <a class="logout" href="#" onclick="fetch('/__auth/logout',{{method:'POST'}}).then(()=>location.reload());return false;">sign out</a>
</div>
<div class="filterbar" id="filterbar">
{filter_chips}
</div>
<div class="filteradd">
  <select id="addFilter" onchange="if(this.value){{selectFilter(this.value);}}">
    <option value="">+ filter by tag&hellip;</option>
{filter_opts}
  </select>
  <button type="button" id="showAllToggle" class="togglechip" onclick="toggleShowAll()">show all</button>
</div>
<p class="status" id="status"></p>
{body}
<script>
const TAG_COUNTS = {tag_counts_json};
let SELECTED = {selected_json};
let DISPLAY_NAMES = {display_names_json};
let SHOW_ALL = false;
const TOTAL_PAGES = {total_pages};

function escapeHtml(s) {{
  return s.replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

function cardTags(card) {{
  const v = card.dataset.tags || '';
  return v ? v.split(',') : [];
}}

function resortCards() {{
  const list = document.querySelector('.cards');
  if (!list) return;
  const cards = Array.from(list.querySelectorAll('.card'));
  cards.sort((a, b) => a.querySelector('.pagelink').textContent
    .localeCompare(b.querySelector('.pagelink').textContent));
  cards.forEach(c => list.appendChild(c));
}}

function applyFilter() {{
  const cards = document.querySelectorAll('.card');
  let hidden = 0;
  cards.forEach(card => {{
    const tags = cardTags(card);
    const visible = SHOW_ALL || SELECTED.length === 0 || tags.some(t => SELECTED.includes(t));
    card.classList.toggle('hidden', !visible);
    if (!visible) hidden++;
  }});
  const status = document.getElementById('status');
  status.textContent = SHOW_ALL
    ? `showing all ${{TOTAL_PAGES}}`
    : `${{hidden}} of ${{TOTAL_PAGES}} hidden`;
}}

function toggleShowAll() {{
  SHOW_ALL = !SHOW_ALL;
  document.getElementById('showAllToggle').classList.toggle('active', SHOW_ALL);
  applyFilter();
}}

function renderFilterBar() {{
  const bar = document.getElementById('filterbar');
  bar.innerHTML = SELECTED.map(t => {{
    const n = TAG_COUNTS[t] || 0;
    const et = escapeHtml(t);
    return `<span class="chip" data-tag="${{et}}">${{et}}:${{n}}`
      + `<button type="button" onclick="deselectFilter('${{et.replace(/'/g,"\\\\'")}}')">&times;</button></span>`;
  }}).join('\\n');
  const sel = document.getElementById('addFilter');
  const unselected = Object.keys(TAG_COUNTS).sort().filter(t => !SELECTED.includes(t));
  sel.innerHTML = '<option value="">+ filter by tag&hellip;</option>' +
    unselected.map(t => `<option value="${{escapeHtml(t)}}">${{escapeHtml(t)}}:${{TAG_COUNTS[t]}}</option>`).join('');
}}

async function selectFilter(tag) {{
  const r = await fetch('/__landing/filter', {{method:'POST',
    headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{tag}})}});
  if (r.ok) {{
    const data = await r.json();
    SELECTED = data.selected_tags;
    renderFilterBar();
    applyFilter();
  }}
}}

async function deselectFilter(tag) {{
  const r = await fetch('/__landing/filter', {{method:'DELETE',
    headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{tag}})}});
  if (r.ok) {{
    const data = await r.json();
    SELECTED = data.selected_tags;
    renderFilterBar();
    applyFilter();
  }}
}}

function closeAllMenus(except) {{
  document.querySelectorAll('.tagmenu.open').forEach(m => {{
    if (m !== except) m.classList.remove('open');
  }});
}}

function toggleTagMenu(btn, page) {{
  const menu = btn.nextElementSibling;
  const isOpen = menu.classList.contains('open');
  closeAllMenus();
  if (isOpen) {{ menu.classList.remove('open'); return; }}
  const card = btn.closest('.card');
  const have = new Set(cardTags(card));
  const opts = Object.keys(TAG_COUNTS).sort().filter(t => !have.has(t));
  const ep = escapeHtml(page).replace(/'/g,"\\\\'");
  const currentLabel = DISPLAY_NAMES[page] || page;
  menu.innerHTML = opts.map(t =>
    `<button type="button" class="opt" onclick="addTag('${{ep}}','${{escapeHtml(t).replace(/'/g,"\\\\'")}}')">${{escapeHtml(t)}}</button>`
  ).join('') +
    `<input type="text" placeholder="new tag" onkeydown="if(event.key==='Enter'){{addTag('${{ep}}',this.value);this.value='';}}" />`
    + `<button type="button" class="addbtn" onclick="const i=this.previousElementSibling;addTag('${{ep}}',i.value);i.value='';">add</button>`
    + `<hr />`
    + `<input type="text" class="renameinput" value="${{escapeHtml(currentLabel)}}" placeholder="card name"`
    + ` onkeydown="if(event.key==='Enter'){{renameCard('${{ep}}',this.value);}}" />`
    + `<button type="button" class="addbtn" onclick="const i=this.previousElementSibling;renameCard('${{ep}}',i.value);">rename</button>`
    + `<button type="button" class="opt" onclick="renameCard('${{ep}}','')">reset to default</button>`;
  menu.classList.add('open');
}}

document.addEventListener('click', ev => {{
  if (!ev.target.closest('.tagmenu') && !ev.target.closest('.dots')) closeAllMenus();
}});

function renderCardTags(page, tags) {{
  const ep = escapeHtml(page).replace(/'/g,"\\\\'");
  return tags.map(t => {{
    const et = escapeHtml(t);
    return `<span class="tagchip">${{et}}`
      + `<button type="button" onclick="removeTag('${{ep}}','${{et.replace(/'/g,"\\\\'")}}')">&times;</button></span>`;
  }}).join('');
}}

function applyTagUpdate(page, data) {{
  Object.assign(TAG_COUNTS, data.tag_counts);
  Object.keys(TAG_COUNTS).forEach(k => {{ if (!(k in data.tag_counts)) delete TAG_COUNTS[k]; }});
  const card = document.querySelector(`.card[data-page="${{CSS.escape(page)}}"]`);
  if (card) {{
    card.dataset.tags = data.tags.join(',');
    card.querySelector('.cardtags').innerHTML = renderCardTags(page, data.tags);
    closeAllMenus();
  }}
  renderFilterBar();
  applyFilter();
}}

async function addTag(page, tag) {{
  tag = (tag || '').trim();
  if (!tag) return;
  const r = await fetch('/__landing/tags', {{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{page, tag}})}});
  if (!r.ok) return;
  applyTagUpdate(page, await r.json());
}}

async function removeTag(page, tag) {{
  const r = await fetch('/__landing/tags', {{method:'DELETE',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{page, tag}})}});
  if (!r.ok) return;
  applyTagUpdate(page, await r.json());
}}

async function renameCard(page, name) {{
  const r = await fetch('/__landing/name', {{method:'POST',
    headers:{{'Content-Type':'application/json'}},
    body: JSON.stringify({{page, name}})}});
  if (!r.ok) return;
  const data = await r.json();
  if (data.display_name) {{ DISPLAY_NAMES[page] = data.display_name; }}
  else {{ delete DISPLAY_NAMES[page]; }}
  const card = document.querySelector(`.card[data-page="${{CSS.escape(page)}}"]`);
  if (card) {{
    card.querySelector('.pagelink').textContent = data.display_name || page;
    closeAllMenus();
    resortCards();
  }}
}}

applyFilter();
</script>
</body></html>"""


def vault_unavailable_page(reason: str) -> str:
    """Shown when the vault is not answering yet.

    The same card on the same backdrop as the sign-in screen, because it is the
    same moment for the person: they asked for the knowledge base and got
    something that is not it. Refreshes itself, because the overwhelmingly
    common cause is a cold start — the child takes up to two minutes to bind its
    port, and the right thing for a person to do in that window is nothing.
    """
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="refresh" content="5" />
<title>Trilium &mdash; starting</title>
<style>{_TOKENS}{_DOOR}</style>
</head><body>
<div class="backdrop">
<main class="card">
  <header>
    <h1>Trilium</h1>
    <p class="muted">The knowledge base is not answering yet.</p>
  </header>
  <p class="note">{html.escape(reason)}</p>
  <p class="note">A vault that has just been started takes up to two minutes to
  come up. This page retries every five seconds.</p>
</main>
</div>
</body></html>"""
