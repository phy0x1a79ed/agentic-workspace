---
name: mamba
tags: [environment, conda, mamba, python, packages, dependencies]
requires: []
description: Conda/mamba reference + workspace env conventions (per-project base, per-scope overlay)
---

# Mamba

Conda/mamba CLI reference plus the workspace's conventions for per-project envs and per-scope overlays.

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

This merges new/changed packages into the existing env. It does **not** remove packages deleted from the YAML; recreate the env if you need a clean slate.

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

**Never** call bare `python`, `python3`, `pip`, or `pip3` — system Python is PEP 668-locked and will reject installs outside a venv.

## Workspace convention: per-project env

Each project owns one mamba env **named after the project**, defined at `projects/<project>/main/envs/environment.yml`:

```yaml
name: <project>
channels:
  - conda-forge   # always first
  - bioconda      # second, for bio packages
dependencies:
  - python=3.11
  - ...
```

Create:

```bash
mamba env create -f projects/<project>/main/envs/environment.yml
```

## Workspace convention: per-scope overlay

Scopes that need extra packages drop an overlay at `env/environment.yml` in the worktree root and apply it on top of the project env — never create a new env for a scope:

```bash
mamba env update -n <project> -f env/environment.yml
```

`env update` merges new packages in; it does not remove packages that were deleted from the YAML.

## Bootstrap (idempotent)

```bash
mamba env list | grep -q <project> || mamba env create -f projects/<project>/main/envs/environment.yml
# then apply any scope overlay
[ -f env/environment.yml ] && mamba env update -n <project> -f env/environment.yml
```

## Lock file

```bash
mamba env export -n <project> --no-builds > projects/<project>/main/envs/environment.lock.yml
```

Commit the lock file alongside the loose `environment.yml`. Loose pins give flexibility; the lock reproduces exact versions.

## Recreating an environment

From scratch:

```bash
mamba env remove -n <project>
mamba env create -f projects/<project>/main/envs/environment.yml
```

From lock file (exact reproduction):

```bash
mamba env remove -n <project>
mamba env create -f projects/<project>/main/envs/environment.lock.yml
```
