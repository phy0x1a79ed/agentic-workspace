# Port plan: awm-embed

The embedder and chunker of `persistence`, used by journal, guide and search (search embeds the query only). The SQLite retrieval engine of `persistence` is not ported, because scopes no longer keeps vectors.

What must match byte for byte, or every row re-embeds:
- **Model:** `sentence-transformers/all-MiniLM-L6-v2` at revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`, stored model name `all-MiniLM-L6-v2`, dimension 384.
- **content hash:** `sha1("1\0128\0" + title\0context\0date\0body)`.
- **header:** `title.strip()[:200]`, then `context · date`, then `section[:120]`, joined by ` — `, sliced in code points.
- **Chunker:** headings, blank lines, list starts, sentence and word splits, and greedy packing, with Python `str` semantics from awm-pycompat.

What must match numerically (cosine ≥ 0.999):
- **CAUTION** Override `tokenizer.json`'s 128 cap. Encode truncated at 256 tokens and padded to the batch's longest. Count tokens uncapped, with no special tokens.
- Mean pooling over the attention mask, then L2. No prefixes. The embedded text is `header + "\n" + chunk` (the chunk alone when the header is empty). Blobs are 384 f32 LE.

Runtime: tract-onnx on `onnx/model.onnx` (sha256 `6fd5d72f…6452`). `ort` is rejected because it ships no musl build. Model files load from `AWM_DIR/models/all-MiniLM-L6-v2/<rev>/` against a sha256 table, never from an HF cache under `$HOME`. Delivery is Tony's call (*Still unsure about*).

The T6 spike measured both runtimes on mizar against 298 real texts and their stored Python vectors:

| Runtime | Min cosine | Static binary | Cold load | Texts/s |
|---|---|---|---|---|
| tract 0.23, 4 threads | 0.9999996 | 36 MB (with tokenizers) | 1.0 s | 24.7 |
| tract 0.23, 1 thread | 0.9999996 | | 1.0 s | 12.5 |
| candle 0.11 | 0.9999995 | 5.7 MB | 0.4 s | 14.6 |
| torch (Python reference) | | | 8.9 s | 66.2 |

tract stays the runtime and candle the fallback. Re-embedding altair's 16,371 journal chunks at 25 texts/s takes about 11 minutes, against 276 s in Python.

Tests: port the embedder and chunker tests of `test_embeddings.py` onto the stub embedder. Committed golden fixtures use synthetic text. The node-local parity run re-embeds scopes.db's 188 rows and the search chunk dump. The Python baseline is min 0.9999998. Without the header, min falls to 0.62, so a header slip shows at once.

journal embeds every post again at its cutover, so a cosine slightly below the gate costs ranking, not data. If the gate fails on both runtimes, ship only when search's known-item replay passes.
