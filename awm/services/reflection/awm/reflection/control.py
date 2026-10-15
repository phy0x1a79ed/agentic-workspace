"""Session control: Remote Control, model, effort, rename, interrupt, stop, link, message.

Every verb here acts on the caller's own session when it names no ``job``, and
on any other session on this node when it does. ``job`` is a job id or the name
`list_other` shows; a name two sessions share is refused with the candidates
listed. Nothing here reaches another node.

A target is reached the way the caller's own session is: a background job
through its PTY socket, an interactive session through its tmux pane. Both go
through :func:`session_target.resolve` with the target's REPL pid, so the
identity checks that guard a self-call guard these too.

Before it types, a verb refuses when the target's record says it is waiting on
an answer or its screen shows a modal. A keystroke into a modal answers it.

`effort` and `model` are the exception that answers a modal on purpose. Claude
Code asks "Change effort level?" / "Switch model?" when a conversation with
output changes either, and the verb picks "Yes, switch to <the value asked for>"
rather than leave the session waiting on a person. The caller's own session
cannot answer in the turn that asks, so a detached thread does, and the resume
prompt then reports how the change ended.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from awm.claudedaemon import cli, roster
from awm.reflection import (
    guards,
    inject,
    observation,
    pending,
    permission_mode,
    session_target,
    settings_guard,
    watcher,
)

log = logging.getLogger("awm.reflection.control")

EFFORTS = ("low", "medium", "high", "xhigh", "max")
_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]:-]*$")
_RC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# How long `remote_control(on)` waits for the bridge id to reach the job record,
# and `off` for the modal and then for the id to clear.
LINK_WAIT_S = 15.0
MODAL_WAIT_S = 8.0
CLEAR_WAIT_S = 10.0
POLL_S = 0.5
# A session turning Remote Control off from inside its own turn has to wait for
# that turn to end before the modal exists.
SELF_MODAL_WAIT_S = 600.0
SELF_POLL_S = 2.0
# Longest a modal's list is walked before giving up. The Remote Control modal
# has a handful of entries.
MAX_MODAL_STEPS = 6
# Beat between a keypress and reading the screen back.
KEY_SETTLE_S = 0.35
# Lets `stop` of the caller's own session return its answer first.
SELF_STOP_DELAY_S = 1.5

DISCONNECT_OPTION = "disconnect this session"

SELF_OFF_FOLLOWUP = ("Remote Control is now off. Continue with what you were "
                     "doing.")

_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic

_CURSOR = re.compile(r"[❯›▶]\s*([^\n]*)")


@dataclass(frozen=True)
class Target:
    """One session on this node that a verb may act on."""

    pid: int
    kind: str
    job: Optional[str]
    name: Optional[str]
    alive: bool
    is_self: bool = False
    session_id: Optional[str] = None

    @property
    def label(self) -> str:
        return self.name or self.job or str(self.pid)


# --- resolving `job` ---------------------------------------------------------


def _alive(pid: Any, start: Any) -> bool:
    return bool(start) and isinstance(pid, int) and roster.proc_start(pid) == str(start)


def _candidates() -> list[Target]:
    """Every session on this node that `list_other` would show."""
    out = [
        Target(s.repl_pid, "bg", s.short, s.name, _alive(s.repl_pid, s.repl_proc_start),
               session_id=s.session_id)
        for s in roster.load(session_target.ROSTER_PATH, session_target.JOBS_DIR)
        if s.has_record and isinstance(s.repl_pid, int)
    ]
    try:
        files = sorted(session_target.SESSIONS_DIR.glob("*.json"))
    except OSError:
        files = []
    for path in files:
        rec = roster.read_json(path)
        pid = rec.get("pid")
        if rec.get("kind") == "bg" or not _alive(pid, rec.get("procStart")):
            continue
        out.append(Target(pid, "interactive", None, rec.get("name"), True,
                          session_id=rec.get("sessionId")))
    return out


def _describe(t: Target) -> str:
    where = f"job {t.job}" if t.job else f"interactive pid {t.pid}"
    return f"{where} named {t.name!r}" + ("" if t.alive else " (not running)")


def _caller_target(caller_pid: Any) -> Target:
    if not caller_pid:
        raise session_target.ResolveError(
            "could not tell which session is calling, so there is no session of "
            "your own to act on. Name another with `job`, or call through a "
            "session's own awm-mcp proxy")
    record = session_target.read_session_record(caller_pid)
    bg = record.get("kind") == "bg"
    return Target(caller_pid, "bg" if bg else "interactive",
                  str(record.get("jobId") or "") or None if bg else None,
                  record.get("name"), True, True, record.get("sessionId"))


def resolve(job: Optional[str], caller_pid: Any) -> Target:
    """The session ``job`` names, or the caller's own when it is omitted.

    Raises :class:`session_target.ResolveError` for an unknown or ambiguous name.
    """
    ref = job.strip() if isinstance(job, str) else ""
    if not ref:
        return _caller_target(caller_pid)
    cands = _candidates()
    hits = [t for t in cands if ref in (t.job, t.session_id)]
    for matches in (hits,
                    [t for t in cands if t.name == ref],
                    [t for t in cands if (t.name or "").lower() == ref.lower()]):
        if len(matches) == 1:
            return _mark_self(matches[0], caller_pid)
        if len(matches) > 1:
            raise session_target.ResolveError(
                f"{ref!r} names {len(matches)} sessions: "
                + "; ".join(_describe(t) for t in matches)
                + ". Pass the job id.")
    raise session_target.ResolveError(
        f"no session on this node has the job id or name {ref!r}; "
        f"`list_other` shows them")


def _mark_self(t: Target, caller_pid: Any) -> Target:
    if not caller_pid:
        return t
    caller_job = roster.job_of_pid(caller_pid, sessions_dir=session_target.SESSIONS_DIR)
    same = t.pid == caller_pid or (t.job is not None and t.job == caller_job)
    return replace(t, is_self=True) if same else t


def _require_running(t: Target) -> None:
    if not t.alive:
        raise session_target.ResolveError(
            f"{_describe(t)} is not running, so there is nothing to type into")


# --- reading and reaching the target ----------------------------------------


def _bridge_id(t: Target) -> Optional[str]:
    """The Remote Control session id now: the job record's, or the live record's."""
    if t.job:
        return roster.job_bridge_id(t.job, session_target.JOBS_DIR)
    return roster.pid_bridge_id(t.pid, sessions_dir=session_target.SESSIONS_DIR)


def _status(t: Target) -> Optional[str]:
    sample = watcher.read_status(t.pid)
    return sample[0] if sample else None


def _open_screen(t: Target):
    return permission_mode.open_session(session_target.resolve(t.pid))


def _refused(t: Target, reason: str, **extra: Any) -> dict:
    return {"ok": False, "refused": True, "session": t.label, "job": t.job,
            "reason": reason, **extra}


def _modal_refusal(t: Target, sess) -> Optional[dict]:
    """A refusal when the target is holding a modal, else None."""
    if _status(t) == "waiting":
        return _refused(t, f"{t.label} is waiting on an answer to a prompt, and a "
                           f"keystroke would answer it; nothing was sent",
                        kind="modal")
    marker = guards.modal_on_screen(sess.read())
    if marker:
        return _refused(t, f"{t.label} is showing a modal or permission prompt "
                           f"(seen: {marker!r}), and a keystroke would answer it; "
                           f"nothing was sent", kind="modal")
    return None


def _check_clear(t: Target) -> Optional[dict]:
    sess = _open_screen(t)
    try:
        return _modal_refusal(t, sess)
    finally:
        sess.close()


def _identify(t: Target, **extra: Any) -> dict:
    return {"session": t.label, "job": t.job, "self": t.is_self, **extra}


def _deliver(t: Target, text: str, *, what: str, attempts: int = inject.ATTEMPTS):
    """Type ``text`` into another session and submit it."""
    obs = observation.observation_for(t.pid)
    tail = obs.open_tail(t.pid)
    tail.watch(text)
    tail.poll()
    return inject.deliver(t.pid, text, what=what, tail=tail,
                          read_status=obs.read_status, attempts=attempts)


def _command(t: Target, text: str, *, what: str, confirm: bool = False,
             followup: Optional[str] = None, allow_modal: bool = False,
             change: Optional[pending.Change] = None,
             attempts: int = inject.ATTEMPTS) -> dict:
    """Submit ``text`` to the target; the caller's own gets a resume prompt."""
    if t.is_self:
        return {**inject.send(text, caller_pid=t.pid, confirm=confirm,
                              followup=followup, allow_modal=allow_modal,
                              change=change),
                "self": True, "job": t.job}
    refused = guards.refusal(text, confirm=confirm, allow_modal=allow_modal)
    if refused is not None:
        return refused
    result = _deliver(t, text, what=what, attempts=attempts)
    return {"ok": True, **_identify(t), "hosting": result.lane.hosting,
            "text": text, "submitted": result.submitted,
            "confirmed": result.confirmed}


