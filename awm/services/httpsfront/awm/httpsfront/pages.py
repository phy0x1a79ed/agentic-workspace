"""Self-contained HTML the edge serves itself (never proxied): the login page
shown to an unauthenticated browser, and the landing page shown at ``/`` after
sign-in.

Both are inline + dependency-free on purpose: the security edge must not depend
on the frontend build being present or on any external asset (a strict rule for
the one authenticated door into AWM). The landing page is *dynamic* — it lists
the live ``/ui/*`` pages the gateway currently has registered.
"""

from __future__ import annotations

import html
import json
from typing import Any

_STYLE = """
  :root { color-scheme: light dark; }
  body { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI",
         Roboto, Helvetica, Arial, sans-serif; max-width: 560px;
         margin: 64px auto; padding: 0 24px; line-height: 1.55; }
  h1 { font-size: 1.4rem; margin-bottom: 0.25rem; }
  .muted { opacity: 0.7; font-size: 0.92rem; }
  form { margin-top: 24px; display: flex; gap: 8px; }
  input[type=password] { flex: 1; padding: 10px 12px; font-size: 1rem;
         border: 1px solid #8886; border-radius: 8px; background: transparent;
         color: inherit; }
  button { padding: 10px 18px; font-size: 1rem; border: 0; border-radius: 8px;
         background: #3b6fc4; color: #fff; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  .err { color: #d33; margin-top: 12px; min-height: 1.2em; font-size: 0.92rem; }
  ul.svc { list-style: none; padding: 0; margin-top: 24px; }
  ul.svc li { margin: 0; }
  ul.svc a { display: block; padding: 12px 14px; margin-top: 8px;
         border: 1px solid #8884; border-radius: 8px; text-decoration: none;
         color: inherit; }
  ul.svc a:hover { border-color: #3b6fc4; }
  .top { display: flex; justify-content: space-between; align-items: baseline; }
  a.logout { font-size: 0.85rem; }
  .recover { margin-top: 32px; font-size: 0.85rem; opacity: 0.7; }

  .filterbar { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 20px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px;
         border: 1px solid #3b6fc4; border-radius: 999px; font-size: 0.82rem;
         background: #3b6fc41a; }
  .chip button { padding: 0; margin: 0; width: 16px; height: 16px; line-height: 1;
         font-size: 0.85rem; border-radius: 50%; background: transparent;
         color: inherit; opacity: 0.6; }
  .chip button:hover { opacity: 1; background: #3b6fc433; }
  .filteradd { margin-top: 8px; }
  select#addFilter { padding: 6px 8px; font-size: 0.85rem; border-radius: 6px;
         border: 1px solid #8886; background: Canvas; color: CanvasText; }
  select#addFilter option { background: Canvas; color: CanvasText; }
  .status { margin-top: 6px; font-size: 0.8rem; opacity: 0.6; }

  .cards { list-style: none; padding: 0; margin-top: 20px; display: flex;
         flex-direction: column; gap: 8px; }
  .card { border: 1px solid #8884; border-radius: 8px; padding: 12px 14px;
         position: relative; }
  .card.hidden { display: none; }
  .card a.pagelink { text-decoration: none; color: inherit; font-weight: 500; }
  .card a.pagelink:hover { color: #3b6fc4; }
  .cardfoot { display: flex; justify-content: flex-end; align-items: center;
         gap: 6px; margin-top: 8px; }
  .cardtags { display: flex; flex-wrap: wrap; gap: 4px; justify-content: flex-end; }
  .tagchip { display: inline-flex; align-items: center; gap: 4px; font-size: 0.76rem;
         padding: 2px 4px 2px 7px; border-radius: 999px; border: 1px solid #8886;
         opacity: 0.85; }
  .tagchip button { padding: 0; margin: 0; width: 13px; height: 13px; line-height: 1;
         font-size: 0.75rem; border-radius: 50%; background: transparent;
         color: inherit; opacity: 0.6; }
  .tagchip button:hover { opacity: 1; background: #8884; }
  .dots { padding: 2px 6px; background: transparent; color: inherit; opacity: 0.55;
         font-size: 0.95rem; border-radius: 6px; }
  .dots:hover { opacity: 1; background: #8882; }
  .tagmenu { display: none; position: absolute; right: 12px; bottom: 44px;
         background: Canvas; color: CanvasText; border: 1px solid #8886;
         border-radius: 8px; padding: 10px; min-width: 180px; box-shadow:
         0 4px 16px #0004; z-index: 10; }
  .tagmenu.open { display: block; }
  .tagmenu .opt { display: block; width: 100%; text-align: left; padding: 6px 8px;
         background: transparent; color: inherit; border-radius: 6px; font-size: 0.85rem; }
  .tagmenu .opt:hover { background: #3b6fc41a; }
  .tagmenu input[type=text] { width: 100%; margin-top: 8px; padding: 6px 8px;
         font-size: 0.85rem; border: 1px solid #8886; border-radius: 6px;
         background: transparent; color: inherit; box-sizing: border-box; }
  .tagmenu .addbtn { margin-top: 6px; width: 100%; padding: 6px; font-size: 0.82rem; }
"""


