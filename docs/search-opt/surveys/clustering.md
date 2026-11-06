# clustering: survey of incremental IVF maintenance

## Purpose & Contents

This file holds the read-only survey, run on 2026-11-05, of how to keep an IVF vector index balanced as the corpus grows without a full retrain. It covers DIAMOND and Linclust, amortized rebalancing schemes, and what LanceDB does today. It feeds the **incremental** topic. It is not a decision.

## DIAMOND and Linclust

- Linclust keeps each sequence's m lowest-hash k-mers. Each k-mer group picks one center. Each member aligns against its center only, so the cost is O(mN).
- DIAMOND DeepClust cascades rounds. New sequences map against existing centroids. Only the unmatched ones cluster among themselves and append.
- For vectors, RaBitQ sign bits or SimHash bands stand in for k-mers.
- IVF already places a new vector against the nearest centroid without touching old data. What transfers is an outlier pool, for vectors that match no centroid within a radius, and Linclust-style bucketing to seed new partitions from that pool.

## Amortized rebalancing

- **Ada-IVF** (arXiv 2411.00970). Each partition scores temperature × (β · size deviation + (1−β) · centroid drift). Temperature rises when queries probe the partition. A global check compares reconstruction error with a full rebuild's estimate. One step splits the violating partition and runs balanced k-means over it and its 25 nearest partitions, about 26/k of the corpus. It reports 2-5 times LIRE's update throughput.
- **SPFresh / LIRE** splits on size alone and reassigns nearby vectors.
- **DeDrift** re-clusters the largest and smallest partitions together.
- **Quake** (OSDI 2025) splits and merges by a latency cost model of size × access frequency.
- Every scheme reads one partition plus a neighbourhood. None reads the corpus.

## LanceDB today

From Lance `main` source, about v14-15.

- `optimize_indices` assigns new rows to existing centroids.
- It splits a partition above 4 times target and joins one below 25% of target. The IVF_RQ default target is 4096 rows.
- A split re-reads the split partition plus its 64 nearest partitions. At 200k vectors there are about 49 partitions, so one split reads the whole corpus. At 2M it reads about 13%.
- Lance acts on size only. It tracks no drift and no query signal.
- Python marks `optimize(retrain=)` deprecated.
- New and updated rows sit outside the index and are flat-scanned until `optimize`.
- Not verified: whether LanceDB Python passes default options through to Lance.

## Recommended shape

- Rely on `optimize()` for appends and size-based split and join. Set `target_partition_size` explicitly, larger while the corpus is small, so 64 neighbours stay a small fraction.
- Keep an Ada-IVF-style deterioration score beside Lance: drift from per-row residuals at insert time, probe frequency from query logs, and size.
- Run maintenance on one partition plus 8-25 neighbours with balanced k-means seeded from their centroids. Writing centroids and assignments into Lance needs its precomputed-partitions path or a fork. This is the main integration risk.
- Use sign-bit bucketing only to seed new partitions from the outlier pool.
- Replace "retrain at each doubling" with the global reconstruction-error check.

## Sources

- https://github.com/bbuchfink/diamond/wiki/Clustering
- https://pmc.ncbi.nlm.nih.gov/articles/PMC6026198/
- https://arxiv.org/html/2411.00970v1
- https://www.usenix.org/conference/osdi25/presentation/mohoney
- https://docs.rs/lance-index/latest/src/lance_index/optimize.rs.html
- https://github.com/lance-format/lance/blob/main/rust/lance/src/index/vector/builder.rs
- https://github.com/lancedb/lancedb/blob/main/python/python/lancedb/table.py