def _slash(job: Optional[str], caller_pid: Any, text: str, what: str, *,
           change: Optional[pending.Change] = None,
           answers: Optional[tuple[str, str]] = None,
           spawn: Callable[[Callable[[], None]], Any] = inject._default_spawn
           ) -> dict:
    """Type a settings command. ``answers`` is (kind, value) for one that may ask to confirm."""
    t = resolve(job, caller_pid)
    _require_running(t)
    refused = _check_clear(t)
    if refused:
        return refused
    inline = answers is not None and not t.is_self and _between_turns(t)
    guard = _DefaultsGuard(answers)
    out = _command(t, text, what=what, change=change)
    if answers is None or not out.get("ok") or not out.get("submitted"):
        return out
    kind, value = answers
    if inline:
        dialog = _answer_switch_confirm(t, kind, value, MODAL_WAIT_S, POLL_S,
                                        before_press=guard.refresh)
        return _with_dialog(out, {**dialog, **guard.restore()})
    _answer_later(t, kind, value, change, spawn, guard)
    return {**out, "switch_dialog": {
        "ok": True, "answered": False, "pending": True,
        "detail": "the command runs when the turn ends, and the service then "
                  "answers Claude Code's confirm dialog"
                  + ("; a follow-up prompt reports the result" if t.is_self else "")}}


