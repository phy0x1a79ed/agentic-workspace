from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
import urllib.request

from awm.hpcllm import ssh_helpers as ssh
from awm.hpcllm.cluster import ClusterConfig, resolve_cluster
from awm.hpcllm.dao import HpcllmDAO
from awm.hpcllm.models import ModelSpec, resolve_model
from awm.hpcllm.provider import (
    build_active_dir,
    build_req_path,
    build_remote_scratch_dir,
    build_sbatch,
)

log = logging.getLogger("awm.hpcllm.lifecycle")

_SQUEUE_POLL_INTERVAL = 10.0
_PROBE_INTERVAL = 5.0
_PROBE_ATTEMPTS = 60
_MAX_RETRIES = 3
_RETRY_DELAY = 2.0

_PORT_LOCK = asyncio.Lock()
_IN_USE_PORTS: set[int] = set()


async def _assign_port(cfg: ClusterConfig) -> int:
    async with _PORT_LOCK:
        port = cfg.base_forward_port
        while port in _IN_USE_PORTS:
            port += 1
        _IN_USE_PORTS.add(port)
        return port


def _release_port(port: int) -> None:
    _IN_USE_PORTS.discard(port)


async def _probe_endpoint(port: int, timeout: float = 15.0) -> bool:
    """Probe the LLM server endpoint through the SSH tunnel."""
    url = f"http://localhost:{port}/v1/models"

    def _probe() -> bool:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

    return await asyncio.to_thread(_probe)


class ServeRun:
    """Tracks a single serve request's in-flight state."""

    __slots__ = (
        "task", "serve_id", "cfg", "model", "job_id",
        "node", "local_port", "remote_port", "cancel_event",
    )

    def __init__(self, serve_id: str, cfg: ClusterConfig,
                 model: ModelSpec) -> None:
        self.task: asyncio.Task | None = None
        self.serve_id = serve_id
        self.cfg = cfg
        self.model = model
        self.job_id = ""
        self.node = ""
        self.local_port = 0
        self.remote_port = 8000
        self.cancel_event = asyncio.Event()


