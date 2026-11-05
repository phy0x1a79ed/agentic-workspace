# semantic: survey of disk-based vector search

## Purpose & Contents

This file holds the read-only survey of disk-based approximate nearest-neighbour methods, judged by cold-cache latency on altair's disk and by update cost. It feeds the **semantic** topic. It is not a decision.

## Internal measurements

- **82c9915c** (5.17M chunks, warm): LanceDB IVF-RQ recall@20 0.991 at 23 ms p50 and 29 ms p95. IVF-PQ reached 0.896. sqlite-vec int8 is exact at about 4.6 s.
- **5f2c38e3** (faiss round 1, warm, in RAM): chunk recall against exact may drop to 0.5-0.7 and the gain over keyword alone still holds. About 75 B per vector keeps the whole gain. It measured altair's vda at **12 random IOPS and 17 MB/s sequential**.
- **7bf7afdc** (faiss round 2): the only cold data. IVF-PQ64 with on-disk lists on altair vda, nprobe 16: cold 195 ms p50 and 779 ms p95, warm 4 ms. mmap readahead inflated reads 4-11 times, so exact `pread` of list ranges is needed. Appends after training cost 7% recall, so retrain at each doubling.
- **f73aeb5c**: grouped IVF with exact pread, cold p50 164 ms and p95 520 ms on SSD.

**CAUTION** altair's sequential speed is disputed: 17 MB/s in 5f2c38e3 against "fine" in 367a5d0d. Re-measure before any design relies on a sequential scan.

## Methods

| Method | Random reads per query | Cold | Update cost | Py/Rust |
|---|---|---|---|---|
| LanceDB IVF-PQ / IVF-RQ | nprobe large partitions, plus a refine read per candidate when refine is on | Good with large partitions and no refine. Never measured cold | Appends are brute-searched until `optimize()`. Versions pile up until cleanup | Both |
| sqlite-vec int8 / bit | None. A sequential scan of every code | 200k 1-bit codes = 26 MB | A row upsert | Both |
| Binary codes + rerank | Scan, plus one read per rerank candidate unless the rerank codes sit beside the bits | A float rerank at QD1 costs seconds | Trivial | Any |
| faiss IVF on-disk lists | nprobe list reads | 195/779 ms cold on altair | Lists fragment. Retrain at doubling | Python |
| SPANN / SPFresh | nprobe posting lists in parallel | About one round trip at QD32 | In-place split and reassign | C++ only |
| DiskANN, HNSW, USearch mmap, libSQL vector | One dependent random read per hop, 30-300 per query | Seconds at 17 IOPS | Varies | Varies |

## Conclusions

- Graph indexes do not fit. Each hop waits on the one before it.
- The fitting shape is a few large reads of compact codes in parallel: IVF with large partitions, exact pread at QD32, and a rerank from extra-bit codes stored in the same partition.
- At 200k vectors a flat scan of 1-bit codes, about 28 MB, may beat any index. The disputed sequential speed decides it. IVF is needed by 2M.
- LanceDB IVF-RQ is the best packaged fit, but it needs a cold benchmark.
- Judge vectors by what they add to keyword search (precedence 807f894227144857). Codes of 32-75 B keep that gain.
