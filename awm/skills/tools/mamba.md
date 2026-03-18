---
name: Mamba
type: tool
tags: [environment, conda, mamba]
description: Conda/mamba quick reference — create, install, export, run
---

# Mamba Quick Reference

## Create Environment

```bash
mamba create -n myenv python=3.11
mamba create -n myenv --file requirements.txt
mamba create -n myenv -c conda-forge numpy pandas
```

## Install Packages

```bash
mamba install -n myenv numpy pandas scikit-learn
mamba install -n myenv -c conda-forge some-package
```

## Update Environment from YAML

```bash
mamba env update -n myenv --file environment.yml
```

This merges new/changed packages into the existing env. It does not remove packages deleted from the YAML; recreate the env if you need a clean slate.

## Export Environment

```bash
mamba env export -n myenv > environment.yml          # full solve (pinned builds)
mamba env export -n myenv --no-builds > environment.yml  # drop build strings
mamba env export -n myenv --from-history > environment.yml  # only explicitly installed
```

Prefer `--from-history` for portability across platforms.

## Channel Configuration

Set conda-forge as highest priority in `~/.condarc`:

```yaml
channels:
  - conda-forge
  - defaults
channel_priority: strict
```

`strict` means a package is only pulled from a lower-priority channel if it does not exist in a higher one.

## List Environments

```bash
mamba env list
```

## Running Commands in an Environment

**Interactive shell** (requires `conda init` / `mamba init`):

```bash
mamba activate myenv
python script.py
mamba deactivate
```

**Non-interactive shell** (agents, scripts, CI — use this by default):

```bash
mamba run -n myenv python script.py
mamba run -n myenv pip install <package>
mamba run -n myenv <any-command>
```

`mamba activate` only works in interactive shells that have run `conda init`. In agent contexts, **always use `mamba run`**.
