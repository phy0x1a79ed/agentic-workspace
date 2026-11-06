# bio: survey of bioinformatics sequence search for text lookup

## Purpose & Contents

This file holds the read-only survey, run on 2026-11-05, of whether BLAST, DIAMOND and related sequence-search tools could serve as the exact-match engine. It feeds the **tooling** topic. It is not a decision.

## Tools

| Tool | Fitting 36 letters | One cold query | RAM | Updates | Ids and filters |
|---|---|---|---|---|---|
| BLAST+ blastn | 3 nt per character. Short queries need word 7 | Scans the database: about 13 s at 17 MB/s for 225 MB packed. makembindex needs word >= 16 | Small, large with index | New volume + alias. Edits via `-negative_seqidlist` | Id in seq id. `-seqidlist` pre-filters |
| DIAMOND | Protein only. Reduced seed alphabet merges letters | Batch: reads the whole reference each run | About 6x block size in GB | Rebuild | Post-filter |
| MMseqs2 | Character coding needed | Interactive use assumes the index in RAM | Index several times the database* | Full re-index | Post-filter |
| BWA / Bowtie2 | DNA, 3 nt per character | 120-240 dependent reads, 7-14 s | Whole index in RAM | Static | Post-filter |
| minimap2 | DNA | Short queries yield no minimizers | GBs | Rebuild | Post-filter |
| Kraken2 / KMC / Jellyfish | DNA k-mer counts, no positions | mmap mode far slower than preload | Whole table | Rebuild | — |
| Mash / sourmash | Whole-set Jaccard | Not substring search | Small | Easy | Document-level |
| rust-bio FMIndex / SeqAn3 | Any alphabet | In memory only. On disk, 40-80 dependent reads, 2.5-5 s | 170-250 MB over 300 MB text* | Static | Own table |

A `*` marks a claim from memory.

## Conclusions

- No bio tool fits as the engine. They are built for batches, for RAM, or for static data. One cold query scans the whole database or needs the index in memory.
- The useful takeaways are data structures:
  1. A k-mer seed index with positional postings, a q-gram index. Query the 2-3 rarest non-overlapping k-mers (k about 5-7 over 36 letters). That is 3-6 random reads, then a verify.
  2. Minimizers shrink the index to about 2/(w+1) of full size. Every query of length >= w+k-1 still hits a stored minimizer.
  3. Append-only volumes plus a tombstone list handle appends and edits, as BLAST volumes and `-negative_seqidlist` do.
  4. FM-index backward search fits a small in-memory segment only.
  5. The q-gram count filter and spaced seeds give typo tolerance over the same postings.

## Sources

- https://www.ncbi.nlm.nih.gov/books/NBK569854/
- https://github.com/bbuchfink/diamond/wiki/3.-Command-line-options
- https://manpages.ubuntu.com/manpages/lunar/man1/mmseqs.1.html
- https://github.com/DerrickWood/kraken2/wiki/Manual
- https://docs.rs/bio