class _DefaultsGuard:
    """Holds the user's saved defaults while `/effort` or `/model` runs.

    Both save their value as the default for new sessions; see
    :mod:`settings_guard`. ``/effort max`` saves nothing, so it needs no guard.
    """

    def __init__(self, answers: Optional[tuple[str, str]]) -> None:
        self.on = answers is not None and answers != ("effort", "max")
        self.snap = settings_guard.snapshot() if self.on else None

    def refresh(self) -> None:
        """Re-read the defaults; the dialog can open long after the command went in."""
        if self.on:
            self.snap = settings_guard.snapshot() or self.snap

    def restore(self) -> dict:
        if not self.on:
            return {}
        out = settings_guard.restore(self.snap, sleep=_sleep, clock=_clock)
        return {"defaults_restored": out["restored"]} if out["restored"] else {}


def _with_dialog(out: dict, dialog: dict) -> dict:
    out = {**out, "switch_dialog": dialog}
    if not dialog["ok"]:
        out["ok"] = False
        out["reason"] = dialog["reason"]
    return out


def _promise_gone(pid: int, change: pending.Change) -> Callable[[], bool]:
    """True once the promise that carried ``change`` has been delivered."""
    seen = [False]

    def gone() -> bool:
        here = pending.has_change(pid, change.key)
        seen[0] = seen[0] or here
        return seen[0] and not here
    return gone


