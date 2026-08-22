"""The opencode lane: type into an opencode session, via tmux or serve HTTP.

OpenCode sessions come in the same two hostings as Claude Code's, and the
writer must speak to both — which is what makes this one module rather than a
fork:

* **tmux** — an interactive session in a pane. Paste the text and press Enter
  exactly the way :mod:`tmux_inject` does (same bracketed-paste, same no-Escape,
  same Ctrl-U clear on retry), so a leading ``/`` lands as literal text and a
  self-directed ``/compact`` queues behind the current turn instead of
  interrupting it. The pane is found the same way too: ``pane_for_pid``.
* **serve** — a background session under ``opencode serve``. There is no pane
  to paste into and no screen to read back; the injection is an HTTP POST to
  ``/session/{id}/message``. This is the direct analogue of the daemon lane:
  the write commits remotely, and confirmation comes from the session's own
  record (the DB), never from a read-back that cannot render.

The writer is held inside the same context-manager shape :mod:`inject` drives
for both Claude lanes — ``write`` / ``commit`` / ``clear`` / ``read_back`` and
the ``read_back_is_evidence`` flag that says whether a negative read means
anything. The serve lane's flag is ``False`` for exactly the daemon-lane
reason: there is no screen, so a probe that never shows up proves nothing.
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.request
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from awm.reflection import oc_session, tmux_inject

log = logging.getLogger("awm.reflection.oc_inject")

# Pause between the write and the read-back/commit, matching tmux_inject's
# settle so the serve POST does not race the submission.
_SETTLE_S = 0.15


class OpencodeError(RuntimeError):
    """An opencode injection attempt failed."""


class ServeError(OpencodeError):
    """The serve HTTP commit could not be delivered."""


class _ServeWriter:
    """A serve-backed session: the commit is an HTTP POST, nothing renders."""

    # There is no screen to read, so a silent read-back proves nothing — the
    # daemon-lane rule. Confirmation comes from the session's own record.
    read_back_is_evidence = False

    def __init__(self, lane: oc_session.OpencodeLane, *,
                 sleep: Callable[[float], None] = tmux_inject.time.sleep,
                 opener=None) -> None:
        self._lane = lane
        self._sleep = sleep
        self._buffer = ""
        # opener is a seam for tests: called as opener(url, data) and must
        # return a file-like object (urllib.response, or a fake).
        self._opener = opener or urllib.request.urlopen

    @property
    def label(self) -> str:
        return f"serve {self._lane.session_id}"

    def read_back(self) -> str:
        return ""

    def clear(self) -> None:
        # A serve session has no prompt box to wipe; a failed attempt is simply
        # not re-posted (the retry decides that upstream). Nothing to do.
        self._buffer = ""

    def write(self, text: str) -> None:
        self._buffer = text
        self._sleep(_SETTLE_S)

    def commit(self) -> None:
        url = f"{self._lane.serve_url}/session/{self._lane.session_id}/message"
        body = json.dumps({
            "parts": [{"type": "text", "text": self._buffer}],
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json"})
            with self._opener(req, timeout=15) as resp:
                status = getattr(resp, "status", 200)
                if status >= 400:
                    raise ServeError(f"{self.label}: serve POST returned "
                                     f"HTTP {status}")
        except (socket.timeout, TimeoutError) as exc:
            # The serve POST is synchronous: it returns when the turn finishes.
            # A timeout means the turn is running and has taken the line — the
            # message is in the DB (the session's own record confirms it, as it
            # does for the daemon lane) — not that delivery failed. So a timeout
            # is a successful submit, and the confirmation downstream reads the
            # record to prove it.
            log.info("reflection: serve POST to %s timed out after the turn "
                     "took the line (submit confirmed downstream from the "
                     "session record)", url)
        except OSError as exc:
            raise ServeError(f"could not reach {url}: {exc}") from exc
        self._buffer = ""


@contextmanager
def open_lane(lane: oc_session.OpencodeLane, *, opener=None, sleep=None,
              **kw) -> Iterator:
    """Open ``lane`` for writing, whichever transport it happens to be.

    ``kw`` (socket/runner/sleep) is only consulted on the tmux path — it is the
    same seam the claude tmux lane threads for tests. The serve path takes an
    ``opener`` seam instead.
    """
    if lane.serve_url:
        yield _ServeWriter(lane, opener=opener,
                           sleep=sleep or tmux_inject.time.sleep)
        return
    if lane.pane:
        tkw = {k: v for k, v in kw.items()
               if k in ("socket", "runner", "sleep")}
        with tmux_inject.open_lane(lane, **tkw) as writer:
            yield writer
        return
    raise OpencodeError("opencode lane has neither a pane nor a serve_url")