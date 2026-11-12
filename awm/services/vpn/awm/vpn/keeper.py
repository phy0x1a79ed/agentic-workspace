"""Keep a profile's exit up without anyone asking: dial at start, re-dial when docker reports it died.

Every dial spends a Duo push, so a tunnel that keeps failing or dying young is
not re-dialed forever: after ``_MAX_SHORT_LIVES`` such dials in a row the keeper
stops and pages the operator. ``vpn up`` starts it again.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from typing import Callable

from awm import config as awm_config
from awm.vpn import config, container

log = logging.getLogger("awm.vpn.keeper")

KEPT: tuple[str, ...] = ("ubc",)
# Wait before a re-dial, by how many short lives came before it.
_BACKOFF_S = (5.0, 60.0, 300.0)
_MAX_SHORT_LIVES = 3
# A dial whose tunnel lasted this long resets the count of short lives.
_SETTLED_S = 600.0


def wait_for_death(profile: str) -> float:
    """Bring the exit up, block until docker reports its container died, and return how long it ran.

    The event stream opens before the dial, so a death in between is not missed.
    A stream that ends (the daemon restarted) returns as if the container died,
    and the next ``up`` finds it running without dialing. The life counts from
    docker's start time, so a tunnel adopted from an earlier service life keeps its age.
    """
    name = container.container_name(profile)
    events = subprocess.Popen(
        ["docker", "events", "--filter", f"container={name}", "--filter", "event=die",
         "--format", "{{.Action}}"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        container.up(profile)
        started = container.started_at(profile)
        events.stdout.readline()
        return time.time() - started
    finally:
        events.kill()
        events.wait()


def page(text: str) -> None:
    from awm import gatewayclient
    account, channel = awm_config.operator_dm()
    try:
        gatewayclient.call_sync("social", "send", {"account": account, "channel": channel, "text": text},
                                timeout=15)
    except Exception as exc:  # noqa: BLE001 — the keeper has stopped either way
        log.error("could not page the operator: %s (%s)", exc, text)


class Keeper:
    def __init__(self, profile: str, *, watch: Callable[[str], float] = wait_for_death,
                 alert: Callable[[str], None] = page) -> None:
        self.profile = profile
        self._watch, self._alert = watch, alert
        self._held = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def start(self) -> None:
        """Keep the exit up from now on, unless a keeper thread already does."""
        with self._guard:
            self._held.clear()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self.run, name=f"vpn-keeper-{self.profile}", daemon=True)
                self._thread.start()

    def hold(self) -> None:
        """Stop re-dialing; the caller takes the exit down."""
        self._held.set()

    def run(self) -> None:
        short_lives = 0
        while not self._held.is_set():
            try:
                lived = self._watch(self.profile)
                log.warning("vpn '%s' container died after %.0fs", self.profile, lived)
                last = "the container died"
            except Exception as exc:  # noqa: BLE001 — a failed dial is counted, not fatal
                log.warning("vpn '%s' dial failed: %s", self.profile, exc)
                lived, last = 0.0, str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            if self._held.is_set():
                return
            short_lives = 0 if lived >= _SETTLED_S else short_lives + 1
            if short_lives >= _MAX_SHORT_LIVES:
                self._alert(
                    f"🔌 vpn [{awm_config.node_name()}]: the `{self.profile}` tunnel failed or died young "
                    f"{short_lives} times in a row (last: {last}). I stopped re-dialing so it spends no "
                    f"more Duo pushes. Run `awm vpn up --profile {self.profile}` once it is fixed.")
                return
            self._held.wait(_BACKOFF_S[short_lives])


KEEPERS: dict[str, Keeper] = {p: Keeper(p) for p in KEPT}


def start_configured() -> None:
    """Start a keeper for each kept profile that vpn.toml configures."""
    for profile, keeper in KEEPERS.items():
        if config.configured(profile):
            keeper.start()
        else:
            log.info("vpn '%s' is not configured here; not keeping it up", profile)