def _answer_later(t: Target, kind: str, value: str,
                  change: Optional[pending.Change], spawn,
                  guard: Optional["_DefaultsGuard"] = None) -> None:
    """Answer the confirm dialog from a detached thread, then record how it ended."""
    def finish() -> None:
        give_up = _promise_gone(t.pid, change) if change else None
        try:
            out = _answer_switch_confirm(
                t, kind, value, SELF_MODAL_WAIT_S, SELF_POLL_S, give_up=give_up,
                before_press=guard.refresh if guard else None)
        except Exception as exc:  # noqa: BLE001 - runs detached
            log.warning("reflection: answering the %s dialog of %s failed: %s",
                        kind, t.label, exc)
            out = {"ok": False, "reason": f"the service could not read the "
                                          f"session's screen ({exc})"}
        log.info("reflection: %s dialog of %s: %s", kind, t.label,
                 out.get("detail") or out.get("reason") or out.get("ok"))
        if change:
            pending.settle(t.pid, change.key,
                           pending.APPLIED if out["ok"] else pending.FAILED,
                           out.get("reason", ""))
        if guard:
            try:
                guard.restore()
            except Exception as exc:  # noqa: BLE001 - runs detached
                log.warning("reflection: could not restore the defaults after "
                            "%s: %s", kind, exc)

    spawn(finish)


# --- verbs: one-line commands -----------------------------------------------


def _change(kind: str, value: str) -> pending.Change:
    return pending.Change(kind=kind, value=value, key=uuid.uuid4().hex[:12])


def model(name: str, *, job: Optional[str], caller_pid: Any,
          spawn: Callable[[Callable[[], None]], Any] = inject._default_spawn) -> dict:
    if not isinstance(name, str) or not _MODEL.match(name):
        return {"ok": False, "reason": "model must be a model name or alias"}
    return _slash(job, caller_pid, f"/model {name}", "the model change",
                  change=_change("model", name), answers=("model", name),
                  spawn=spawn)


def effort(level: str, *, job: Optional[str], caller_pid: Any,
           spawn: Callable[[Callable[[], None]], Any] = inject._default_spawn) -> dict:
    if level not in EFFORTS:
        return {"ok": False, "reason": f"effort must be one of {', '.join(EFFORTS)}"}
    return _slash(job, caller_pid, f"/effort {level}", "the effort change",
                  change=_change("effort", level), answers=("effort", level),
                  spawn=spawn)


def rename(name: str, *, job: Optional[str], caller_pid: Any) -> dict:
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 80 or not name.isprintable() or name.startswith("-"):
        return {"ok": False, "reason": "name must be a short printable string on "
                                       "one line that does not start with '-'"}
    if "<warm" in name.lower():
        return {"ok": False, "reason": "a name may not carry the warm pool's mark"}
    return _slash(job, caller_pid, f"/rename {name}", "the rename",
                  change=_change("rename", name))


def message(job: Optional[str], text: str, *, caller_pid: Any,
            confirm: bool = False) -> dict:
    """Type ``text`` into another session's prompt, under the guards `message_self` has."""
    if not isinstance(job, str) or not job.strip():
        return {"ok": False, "reason": "job is required: the session to message"}
    refused = guards.refusal(text, confirm=confirm)
    if refused is not None:
        return refused
    t = resolve(job, caller_pid)
    _require_running(t)
    blocked = _check_clear(t)
    if blocked:
        return blocked
    return _command(t, text, what="the message", confirm=confirm)


def interrupt(*, job: Optional[str], caller_pid: Any) -> dict:
    """Press Escape once in the target, which interrupts a running turn."""
    t = resolve(job, caller_pid)
    _require_running(t)
    sess = _open_screen(t)
    try:
        refused = _modal_refusal(t, sess)
        if refused:
            return refused
        before = _status(t)
        sess.press("Escape")
    finally:
        sess.close()
    return {"ok": True, **_identify(t), "status_before": before}


# --- link --------------------------------------------------------------------


def _link_fields(t: Target, bridge: Optional[str]) -> dict:
    link = roster.bridge_link(bridge)
    out = {"remote_control": bool(bridge), "link": link}
    if link is None:
        out["link_reason"] = ("Remote Control is off" if not bridge
                              else "the bridge id is not in a form with an app link")
    return out


