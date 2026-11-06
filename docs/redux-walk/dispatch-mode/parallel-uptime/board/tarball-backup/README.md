# tarball-backup

## Purpose & Contents

The backup policy for the pinned Trilium server tarball.

## Result (2026-11-05)

- Tony approved a GitHub backup: "Everything else looks good" (answer to Q-tarball-backup).
- The pinned tarball is a release on the public fork phy0x1a79ed/Trilium, tag fork-v0.105.0-110-gd13d5fa4ee. It is not marked latest, and its notes carry sha256 6699d45396df91d8ae9884d8b4d8b75182327e5391522a923bdb3511dcb63fda. A download back matched the pin.
- deneb still gets mizar's push. GitHub is a backup copy only.
- The fork's code branches and tags are pushed. `release` goes as `fork-release`, because GitHub refused the name next to upstream's `release/*`.
- vault-history is not pushed. `fork-release` carries the vault's .dvc pins: md5, size, path, nfiles and the hash algorithm, with no remote and no credentials.
