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


def landing_page(services: list[dict[str, Any]]) -> str:
    """Dynamic index of the registered ``/ui/*`` pages."""
    pages = [
        s for s in services
        if s.get("kind") in ("page", "static", "url")
        and str(s.get("prefix", "")).startswith("/ui/")
    ]
    pages.sort(key=lambda s: str(s.get("name", "")))
    if pages:
        items = "\n".join(
            f'    <li><a href="{html.escape(str(s["prefix"]).rstrip("/") + "/")}">'
            f'{html.escape(str(s.get("name", s["prefix"])))}</a></li>'
            for s in pages
        )
        body = f'  <ul class="svc">\n{items}\n  </ul>'
    else:
        body = '  <p class="muted">No pages are registered.</p>'
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
<p class="muted">Registered pages on this node:</p>
{body}
</body></html>"""