def link(*, job: Optional[str], caller_pid: Any) -> dict:
    """Read the target's Remote Control link from its record. Types nothing."""
    t = resolve(job, caller_pid)
    return {"ok": True, **_identify(t), "running": t.alive,
            **_link_fields(t, _bridge_id(t))}


# --- stop --------------------------------------------------------------------


def stop(job: Optional[str], *, caller_pid: Any, confirm: bool = False,
         spawn: Callable[[Callable[[], None]], Any] = inject._default_spawn,
         remove: Callable[..., dict] = cli.stop_and_remove) -> dict:
    """Stop a background session and remove its job.

    The caller's own session is refused without ``confirm``, and is stopped after
    this call has returned.
    """
    if not isinstance(job, str) or not job.strip():
        return {"ok": False, "reason": "job is required: the session to stop"}
    t = resolve(job, caller_pid)
    if t.kind != "bg" or not t.job:
        return _refused(t, f"{_describe(t)} is a terminal session, not a "
                           f"background job; close its terminal instead")
    if t.is_self and not confirm:
        return _refused(t, "that is your own session; stopping it ends this "
                           "conversation. Pass confirm=true to proceed.",
                        kind="self")
    if t.is_self:
        def later() -> None:
            _sleep(SELF_STOP_DELAY_S)
            try:
                remove(t.job)
            except Exception as exc:  # noqa: BLE001 - nobody is left to tell
                log.warning("reflection: stopping own session %s failed: %s",
                            t.job, exc)
        spawn(later)
        return {"ok": True, **_identify(t), "stopping": True}
    result = remove(t.job)
    out = {"ok": bool(result["removed"]), **_identify(t), **result}
    if not out["ok"]:
        out["reason"] = result["detail"] or "claude rm failed"
    return out


# --- Remote Control ----------------------------------------------------------


def remote_control(on: bool, *, name: Optional[str] = None, job: Optional[str],
                   caller_pid: Any,
                   spawn: Callable[[Callable[[], None]], Any] = inject._default_spawn
                   ) -> dict:
    """Turn the target's Remote Control on or off.

    On types ``/rc <name>`` and waits briefly for the link. Off types ``/rc``,
    which opens a modal, and selects "Disconnect this session" in it. Both ask
    the job record first: a bare ``/rc`` on a connected session opens that
    modal, so the state decides which command is safe to type.
    """
    t = resolve(job, caller_pid)
    _require_running(t)
    bridge = _bridge_id(t)
    if on:
        if name is not None and not _RC_NAME.match(str(name)):
            return {"ok": False, "reason": "name must be a plain label"}
        if bridge:
            return {"ok": True, **_identify(t), "changed": False,
                    "detail": "Remote Control is already on",
                    **_link_fields(t, bridge)}
        return _turn_on(t, name)
    if not bridge:
        return {"ok": True, **_identify(t), "changed": False,
                "detail": "Remote Control is already off", **_link_fields(t, None)}
    return _turn_off(t, spawn)


def _turn_on(t: Target, name: Optional[str]) -> dict:
    refused = _check_clear(t)
    if refused:
        return refused
    text = f"/rc {name}" if name else "/rc"
    if t.is_self:
        # The command queues behind this very turn, so there is nothing to wait
        # for here; the resume prompt carries the link.
        sent = _command(t, text, what="the Remote Control command",
                        change=_change("rc_on", name or ""), allow_modal=True)
        if not sent.get("ok"):
            return sent
        return {**sent, "link": None,
                "link_reason": "the command runs when your current turn ends; "
                               "a follow-up prompt will carry the link"}
    # One attempt: a second `/rc` would land in whatever the first opened.
    sent = _command(t, text, what="the Remote Control command",
                    allow_modal=True, attempts=1)
    link_found, reason = _await_link(t)
    out = {**sent, "remote_control": link_found is not None, "link": link_found}
    if link_found is None:
        out["link_reason"] = reason
    return out


