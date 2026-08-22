"""Which agent does this process belong to?

The answer is one environment variable. Every process a Claude Code Bash tool
spawns carries ``CLAUDE_CODE_SESSION_ID``, and every descendant inherits it —
including detached, ``nohup``'d, ``setsid``'d and init-reparented work. It is
the same string the hooks receive in their payload, so the join between "the
job we stopped" and "the agent to tell" is exact rather than heuristic.

This replaced an earlier design that walked the process tree up to the owning
``claude`` process. Prototyped against this box, that misattributed 76 of 297
processes — a live ESM-C embedding job among them — to "the harness". The
cause is structural and will not go away: Claude Code is daemon-hosted, so
every session hangs off one shared ``claude daemon run``; the session process
is a version-numbered binary (``.../versions/2.1.220``) rather than anything
called ``claude``; and a re-used spare still has ``bg-spare`` in its argv. No
amount of pattern-matching recovers ownership from that shape. The environment
variable is simply correct.

Two rules, in order:

1. **Believe the process's own environment.** A read costs ~6 us and only ever
   happens once per process, so the whole box is ~2.2 ms cold and effectively
   free thereafter. Asking the process itself is also the only way to get a
   *nested* agent right: when one agent spawns another, the inner session's
   processes descend from the outer agent's shell, so inheritance alone would
   file the child's work under the parent.
2. **Fall back to an already-attributed parent.** This is what covers a job
   that scrubs its own environment, or one we cannot read — it is still that
   agent's job.

Everything is cached on ``(pid, start_ticks)``. The start time is not
decoration: without it, a recycled pid inherits the attribution of whatever
used to hold that number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from awm.compute.probe import Proc, ProcKey, read_environ

SESSION_VAR = "CLAUDE_CODE_SESSION_ID"
JOB_DIR_VAR = "CLAUDE_JOB_DIR"


@dataclass(slots=True)
class Attributor:
    """Sticky pid → session-id map, rebuilt incrementally each full pass."""

    _cache: dict[ProcKey, str | None] = field(default_factory=dict)
    #: Job dir per session, when the process advertised one. Purely for the
    #: human-readable record; nothing keys off it.
    _job_dirs: dict[str, str] = field(default_factory=dict)
    env_reads: int = 0
    total_reads: int = 0

    def resolve_all(self, procs: dict[int, Proc]) -> dict[int, str | None]:
        """Attribute every sampled process. Returns pid → session id or None."""
        out: dict[int, str | None] = {}
        for pid in procs:
            out[pid] = self._resolve(pid, procs, set())
        self._prune({p.key for p in procs.values()})
        return out

    def _resolve(
        self,
        pid: int,
        procs: dict[int, Proc],
        seen: set[int],
    ) -> str | None:
        proc = procs.get(pid)
        if proc is None or pid in seen:
            return None
        key = proc.key
        cached = self._cache.get(key, _MISS)
        if cached is not _MISS:
            return cached  # type: ignore[return-value]

        seen.add(pid)

        self.env_reads += 1
        env = read_environ(pid)
        sid: str | None = env.get(SESSION_VAR) or None
        if sid and (job_dir := env.get(JOB_DIR_VAR)):
            self._job_dirs.setdefault(sid, job_dir)

        if sid is None:
            parent = procs.get(proc.ppid)
            # A parent that started *after* its child is not really its parent
            # — the pid was recycled. Refuse the inheritance; an unattributed
            # process is never acted on, which is the safe direction.
            if parent is not None and parent.start_ticks <= proc.start_ticks:
                sid = self._resolve(proc.ppid, procs, seen)

        self.total_reads += 1
        self._cache[key] = sid
        return sid

    def _prune(self, live: set[ProcKey]) -> None:
        if len(self._cache) <= len(live) * 2:
            return
        for key in [k for k in self._cache if k not in live]:
            del self._cache[key]

    def job_dir(self, session_id: str) -> str | None:
        return self._job_dirs.get(session_id)

    def stats(self) -> dict:
        return {
            "cached_pids": len(self._cache),
            "env_reads": self.env_reads,
            "resolutions": self.total_reads,
        }


class _Miss:
    __slots__ = ()


_MISS = _Miss()
