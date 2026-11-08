# Port plan: awm-pycompat

A shared crate for the Python byte formats the ports must reproduce exactly:
- `isoformat()` of an aware UTC datetime, with no fraction when microseconds are 0, and the inputs `fromisoformat` accepts on 3.12.
- A `json.dumps` writer with default separators, `ensure_ascii` and insertion order.
- `repr` of str, bool, None, numbers, list, tuple and dict.
- difflib `SequenceMatcher` with autojunk, `unified_diff` and `ratio`.
- `str` rules: `splitlines` on every Python separator, `strip`/`split` treating `\x1c`-`\x1f` as whitespace, `\w` as `isalnum()` plus `_`, code-point slicing, and a hand-written sentence splitter in place of the `(?<=[.!?])\s+` lookbehind.

Tests: each function equals the T2 fixtures byte for byte, including a Unicode corpus (separators, combining marks, CJK, emoji, Turkish dotted I, ligatures).
