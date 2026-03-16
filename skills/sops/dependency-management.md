---
name: dependency-management
type: sop
tags: [mamba, conda, environment, dependencies]
---

# Dependency Management

## Per-Project Mamba Environment

Each project has a dedicated mamba environment named after the project, defined in `envs/environment.yml` within the main worktree.

```yaml
# envs/environment.yml
name: my-project
channels:
  - conda-forge
  - bioconda
dependencies:
  - python=3.11
  - numpy
  - pandas
```

### Channel Priority

1. **conda-forge** — default, general-purpose packages
2. **bioconda** — bioinformatics-specific packages

Always list `conda-forge` before `bioconda` in the channels list.

### Creating the Environment

```bash
mamba env create -f envs/environment.yml
```

### Activating

```bash
mamba activate <project-name>
```

## Per-Task Overlay Pattern

Tasks can add temporary or task-specific dependencies via an overlay file at `env/environment.yml` in the worktree root.

```yaml
# env/environment.yml (in task worktree)
channels:
  - conda-forge
  - bioconda
dependencies:
  - scikit-learn
  - seaborn
```

Install the overlay on top of the project env:

```bash
mamba env update -n <project-name> -f env/environment.yml
```

This adds packages to the existing environment without replacing it.

## Adding Dependencies

1. Add the package to the appropriate `environment.yml` (project-level or task overlay).
2. Run `mamba env update`:
   ```bash
   # Project-level
   mamba env update -n <project-name> -f envs/environment.yml

   # Task overlay
   mamba env update -n <project-name> -f env/environment.yml
   ```
3. Commit the updated `environment.yml`.

## Exporting an Environment

```bash
mamba env export -n <project-name> --no-builds > envs/environment.lock.yml
```

The lock file captures exact versions for reproducibility. The base `environment.yml` keeps loose pins for flexibility.

## Recreating an Environment

From scratch:

```bash
mamba env remove -n <project-name>
mamba env create -f envs/environment.yml
```

From lock file (exact reproduction):

```bash
mamba env remove -n <project-name>
mamba env create -f envs/environment.lock.yml
```
