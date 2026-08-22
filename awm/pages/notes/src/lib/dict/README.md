# Vendored Hunspell dictionary

`en.aff` / `en.dic` are the English (US) Hunspell dictionary, vendored verbatim
from the `dictionary-en` npm package (a devDependency, kept only as the source
of these files — it is **not** imported at runtime: its `index.js` reads the
files via Node `fs`, and its `"exports": "./index.js"` blocks subpath `?url`
imports, so we ship the raw files here instead).

They are loaded lazily by `../spellcheck.ts` via Vite `?url` + `fetch`, then fed
to `nspell`. To refresh after a `dictionary-en` bump:

    cp node_modules/dictionary-en/index.aff pages/notes/src/lib/dict/en.aff
    cp node_modules/dictionary-en/index.dic pages/notes/src/lib/dict/en.dic

License: the `dictionary-en` package is `(MIT AND BSD-3-Clause)`; see its
`node_modules/dictionary-en/license`.
