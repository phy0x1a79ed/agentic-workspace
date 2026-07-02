"""Unit tests for the reflection self-injection primitive.

All tmux calls go through an injected fake runner, so these exercise the argv
assembly + guard logic without a real tmux server.
"""
import subprocess

import pytest

from awm.reflection import tmux_inject


class FakeRunner:
    def __init__(self, returncode: int = 0):
        self.calls: list[tuple[list, dict]] = []
        self.returncode = returncode

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        stdout = "%32" if "display-message" in argv else ""
        return subprocess.CompletedProcess(argv, self.returncode,
                                           stdout=stdout, stderr="boom")

    def verbs(self) -> list[str]:
        # The tmux subcommand is the token after the binary (and optional -S sock).
        out = []
        for argv, _ in self.calls:
            rest = argv[1:]
            if rest[:1] == ["-S"]:
                rest = rest[2:]
            out.append(rest[0] if rest else "")
        return out

    def flat(self) -> list[str]:
        return [tok for argv, _ in self.calls for tok in argv]


@pytest.mark.smoke
def test_send_pastes_loads_and_submits():
    r = FakeRunner()
    res = tmux_inject.send("/compact", pane="%32", runner=r)
    assert res == {"ok": True, "pane": "%32", "text": "/compact", "submitted": True}
    # display-message (existence check) → load-buffer → paste-buffer → send-keys.
    assert r.verbs() == ["display-message", "load-buffer", "paste-buffer", "send-keys"]


@pytest.mark.smoke
def test_bracketed_paste_targets_pane():
    r = FakeRunner()
    tmux_inject.send("/compact", pane="%7", runner=r)
    paste = next(argv for argv, _ in r.calls if "paste-buffer" in argv)
    assert "-p" in paste           # bracketed paste, so a leading / is literal
    assert "-t" in paste and "%7" in paste


@pytest.mark.smoke
def test_never_sends_escape():
    # Escape would interrupt the in-flight turn; the whole point is to queue.
    r = FakeRunner()
    tmux_inject.send("/compact", pane="%32", runner=r)
    assert "Escape" not in r.flat()


@pytest.mark.smoke
def test_load_buffer_gets_text_on_stdin():
    r = FakeRunner()
    tmux_inject.send("/model opus", pane="%32", runner=r)
    lb = next(kw for argv, kw in r.calls if "load-buffer" in argv)
    assert lb.get("input") == b"/model opus"


@pytest.mark.smoke
def test_destructive_refused_without_confirm():
    r = FakeRunner()
    res = tmux_inject.send("/clear", pane="%32", runner=r)
    assert res["ok"] is False and res["refused"] is True
    assert "/clear" in res["reason"]
    assert r.calls == []           # nothing was pasted


@pytest.mark.smoke
def test_destructive_allowed_with_confirm():
    r = FakeRunner()
    res = tmux_inject.send("/clear", pane="%32", confirm=True, runner=r)
    assert res["ok"] is True
    assert "send-keys" in r.verbs()


@pytest.mark.smoke
def test_enter_false_skips_submit():
    r = FakeRunner()
    res = tmux_inject.send("draft text", pane="%32", enter=False, runner=r)
    assert res["submitted"] is False
    assert "send-keys" not in r.verbs()


@pytest.mark.smoke
def test_socket_is_threaded():
    r = FakeRunner()
    tmux_inject.send("/compact", pane="%32", socket="/tmp/s", runner=r)
    assert all(argv[1:3] == ["-S", "/tmp/s"] for argv, _ in r.calls)


@pytest.mark.smoke
def test_empty_text_raises():
    with pytest.raises(ValueError):
        tmux_inject.send("   ", pane="%32", runner=FakeRunner())


@pytest.mark.smoke
def test_tmux_failure_raises_tmuxerror():
    with pytest.raises(tmux_inject.TmuxError):
        tmux_inject.send("/compact", pane="%32", runner=FakeRunner(returncode=1))