def login_page() -> str:
    """Password sign-in form; POSTs JSON to ``/__auth/login`` and reloads."""
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>awm — sign in</title>
<style>{_STYLE}</style>
</head><body>
<h1>awm</h1>
<p class="muted">Enter the current day's login password. Get it on the daemon
host with <code>awm auth password</code>; it is also posted to Discord
<code>#notifications</code> when minted.</p>
<form id="f" autocomplete="off">
  <input id="p" type="password" placeholder="password" autofocus
         aria-label="password" />
  <button id="b" type="submit">Sign in</button>
</form>
<div class="err" id="e"></div>
<p class="recover">A new device has to trust this node's CA once, or the browser
blocks pages and sockets alike: <a href="/ca.crt">install the certificate</a>.
(Served unauthenticated on purpose — a device that doesn't trust us yet can't
sign in to fetch it.)</p>
<script>
const f=document.getElementById('f'),p=document.getElementById('p'),
      b=document.getElementById('b'),e=document.getElementById('e');
f.addEventListener('submit',async(ev)=>{{
  ev.preventDefault(); e.textContent=''; b.disabled=true;
  try {{
    const r=await fetch('/__auth/login',{{method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{password:p.value}})}});
    if(r.ok){{ location.replace('/'); return; }}
    e.textContent = r.status===401 ? 'Incorrect password.' : ('Error '+r.status);
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
) -> str:
    """Dynamic index of the registered ``/ui/*`` pages, taggable and
    filterable by tag. ``tags_by_page``/``tag_counts``/``selected_tags`` come
    from ``store.LandingDAO`` and are persisted server-side."""
    tags_by_page = tags_by_page or {}
    tag_counts = tag_counts or {}
    selected_tags = selected_tags or []

    pages = [
        s for s in services
        if s.get("kind") in ("page", "static")
        and str(s.get("prefix", "")).startswith("/ui/")
    ]
    pages.sort(key=lambda s: str(s.get("name", "")))

    if pages:
        cards = []
        for s in pages:
            name = str(s.get("name", s.get("prefix", "")))
            href = html.escape(str(s["prefix"]).rstrip("/") + "/")
            page_tags = tags_by_page.get(name, [])
            data_tags = html.escape(",".join(page_tags))
            tag_chips = "\n".join(
                f'      <span class="tagchip">{html.escape(t)}'
                f'<button type="button" onclick="removeTag(\'{html.escape(name, quote=True)}\',\'{html.escape(t, quote=True)}\')">&times;</button></span>'
                for t in page_tags
            )
            cards.append(f"""  <li class="card" data-page="{html.escape(name)}" data-tags="{data_tags}">
    <a class="pagelink" href="{href}">{html.escape(name)}</a>
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
    total_pages = len(pages)

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>awm</title>
<style>{_STYLE}</style>
</head><body>
<div class="top"><h1>awm</h1>
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
</div>
<p class="status" id="status"></p>
<p class="muted">Registered pages on this node:</p>
{body}
<script>
const TAG_COUNTS = {tag_counts_json};
let SELECTED = {selected_json};
const TOTAL_PAGES = {total_pages};

function escapeHtml(s) {{
  return s.replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
}}

function cardTags(card) {{
  const v = card.dataset.tags || '';
  return v ? v.split(',') : [];
}}

function applyFilter() {{
  const cards = document.querySelectorAll('.card');
  let hidden = 0;
  cards.forEach(card => {{
    const tags = cardTags(card);
    const visible = SELECTED.length === 0 || tags.some(t => SELECTED.includes(t));
    card.classList.toggle('hidden', !visible);
    if (!visible) hidden++;
  }});
  const status = document.getElementById('status');
  status.textContent = `${{hidden}} of ${{TOTAL_PAGES}} hidden`;
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
  menu.innerHTML = opts.map(t =>
    `<button type="button" class="opt" onclick="addTag('${{escapeHtml(page).replace(/'/g,"\\\\'")}}','${{escapeHtml(t).replace(/'/g,"\\\\'")}}')">${{escapeHtml(t)}}</button>`
  ).join('') +
    `<input type="text" placeholder="new tag" onkeydown="if(event.key==='Enter'){{addTag('${{escapeHtml(page).replace(/'/g,"\\\\'")}}',this.value);this.value='';}}" />`
    + `<button type="button" class="addbtn" onclick="const i=this.previousElementSibling;addTag('${{escapeHtml(page).replace(/'/g,"\\\\'")}}',i.value);i.value='';">add</button>`;
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

applyFilter();
</script>
</body></html>"""
