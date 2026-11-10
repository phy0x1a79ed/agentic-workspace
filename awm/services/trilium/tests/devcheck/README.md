# Dev-server check

Drives the Control panel and the bot button on the fork's dev server (`projects/trilium`, on a copy of a
vault snapshot, never the live vault) against a stub of alcor. Not part of the pytest run.

1. Copy a snapshot into the dev scratch dir, add `[General] noAuthentication=true` to its
   `config.ini`, and run `apps/server/scripts/dev-server.sh --port 37750` in the fork.
2. `alcor_stub.py` serves alcor's API and modals on 127.0.0.1:37751 and logs each call.
3. `seed.py` (env `awm`, `AWM_VAULT_PORT=37750`, `AWM_TRILIUM_ALCOR_ORIGIN=http://127.0.0.1:37751`) installs the pages, runs the layout migration and
   drains synthetic cards and a roster into the vault.
4. `browse.py` (env `awm-queue`, which has playwright) opens the pages and asserts what it sees.
