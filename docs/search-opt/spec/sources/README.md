# sources

## Purpose & Contents

This file holds which sources search covers, how they relate to one mechanism, and where each one is stored.

## As built

- The signature names three kinds: `journals`, `kb` (scientific papers) and `transcripts`. Each carries a node, such as `kb@altair`.
- Journals live on each node in the journal database. search keeps its own SQLite index of them, rebuilt by an export pass.
- Papers live in kb on altair, with their own FTS5 and vector indexes. kb serves search only.
- Transcripts are a live source that search calls.
- Earlier, Tony: "papers can stay slow, daily use journals is what matters. I mean the same mechanism can prolly work for both" and "We can then use this to hold each of the sources separately."

## Decisions