def _await_link(t: Target) -> tuple[Optional[str], str]:
    deadline = _clock() + LINK_WAIT_S
    while True:
        found = roster.bridge_link(_bridge_id(t))
        if found:
            return found, ""
        if _clock() >= deadline:
            break
        _sleep(POLL_S)
    why = f"no bridge id in the job record after {LINK_WAIT_S:g}s"
    if _status(t) in ("busy", "shell"):
        why += "; the session is mid-turn, so the command may still be queued"
    return None, why


def _between_turns(t: Target) -> bool:
    sample = watcher.read_status(t.pid)
    if sample is None:
        return False
    tail = observation.observation_for(t.pid).open_tail(t.pid)
    tail.poll()
    return watcher.is_settled(sample[0], sample[1], tail)


def _turn_off(t: Target, spawn: Callable[[Callable[[], None]], Any]) -> dict:
    if t.is_self:
        return _turn_off_self(t, spawn)
    if not _between_turns(t):
        return _refused(t, f"{t.label} is mid-turn, so `/rc` would queue and "
                           f"open its modal later with nobody to answer it. "
                           f"Interrupt it or retry when it is idle.")
    refused = _check_clear(t)
    if refused:
        return refused
    error = None
    try:
        # One attempt: a second `/rc` + Enter would land in the modal the first opened.
        _deliver(t, "/rc", what="the Remote Control command", attempts=1)
    except inject.DeliveryError as exc:
        error = str(exc)
    return _drive_disconnect(t, wait_s=MODAL_WAIT_S, poll_s=POLL_S,
                             delivery_error=error)


def _turn_off_self(t: Target, spawn: Callable[[Callable[[], None]], Any]) -> dict:
    refused = _check_clear(t)
    if refused:
        return refused
    _deliver(t, "/rc", what="the Remote Control command", attempts=1)

    def finish() -> None:
        try:
            out = _drive_disconnect(t, wait_s=SELF_MODAL_WAIT_S, poll_s=SELF_POLL_S)
            log.info("reflection: self Remote Control off for %s: %s", t.label,
                     out.get("detail") or out.get("reason") or out.get("ok"))
            if out.get("modal_open") or out.get("refused"):
                return
            _deliver(t, SELF_OFF_FOLLOWUP, what="the resume prompt")
        except Exception as exc:  # noqa: BLE001 - runs detached
            log.warning("reflection: self Remote Control off for %s failed: %s",
                        t.label, exc)

    spawn(finish)
    return {"ok": True, **_identify(t), "changed": False, "remote_control": True,
            "detail": "`/rc` is queued behind this turn; when its modal opens "
                      "the service selects 'Disconnect this session' and then "
                      "resumes you"}


def selected_option(screen: str) -> Optional[str]:
    """The text of the option the cursor is on, from the latest paint of it."""
    found = _CURSOR.findall(guards.plain_screen(screen))
    return found[-1].strip() if found else None


def _direction(screen: str) -> Optional[str]:
    """Which key moves the cursor toward the disconnect option, or None if unknown."""
    lines = guards.plain_screen(screen).split("\n")
    target = [i for i, ln in enumerate(lines) if DISCONNECT_OPTION in ln.lower()]
    cursor = [i for i, ln in enumerate(lines) if _CURSOR.search(ln)]
    if not target or not cursor:
        return None
    return "Up" if cursor[-1] > target[-1] else "Down"


