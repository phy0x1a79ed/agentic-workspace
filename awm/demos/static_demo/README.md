# static_demo — smoke test for `kind="static"` hub registrations

Counterpart to `awm/demos/echo_svc.py`. Where the echo demo proves
URL-forwarding works, this proves directory-serving + the auto-generated
ESM shell work.

The bundle is deliberately naked: a `main.js` ESM module + a `style.css`,
no `index.html`. The hub renders the shell so the prefix root is a viewable
page.

## Use

```bash
# Terminal A: run the hub
awm serve-exposed

# Terminal B: register this directory at /comp-demo
awm hub register \
  --name comp-demo \
  --prefix /comp-demo \
  --dir awm/demos/static_demo \
  --entry main.js \
  --css style.css

# Terminal C: verify
curl -sk https://127.0.0.1:7820/comp-demo/                  # auto-shell HTML
curl -sk https://127.0.0.1:7820/comp-demo/main.js           # bundle JS
curl -sk https://127.0.0.1:7820/comp-demo/style.css         # bundle CSS
awm hub list                                                 # kind: static
```

Open `https://127.0.0.1:7820/comp-demo/` in a browser to confirm the
component mounts against `<div id="app">`.

Drop an `index.html` into this directory to override the auto-shell —
the hub serves whatever file you put there verbatim.