class ServeLifecycle:
    """Manages the full lifecycle of a serve request."""

    def __init__(self, dao: HpcllmDAO,
                 emit_fn) -> None:
        self._dao = dao
        self._emit = emit_fn
        self._runs: dict[str, ServeRun] = {}

    async def start(self, serve_id: str, model_name: str,
                    cluster: str, hours: int, gpus: int,
                    cpus: int, mem: int) -> dict:
        cfg = resolve_cluster(cluster)
        model = resolve_model(model_name, cluster)
        if model is None:
            raise ValueError(
                f"model {model_name!r} not available on cluster {cluster!r}"
            )

        run = ServeRun(serve_id, cfg, model)
        self._runs[serve_id] = run
        run.task = asyncio.create_task(
            self._run(run, hours, gpus, cpus, mem)
        )
        return self._dao.get(serve_id)

    async def cancel(self, serve_id: str) -> None:
        run = self._runs.pop(serve_id, None)
        if run is None:
            rec = self._dao.get(serve_id)
            if rec and rec["status"] in ("cancelled", "error", "done"):
                return
            raise ValueError(f"no active serve {serve_id!r}")

        run.cancel_event.set()

        if run.task and not run.task.done():
            run.task.cancel()
            try:
                await run.task
            except asyncio.CancelledError:
                pass

        host = run.cfg.ssh_host

        if run.job_id:
            try:
                ssh.ssh_run(host, f"scancel {run.job_id}", check=False,
                            timeout=15.0)
            except Exception as e:
                log.warning("scancel %s failed: %s", run.job_id, e)

        if run.node and run.local_port:
            try:
                ssh.ssh_cancel_forward(
                    host, run.local_port, run.node, run.remote_port)
            except Exception as e:
                log.warning("cancel forward for %s failed: %s",
                            serve_id, e)

        try:
            remote_dir = build_remote_scratch_dir(run.cfg, serve_id)
            await ssh.ssh_rm_async(host, remote_dir)
        except Exception as e:
            log.warning("cleanup scratch %s failed: %s", serve_id, e)

        _release_port(run.local_port)

        self._dao.update_status(
            serve_id, "cancelled",
            error_message="cancelled by user",
        )

        await self._emit({
            "serve_id": serve_id,
            "status": "cancelled",
            "model": run.model.name,
            "cluster": run.cfg.name,
            "timestamp": time.time(),
            "message": "Serve cancelled",
        })

    async def _run(self, run: ServeRun, hours: int, gpus: int,
                   cpus: int, mem: int) -> None:
        log.info("starting lifecycle for serve %s", run.serve_id)
        host = run.cfg.ssh_host
        serve_id = run.serve_id

        try:
            await self._emit_status(run, "staging",
                                    "Ensuring SSH connection")

            for attempt in range(_MAX_RETRIES):
                try:
                    await asyncio.to_thread(ssh.ensure_connected, host)
                    break
                except RuntimeError:
                    if attempt < _MAX_RETRIES - 1:
                        log.warning("ssh connect attempt %d failed, retrying",
                                    attempt + 1)
                        await asyncio.sleep(_RETRY_DELAY)
                    else:
                        raise

            await self._emit_status(run, "staging",
                                    "Staging sbatch script to scratch")

            scratch_dir = build_remote_scratch_dir(run.cfg, serve_id)
            await asyncio.to_thread(
                ssh.ssh_run, host, f"mkdir -p {scratch_dir} {run.cfg.scratch_dir}/logs",
                timeout=15.0,
            )

            req_path = build_req_path(run.cfg, serve_id)
            sbatch_script = build_sbatch(
                serve_id, run.model, run.cfg, hours, gpus, cpus, mem,
            )
            await asyncio.to_thread(
                ssh.ssh_write_file, host, req_path, sbatch_script,
            )

            await self._emit_status(run, "submitted",
                                    "Submitting SLURM job")

            result = await asyncio.to_thread(
                ssh.ssh_run, host,
                f"sbatch --chdir={run.cfg.scratch_dir} {req_path}",
                timeout=30.0,
            )
            out = result.stdout.strip()
            job_id = ""
            for line in out.splitlines():
                line = line.strip()
                if "Submitted batch job" in line:
                    job_id = line.split()[-1]
                    break
                if line.isdigit():
                    job_id = line
                    break

            if not job_id:
                raise RuntimeError(
                    f"sbatch did not return a job ID: {out}"
                )

            run.job_id = job_id
            self._dao.update_status(serve_id, "submitted",
                                    slurm_job_id=job_id)

            await self._emit_status(
                run, "submitted",
                f"SLURM job {job_id} submitted",
                slurm_job_id=job_id,
            )

            await self._monitor(run)

        except asyncio.CancelledError:
            log.info("lifecycle for %s cancelled", serve_id)
            raise
        except Exception as e:
            log.error("lifecycle for %s failed: %s", serve_id, e)
            self._dao.update_status(serve_id, "error",
                                    error_message=str(e))
            await self._emit_status(run, "error", str(e))
            self._cleanup(run)

    async def _monitor(self, run: ServeRun) -> None:
        host = run.cfg.ssh_host
        serve_id = run.serve_id
        job_id = run.job_id

        node_detected = False
        tunnel_created = False
        ready_emitted = False
        probe_attempts = 0

        while not run.cancel_event.is_set():
            rc, out, err = await ssh.ssh_run_async(
                host, f"squeue -j {job_id} -h -o %T",
                timeout=15.0,
            )

            if rc != 0:
                if ("Invalid job id" in err or "does not exist" in err
                        or "launching" in err.lower()):
                    if not ready_emitted:
                        raise RuntimeError(
                            f"SLURM job {job_id} no longer exists"
                        )
                    break
                log.warning("squeue poll failed (rc=%d): %s", rc, err)
                await asyncio.sleep(_SQUEUE_POLL_INTERVAL)
                continue

            slurm_state = out.strip()

            if slurm_state == "PENDING":
                if run.cancel_event.is_set():
                    return
                await asyncio.sleep(_SQUEUE_POLL_INTERVAL)
                continue

            if slurm_state == "RUNNING" and not node_detected:
                active_dir = build_active_dir(run.cfg, serve_id)
                node = await ssh.ssh_read_file_async(
                    host, f"{active_dir}/hostname"
                )
                if node:
                    run.node = node
                    node_detected = True
                    self._dao.update_status(
                        serve_id, "running", node=node,
                    )

                    if not tunnel_created:
                        # The job picks its own free port, because the node may
                        # be shared with three other GPU jobs. Absent file means
                        # an older sbatch that still hardcoded 8000.
                        remote = await ssh.ssh_read_file_async(
                            host, f"{active_dir}/port")
                        run.remote_port = int(remote) if remote else 8000
                        local_port = await _assign_port(run.cfg)
                        run.local_port = local_port
                        await asyncio.to_thread(
                            ssh.ssh_forward, host, local_port, node,
                            run.remote_port,
                        )
                        tunnel_created = True
                        self._dao.update_status(
                            serve_id, "running",
                            node=node,
                            api_port=run.remote_port,
                            local_port=local_port,
                        )
                        await self._emit_status(
                            run, "running",
                            f"Server starting on node {node}",
                            node=node,
                            api_url=f"http://localhost:{local_port}",
                        )

                await asyncio.sleep(_SQUEUE_POLL_INTERVAL)
                continue

            if slurm_state == "RUNNING" and node_detected and not ready_emitted:
                status_val = await ssh.ssh_read_file_async(
                    host, f"{build_active_dir(run.cfg, serve_id)}/status"
                )

                # The status file is a hint, never the authority. The sbatch
                # writes "loading" and then execs llama-server, so it has no
                # chance to write anything afterwards -- and a serve whose
                # readiness was gated on that file leaving "loading" therefore
                # never became ready at all, however healthy the endpoint was.
                # Probe first, and treat the file as colour for the message.
                if not await _probe_endpoint(run.local_port):
                    if status_val == "loading":
                        await self._emit_status(
                            run, "loading",
                            "Model loading onto GPU",
                            api_url=f"http://localhost:{run.local_port}",
                            node=run.node,
                        )
                        probe_attempts += 1
                        if probe_attempts >= _PROBE_ATTEMPTS:
                            raise RuntimeError(
                                f"Server did not become ready after "
                                f"{_PROBE_ATTEMPTS * _PROBE_INTERVAL:.0f}s"
                            )
                        await asyncio.sleep(_PROBE_INTERVAL)
                        continue

                if await _probe_endpoint(run.local_port):
                    ready_emitted = True
                    self._dao.update_status(serve_id, "ready")
                    await self._emit_status(
                        run, "ready",
                        f"Server ready on node {run.node}",
                        api_url=f"http://localhost:{run.local_port}",
                        node=run.node,
                    )
                    log.info("serve %s ready at localhost:%d",
                             serve_id, run.local_port)
                    break

                probe_attempts += 1
                if probe_attempts >= _PROBE_ATTEMPTS:
                    raise RuntimeError(
                        f"Server did not become ready after "
                        f"{_PROBE_ATTEMPTS * _PROBE_INTERVAL:.0f}s"
                    )

                await asyncio.sleep(_PROBE_INTERVAL)
                continue

            if slurm_state in ("COMPLETED", "FAILED", "CANCELLED",
                               "TIMEOUT", "NODE_FAIL"):
                if not ready_emitted:
                    raise RuntimeError(
                        f"SLURM job {job_id} ended with state {slurm_state}"
                    )
                break

            await asyncio.sleep(_SQUEUE_POLL_INTERVAL)

        if tunnel_created and not run.cancel_event.is_set():
            log.info("serve %s lifecycle complete, tunnel active at :%d",
                     serve_id, run.local_port)

    def _cleanup(self, run: ServeRun) -> None:
        try:
            if run.node and run.local_port:
                ssh.ssh_cancel_forward(
                    run.cfg.ssh_host, run.local_port, run.node,
                    run.remote_port)
        except Exception as e:
            log.warning("cleanup forward for %s: %s", run.serve_id, e)
        _release_port(run.local_port)
        self._runs.pop(run.serve_id, None)

    async def _emit_status(self, run: ServeRun, status: str,
                           message: str, *,
                           slurm_job_id: str = "",
                           node: str = "",
                           api_url: str = "") -> None:
        payload = {
            "serve_id": run.serve_id,
            "status": status,
            "model": run.model.name,
            "cluster": run.cfg.name,
            "timestamp": time.time(),
            "message": message,
        }
        if slurm_job_id:
            payload["slurm_job_id"] = slurm_job_id
        if node:
            payload["node"] = node
        if api_url:
            payload["api_url"] = api_url
        await self._emit(payload)
