"""awm.fileviewer — view a file in the browser by its absolute path.

Point a URL at any file the awm user can read
(``http://127.0.0.1:<port>/?path=/abs/path/thing.svg``) and the browser renders
it natively: SVG draws, HTML renders, PNG/JPEG display, ``.py``/``.md``/``.json``
show as readable text — each served with a correct ``Content-Type``. A missing,
directory, or unreadable path returns a small styled 404 not-found page.

Like ``mic``, the transport does **not** ride the awm hub: the hub function
channel is JSON-only and can't hand a browser raw bytes with a real
``Content-Type``, so the file bytes ride a self-contained loopback HTTP listener
(``server``) launched in a daemon thread from the adapter's ``on_start``. The
gateway registration buys **supervision + a status surface** (``hub_adapter``),
nothing more — the listener's lifetime is the gateway lease.
"""
