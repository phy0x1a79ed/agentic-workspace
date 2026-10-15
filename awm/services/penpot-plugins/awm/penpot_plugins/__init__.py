"""awm.penpot_plugins — hosts awm's own first-party Penpot plugins.

A pure ``kind=static`` gateway mount at ``/penpot-plugins``, serving the
``local/`` tree on disk beside this package. Every plugin under
``local/<name>/`` gets a stable install URL,
``/penpot-plugins/<name>/manifest.json``, that Penpot's in-app Plugin Manager
can paste directly — for local dev now and for sirius later, without
depending on a third-party static host (ConnectFlow, by contrast, is hosted
on Cloudflare Pages and out of our control).

This tree lives in the ``awm`` repo rather than inside the Penpot fork
specifically so it never collides with upstream's own ``plugins/`` pnpm
workspace on a future Penpot version bump — see ``INSTALL.md``.

There is nothing to supervise beyond the mount itself: this service has no
RPC verbs of its own beyond ``status``, and no database. The gateway
registration (``hub_adapter``) buys **supervision + a status surface**;
``mount`` owns the actual ``kind=static`` registration and its lease, the
same split ``fileviewer`` and ``drawio`` use for their own static mounts.
"""
