# kb

The paper knowledgebase as an awm service. It answers the archive's two live paper sources, `kb` and `library`, for `search`, and reads one paper whole for `search get`. It also supervises the kb server from `projects/kb`, which encodes query text and serves the meaning indexes. The tool is off the agent surface (`tier = "off"`): agents find papers through `search query`.

## Purpose & Contents

This file holds the contract between awm and the kb project: what the child runs under, which files the two sides share, what the two sources serve, and what a foreign peer may call. How the kb project builds the shared files is in `projects/kb`.

## The child

The server is `python -m kb.server` from the kb checkout, run by the `kb` env's interpreter. It answers `/encode`, `/related` and `/status`. Its dependencies (fastembed, faiss, PyMuPDF) never enter the awm env, whose torch pin is load-bearing.

- **Checkout.** `KB_CHECKOUT`, default `projects/kb/release`.
- **Interpreter.** `install.sh` builds the `kb` env from the checkout's `envs/kb/base.yml` and records its python in `kb-python`. Under systemd there is no `mamba` to find it at runtime.
- **Port.** `KB_PORT`, default 12531, on loopback. Only this service talks to it.
- **Process tree.** The server runs in its own session with `PR_SET_PDEATHSIG`. One group kill stops it, and it never outlives its parent.
- **Encoder.** The model loads only from the checkout's `live/fastembed` cache and never downloads. Until it loads, and whenever the server is down, `kb query` answers by keyword alone.

**CAUTION** The child's `PYTHONPATH` is replaced, not extended. A dev sandbox puts awm's source there, and awm code must never load in the kb interpreter.

**CAUTION** compute's `PROTECTED` names `kb.server`. The server runs in its own session, so the `awm-service` pattern does not cover it. A rename of the module must update that pattern.

## The shared files

The kb project builds these files and ships them to the state directory (`KB_STATE_DIR`, default `.awm/services/kb`). `awm/kb/instances.py` names each path. A builder in `projects/kb` and a reader here agree on each format:

| file | built by | read by |
|---|---|---|
| `papers.db` | `kb.fastlayer` | `awm.kb.fastlayer`, for both sources and `kb get` |
| `hops/` | `kb.hops`, from `kb.graph`'s `graph.db` | `awm.kb.hops`, for `kb hop` and the author filter |
| `w2/vectors/` | `kb.vectors` | the kb server, for `kb related` |

**CAUTION** Land a format change in both repositories together. A reader refuses a `papers.db` whose `meta.schema` it does not know, and the verbs that read the file then answer nothing.

`papers_delta.db` is the one file this service writes. It holds the Zotero items that `papers.db` lacks. The service polls the zotero service on `KB_ZOTERO_PEER` (default `mira`) for items that moved, and re-maps the held items each time a new base ships.

**WARNING** Never `dvc add` the delta. A pinned file is a read-only hardlink into the shared cache, and this service writes the delta while it runs.

## The archive sources

`service.toml` declares two live `[[archive]]` tables. `kb` answers through `kb_query` over every paper. `library` answers through `kb_library` over the Zotero library's papers alone. Both read a whole paper through `kb_get`. Each name is also the federation grant. Posts are not in either source: the `journal` service owns them and `search` indexes them from there.

## A foreign peer

A foreign peer holding the `kb` grant may call the `kb` read verbs (`FEDERATION.md` § *The foreign gate*), with one exception and one cut:

- `related` refuses a foreign caller. It serves this node's `search` only.
- `status` tells a foreign caller only whether the server is up.

A refusal raises `PermissionError`, which the gateway returns as the same 404 an unknown tool gets. The lifecycle verbs are operator-only and refuse every edge caller.
