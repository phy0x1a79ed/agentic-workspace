---
name: metasmith
type: reference
scope: workspace
tags: [pipeline, metasmith, bioinformatics, hpc, slurm, nextflow, annotation]
requires: []
description: Metasmith pipeline reference — composable transforms for HPC bioinformatics workflows via Nextflow + SLURM
---

# Metasmith Quick Reference

Metasmith is a workflow orchestration tool that generates and runs Nextflow pipelines from composable Python transforms. It handles containerized execution (Docker/Apptainer), SLURM job submission, and result collection.

## Environment Setup

```bash
conda activate msm_env  # metasmith 0.15.1, Nextflow 25.10.0
```

## Core Concepts

- **Transform**: A Python file defining inputs, outputs, container image, and a `protocol()` function with shell commands
- **DataInstanceLibrary**: Registry of input files with typed entries (e.g., `sequences::orfs`, `sequences::assembly`)
- **TransformInstanceLibrary**: Registry of available transforms
- **Agent**: Connection to a metasmith deployment (local or remote via SSH)
- **Task**: A generated workflow with a unique key (e.g., `W06rU63X`)

## Python API Usage

```python
from metasmith.python_api import (
    Agent, SshSource,
    DataInstanceLibrary, TransformInstanceLibrary,
    TargetBuilder, ContainerRuntime, Resources, Size,
)

# Connect to remote HPC agent
agent_home = SshSource(host="fir", path="/scratch/user/metasmith").AsSource()
smith = Agent(
    home=agent_home,
    runtime=ContainerRuntime.APPTAINER,
    setup_commands=["module load apptainer"],
)

# Build inputs
inputs = DataInstanceLibrary(lib_dir)
inputs.AddTypeLibrary(mlib / "data_types" / "sequences.yml")
inputs.AddItem(Path("/path/to/file.faa"), "sequences::orfs")
inputs.Save()

# Generate workflow
task = smith.GenerateWorkflow(
    samples=list(inputs.AsSamples("sequences::orfs")),
    resources=[containers, inputs],
    transforms=[TransformInstanceLibrary.Load(transforms_dir)],
    targets=targets,
)

# Stage and run
smith.StageWorkflow(task, on_exist="update", verify_external_paths=False)
smith.RunWorkflow(
    task=task,
    config_file=smith.GetNxfConfigPresets()["slurm"],
    params=dict(
        slurmAccount="rrg-shallam-ab",
        executor=dict(queueSize=500),
        process=dict(tries=3),
    ),
    resource_overrides={"*": Resources(cpus=4, memory=Size.GB(32))},
)
```

## Transform Structure

```python
from metasmith.python_api import *

lib = TransformInstanceLibrary.ResolveParentLibrary(__file__)
model = Transform()

image = model.AddRequirement(lib.GetType("containers::tool.oci"))
orfs  = model.AddRequirement(lib.GetType("sequences::orfs"))
out   = model.AddProduct(lib.GetType("annotation::results"))

def protocol(context: ExecutionContext):
    iorfs = context.Input(orfs)
    iout  = context.Output(out)

    context.ExecWithContainer(
        image=image,
        cmd=f"tool --input {iorfs.container} --output /ws/result",
    )
    context.LocalShell(f"cp result/output.txt {iout.local}")

    return ExecutionResult(
        manifest=[{out: iout.local}],
        success=iout.local.exists(),
    )

TransformInstance(
    protocol=protocol,
    model=model,
    group_by=orfs,
    resources=Resources(cpus=4, memory=Size.GB(8), duration=Duration(hours=2)),
)
```

## Staging Modes (`on_exist`)

- `"clear"` — wipes previous task state and re-stages from scratch. **Dangerous:** if results were already collected, they are lost.
- `"update"` — preserves existing state, only updates changed files. **Use this for re-runs** so completed samples are not re-processed.

## Killing a Running Workflow

To stop a running workflow on fir, delete its PID lock file:

```bash
# Find and remove the lock file for the task
rm /scratch/phyberos/metasmith/runs/<TASK_KEY>/PID.lock
```

This signals the metasmith agent to stop. The Nextflow process and any running SLURM jobs will be cleaned up. Do **not** kill the Nextflow Java process directly — use the lock file.

## Nextflow Config (SLURM)

The metasmith SLURM config (`workflow.config.nf`) controls:

```groovy
// Resource scaling: doubles memory and time on each retry
process {
    memory = { (2**(task.attempt-1)) * ('32 GB' as MemoryUnit) }
    time   = { (2**(task.attempt-1)) * ('4hours' as Duration) }
    errorStrategy = { task.attempt < params.process.tries ? 'retry' : 'ignore' }
}
cleanup = true  // removes work dirs of completed tasks (saves disk/inodes)
```

Resource overrides per process are written to `workflow.resources.nf`.

## Common Gotchas

- **`cleanup=true`** removes work dirs after task completion. This makes `-resume` useless for recovery. Set `cleanup=false` if you need to re-run failed samples without re-processing everything.
- **`start.sh` overwrites results**: Running `start.sh` directly on fir creates a new log dir and re-runs the full workflow. The "compiling results" step at the end **overwrites `results/`**. Use the Python API with `on_exist="update"` instead.
- **Concurrent Nextflow JVMs**: Do not run more than ~2 Nextflow JVMs on a login node simultaneously. Each JVM spawns many threads; exceeding the per-user process limit causes `pthread_create EAGAIN` and crashes all running pipelines.
- **`module load StdEnv/2023`** on fir login nodes intermittently fails (lmod/cvmfs issue). The metasmith agent config on fir adds this to `start.sh`. Comment it out — `module load apptainer` alone works.
- **Container CWD**: Metasmith's exit-code trap needs `/ws` as CWD. If a tool requires `cd` to its install dir, use a subshell: `(cd /opt/tool && python run.py -o /ws/result)`.
- **Exit code 140** = SIGKILL (OOM or wall-time exceeded by SLURM). Exit code `-` (no code) in the trace also typically means SLURM killed the job.

## HPC Paths (fir)

| Resource | Path |
|----------|------|
| Metasmith home | `/scratch/phyberos/metasmith/` |
| Container images | `/scratch/phyberos/metasmith/container_images/` |
| Run dirs | `/scratch/phyberos/metasmith/runs/<TASK_KEY>/` |
| Logs (latest) | `/scratch/phyberos/metasmith/runs/<KEY>/_metasmith/logs.latest/` |
| SLURM account | `rrg-shallam-ab` |