def _drive_disconnect(t: Target, *, wait_s: float, poll_s: float,
                      delivery_error: Optional[str] = None) -> dict:
    """Walk the Remote Control modal to "Disconnect this session" and confirm it.

    Refuses, and closes whatever is open with Escape, when the modal is not the
    one expected, so a wrong layout can never press Enter on a different option.
    """
    sess = _open_screen(t)
    try:
        screen = _await_modal(t, sess, wait_s, poll_s)
        if screen is None:
            return {"ok": False, **_identify(t), "remote_control": True,
                    "reason": delivery_error or (
                        f"the Remote Control modal did not appear within "
                        f"{wait_s:g}s; nothing was selected")}
        if DISCONNECT_OPTION not in guards.plain_screen(screen).lower():
            sess.press("Escape")
            return _refused(t, "a modal opened but it does not offer 'Disconnect "
                               "this session'; it was closed with Escape and "
                               "nothing was selected", kind="unexpected_modal",
                            modal_open=False)
        step = _walk_to_disconnect(sess, screen)
        if step is not None:
            sess.press("Escape")
            return _refused(t, step, kind="unexpected_modal", modal_open=False)
        sess.press("Enter")
        _sleep(KEY_SETTLE_S)
        cleared = _await_cleared(t)
        out = {"ok": cleared, **_identify(t), "remote_control": not cleared,
               "changed": cleared, "bridge_cleared": cleared,
               **_link_fields(t, None if cleared else _bridge_id(t))}
        if not cleared:
            out["reason"] = (f"'Disconnect this session' was selected but the "
                             f"bridge id did not clear within {CLEAR_WAIT_S:g}s")
            out["modal_open"] = guards.modal_on_screen(sess.read()) is not None
        return out
    finally:
        sess.close()


def _await_modal(t: Target, sess, wait_s: float, poll_s: float) -> Optional[str]:
    """The screen once a modal is on it, or None if none appears in ``wait_s``."""
    deadline = _clock() + wait_s
    while True:
        screen = sess.read()
        if DISCONNECT_OPTION in guards.plain_screen(screen).lower():
            return screen
        if guards.modal_on_screen(screen):
            return screen
        if _clock() >= deadline:
            return None
        _sleep(poll_s)


def _walk_to_disconnect(sess, screen: str) -> Optional[str]:
    """Move the cursor onto 'Disconnect this session'. A string is the reason it could not."""
    key = None
    for _ in range(MAX_MODAL_STEPS + 1):
        picked = selected_option(screen)
        if picked is not None and DISCONNECT_OPTION in picked.lower():
            return None
        if key is None:
            key = _direction(screen)
            if key is None:
                return ("the modal shows no selection cursor to steer by; it "
                        "was closed with Escape and nothing was selected")
        sess.forget()
        sess.press(key)
        _sleep(KEY_SETTLE_S)
        screen = sess.read()
    return (f"the cursor did not reach 'Disconnect this session' in "
            f"{MAX_MODAL_STEPS} steps; the modal was closed with Escape")


def _await_cleared(t: Target) -> bool:
    deadline = _clock() + CLEAR_WAIT_S
    while True:
        if not _bridge_id(t):
            return True
        if _clock() >= deadline:
            return False
        _sleep(POLL_S)


# --- the effort / model confirm dialog ---------------------------------------

SWITCH_TITLES = {"effort": "change effort level?", "model": "switch model?"}
# How long a dialog that offers a different switch is left for whoever asked
# for that one, before it is closed so the session is not left waiting.
WRONG_OFFER_GRACE_S = 6.0
_HOOK_ASKED = "a premodelswitch hook asked you to confirm"
_YES_SWITCH = re.compile(r"^(?:\d+\s*[.)]\s*)?yes,?\s+switch to\s+(.+)$", re.I)
_NO_OPTION = re.compile(r"^(?:\d+\s*[.)]\s*)?no\b", re.I)
_EFFORT_ALIASES = {"xhigh": ("xhigh", "extra high", "x-high")}
_WORDS = re.compile(r"[^a-z0-9]+")


def _offer_names(kind: str, value: str, offered: str) -> bool:
    """Whether "Yes, switch to <offered>" is the switch that was asked for."""
    offered = offered.strip().lower()
    if kind == "effort":
        names = _EFFORT_ALIASES.get(value.lower(), (value.lower(),))
        return any(re.match(re.escape(n) + r"(?![a-z0-9])", offered) for n in names)
    # A model is offered under its display name, which an alias or a dated id
    # only partly spells, so every word of the request must appear in the offer.
    base = re.sub(r"\[.*?\]", "", value.lower())
    words = [w for w in _WORDS.split(base)
             if w and w != "claude" and not (w.isdigit() and len(w) >= 8)]
    return bool(words) and set(words) <= set(_WORDS.split(offered))


