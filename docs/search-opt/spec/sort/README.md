# sort

## Purpose & Contents

This file holds how a query orders each source's results.

## As built

The signature orders a section by meaning when `text` holds an `$about`, and newest first otherwise. Newest first assumes a `ts` field, which a source no longer has to carry (`sources`).

## Decisions

- 2026-11-05: Sort is per source, because results return per source. A source with no sort returns its results unsorted, and the caller does not care about their order. A sort names one field with `+` or `-`. On a `content` field it orders by match score or relevance. On any other field it orders alphabetically, numerically or by time. Tony: "Results are returned per source So sort is per source. If not specified, just return as is without sorting (implying that order doesnt matter to the calling agent) So sort_by names a field with +/- as suggested. For content, +/- means match score or relevance, others are alphabetical or numeric order."
- 2026-11-05: A source's sort is the reserved key `$sort` in that source's filter object, such as `"journals": {"project": "awm", "$sort": "-ts"}`. The `$` prefix cannot collide with a field name. The description gains one line in its style: `$sort in F: one field, +/-. On content, by match score.` Tony: "Yes and accept reseeved $sort".
