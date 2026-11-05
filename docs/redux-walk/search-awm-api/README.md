# search-awm-api

## Purpose & Contents

This topic holds the search source for the awm API itself.

## Tony's words

> can we make it so search also retrieves information about the awm API itself (as a source) agents seem to use it for that as well

> search: just the tools is fine (with their descriptions), then I guess `get` will work like (or maybe be syntactic sugar for) `describe`

## Result

1. Search gains a source `awm`: one document per tool and verb on each node, with the verb's description and its params. It comes from the node's live tool table and refreshes when the table changes.
2. The source holds the tools only. The workspace docs stay out.
3. `get` on an `awm` hit returns what `<tool>(verb="describe", args={verbs:[<verb>]})` returns, ideally by calling describe.