def _read_switch_dialog(kind: str, value: str, dialog: str) -> tuple[str, str]:
    """(verdict, selected option) for the dialog text from its title down.

    The verdict is ``hook``, ``enter`` (Yes to the asked-for switch), ``no``,
    ``wrong`` (Yes to some other switch) or ``unclear``.
    """
    if _HOOK_ASKED in dialog.lower():
        return "hook", ""
    picked = selected_option(dialog) or ""
    yes = _YES_SWITCH.match(picked)
    if yes:
        return ("enter" if _offer_names(kind, value, yes.group(1)) else "wrong"), picked
    return ("no" if _NO_OPTION.match(picked) else "unclear"), picked


def _answer_switch_confirm(t: Target, kind: str, value: str, wait_s: float,
                           poll_s: float, *,
                           give_up: Optional[Callable[[], bool]] = None,
                           before_press: Optional[Callable[[], None]] = None
                           ) -> dict:
    """Answer Claude Code's "Change effort level?" / "Switch model?" dialog.

    Presses Enter only with the cursor on "Yes, switch to <value>". The cursor on
    "No" closes the dialog with Escape. A dialog a PreModelSwitch hook raised is
    left alone: that is the user's own gate. No dialog within ``wait_s`` is
    success, since a conversation without output switches without asking;
    ``give_up`` ends the wait early with that same answer, and ``before_press`` runs
    just before Enter. Returns ``ok``,
    ``answered`` and a ``detail`` or ``reason``.
    """
    title = SWITCH_TITLES[kind]
    sess = _open_screen(t)
    try:
        deadline = _clock() + wait_s
        wrong_since: Optional[float] = None
        verdict, picked = "", ""
        while True:
            screen = guards.current_screen(sess.read())
            at = screen.lower().rfind(title)
            verdict, picked = (_read_switch_dialog(kind, value, screen[at:])
                               if at >= 0 else ("", ""))
            if verdict == "enter":
                if before_press:
                    before_press()
                sess.press("Enter")
                _sleep(KEY_SETTLE_S)
                return {"ok": True, "answered": True, "dialog": True,
                        "detail": f"pressed Enter on {picked!r}"}
            if verdict == "hook":
                return {"ok": False, "answered": False, "dialog": True,
                        "reason": "a PreModelSwitch hook asked for the confirmation, "
                                  "so it was left for you to answer"}
            if verdict == "no":
                sess.press("Escape")
                return {"ok": False, "answered": False, "dialog": True,
                        "reason": f"the dialog's cursor was on {picked!r}, so it was "
                                  f"closed with Escape"}
            now = _clock()
            if verdict != "wrong":
                wrong_since = None
            elif wrong_since is None:
                wrong_since = now
            if wrong_since is not None and now - wrong_since >= WRONG_OFFER_GRACE_S:
                sess.press("Escape")
                return {"ok": False, "answered": False, "dialog": True,
                        "reason": f"the dialog offered {picked!r}, not a switch to "
                                  f"{value}; it was closed with Escape"}
            if (give_up and give_up()) or now >= deadline:
                break
            _sleep(poll_s)
        if verdict:
            return {"ok": False, "answered": False, "dialog": True,
                    "reason": f"the dialog's selection ({picked or 'none'!r}) was "
                              f"not one to answer; nothing was pressed"}
        other = guards.modal_on_screen(screen)
        if other:
            return {"ok": False, "answered": False, "dialog": False,
                    "reason": f"a different prompt was showing instead ({other!r}); "
                              f"nothing was pressed"}
        return {"ok": True, "answered": False, "dialog": False,
                "detail": "no confirm dialog appeared"}
    finally:
        sess.close()
