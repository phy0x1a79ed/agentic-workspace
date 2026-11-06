# shogun-singletons: which shared services shogun calls

Raised by fleet-endstate (daae3825): after its awm_config render, shogun calls no singleton.

> shogun runs core + operations + 2fa + socials

> core = relay + search + files (all nodes)

## Design

shogun is a caller of operations, 2fa and social on alcor, and of relay on deneb. It runs search and the file viewer itself. It calls no browser, ibkr, massive, trilium or zotero. fleet-endstate applies this in cloud's `edges.yml`.
