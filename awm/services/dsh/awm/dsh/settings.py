"""The harness's model route: seeded once, and steerable from awm afterwards.

The harness ships with no usable provider on this node: its default is DeepSeek
direct, and the credential this workspace actually holds is an OpenRouter key.
So the service declares OpenRouter as a custom ``openai-completions`` provider
in ``$DSH_HOME/settings.yaml`` on first start.

**Seeded, not owned.** The file is the user's — the harness writes to it, and so
does anyone who edits it. :func:`ensure` adds a section when it is absent and
otherwise changes nothing, so a route tuned elsewhere survives every restart and
every deploy. Removing a section is therefore a way to say "don't".

**Why awm can steer it at all.** The harness's own Settings UI is unreachable
from a mesh browser: the client decides from ``location.hostname`` alone whether
settings are available, so a page served on a mesh address gets an in-memory
mirror that never loads and the Models page reports "settings are unavailable in
this browser". No proxy header changes that — it is a client-side test, not a
server fence. The route is the one thing anybody needs from that page, so
:func:`set_default_model` gives it a verb instead. ``dsh-settings-file`` watches
the document and hot-publishes external edits, so a write here reaches a running
harness without a restart.

**Writes take the harness's own lock.** ``dsh-settings-file`` guards its
read-modify-write with a ``<file>.lock`` sibling created ``O_EXCL``, backing off
to a 2 s deadline and never stealing an existing lock (age cannot distinguish a
crashed owner from a paused writer). Writing without it would race a concurrent
harness write and silently drop one side, so this module speaks the same
protocol. It does *not* reproduce the harness's leaf-level YAML diffing: a write
here re-renders the parsed document, so comments a human added do not survive.

**The key is referenced, never stored.** ``apiKeyEnv`` names an environment
variable; the supervisor reads the value out of opencode's ``auth.json`` at
spawn and passes it in. The key stays in the one file that already owns it, and
this settings file — which is plain text in a state directory — never contains
a credential.

The two ``compat`` corrections are the ones upstream names as needed by most
OpenAI-compatible gateways: OpenRouter has no ``developer`` role, and expects
``max_tokens`` rather than ``max_completion_tokens``.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Any

import yaml

from awm.dsh.harness import (
    API_KEY_ENV,
    DEFAULT_MODEL_KEY,
    DSH_HOME,
    PLUGIN_KEY,
    PROVIDER_ID,
    SETTINGS_FILE,
)

log = logging.getLogger("awm.dsh.settings")

BASE_URL = "https://openrouter.ai/api/v1"

#: How long to wait for the document lock before giving up. The harness uses the
#: same deadline; a contender that waited longer would be betting that the holder
#: crashed, which it cannot know.
LOCK_TIMEOUT_S = 2.0

#: What the model picker offers. The first is the workspace's habitual default
#: (opencode points at the same id); the others are a stronger DeepSeek and a
#: non-DeepSeek fallback, so a provider-side outage is not a dead harness.
MODELS = [
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "anthropic/claude-sonnet-5",
]


class SettingsUnavailable(RuntimeError):
    """The document could not be read or written — never a partial write."""


@contextmanager
def _document_lock():
    """Hold the ``<settings>.lock`` sibling ``dsh-settings-file`` writes under.

    Exclusive-create with backoff, and on timeout give up rather than remove
    somebody else's lock: a stale lock and a live writer look identical from
    here, and stealing turns a delay into a lost write.
    """
    lock = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".lock")
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    delay = 0.01
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise SettingsUnavailable(
                    f"{lock} is held; the harness is mid-write, or an earlier "
                    f"writer crashed and the lock needs removing by hand")
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
        except OSError as exc:
            raise SettingsUnavailable(f"cannot lock {lock}: {exc}") from exc
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:  # pragma: no cover — someone removed it under us
            pass


def read_document() -> dict[str, Any]:
    """The settings document, or ``{}`` when there is none.

    Raises rather than returning ``{}`` for a file that exists and does not
    parse: treating an unreadable document as an empty one is how a write
    silently deletes somebody's configuration.
    """
    try:
        text = SETTINGS_FILE.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SettingsUnavailable(f"cannot read {SETTINGS_FILE}: {exc}") from exc
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise SettingsUnavailable(f"{SETTINGS_FILE} does not parse: {exc}") from exc
    if not isinstance(doc, dict):
        raise SettingsUnavailable(f"{SETTINGS_FILE} top level is not a mapping")
    return doc


def _write_document(doc: dict[str, Any]) -> None:
    """Render and rename into place. Caller holds the lock."""
    DSH_HOME.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(DSH_HOME), prefix=".settings-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(yaml.safe_dump(doc, sort_keys=False))
        os.chmod(tmp, 0o600)
        # Atomic: a reader never sees a half-rendered document, which is why
        # the harness's readers can skip the lock entirely.
        os.replace(tmp, SETTINGS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def provider_block() -> dict[str, Any]:
    return {
        "apiKeyEnv": API_KEY_ENV,
        "api": "openai-completions",
        "baseURL": BASE_URL,
        "compat": {
            "supportsDeveloperRole": False,
            "maxTokensField": "max_tokens",
        },
        "models": [{"id": model} for model in MODELS],
    }


def ensure() -> dict[str, Any]:
    """Add the OpenRouter provider and the default model selection if absent.

    Two sections, seeded independently, because they fail differently: a
    declared route nothing selects is a working provider list and a dead
    harness, and a selection pointing at an undeclared route is the reverse.

    Returns what happened, so ``status`` can say whether the route was seeded on
    this start or was already there.
    """
    try:
        with _document_lock():
            doc = read_document()
            seeded = _seed(doc)
            if seeded:
                _write_document(doc)
    except SettingsUnavailable as exc:
        # A settings file this module cannot read is a file it must not
        # rewrite: the harness may still be serving from it, and clobbering a
        # hand edit to fix our own convenience is the wrong trade.
        log.warning("dsh: leaving %s alone — %s", SETTINGS_FILE, exc)
        return {"changed": False, "error": str(exc)}
    if seeded:
        log.info("dsh: seeded %s in %s", " and ".join(seeded), SETTINGS_FILE)
    return {"changed": bool(seeded), "seeded": seeded,
            "provider": PROVIDER_ID, "models": MODELS}


def _seed(doc: dict[str, Any]) -> list[str]:
    """Fill in the missing sections in place; return which ones were added."""
    seeded = []
    plugin = doc.setdefault(PLUGIN_KEY, {}) or {}
    providers = plugin.setdefault("providers", {}) or {}
    if PROVIDER_ID not in providers:
        providers[PROVIDER_ID] = provider_block()
        plugin["providers"] = providers
        doc[PLUGIN_KEY] = plugin
        seeded.append("provider")

    # Declaring a route is not selecting one. The profile ships a default of
    # deepseek-official/deepseek-v4-flash, and a harness left on it fails every
    # request with MISSING_CREDENTIAL against a key this workspace does not hold.
    if DEFAULT_MODEL_KEY not in doc:
        doc[DEFAULT_MODEL_KEY] = {"provider": PROVIDER_ID, "model": MODELS[0]}
        seeded.append("default_model")
    return seeded


def declared_models(doc: dict[str, Any] | None = None) -> list[str]:
    """Model ids the OpenRouter route advertises, in declared order."""
    doc = read_document() if doc is None else doc
    provider = ((doc.get(PLUGIN_KEY) or {}).get("providers") or {}).get(PROVIDER_ID) or {}
    return [m.get("id") for m in (provider.get("models") or []) if m.get("id")]


def selection(doc: dict[str, Any] | None = None) -> dict[str, Any]:
    """The current default model selection, as the harness reads it."""
    doc = read_document() if doc is None else doc
    return dict(doc.get(DEFAULT_MODEL_KEY) or {})


def set_default_model(model: str, *, declare: bool = False,
                      reasoning: str | None = None) -> dict[str, Any]:
    """Point the default selection at ``model`` on the OpenRouter route.

    ``declare`` also adds the id to the route's catalog. Off by default: a
    selection naming a model the route does not advertise is the exact failure
    this verb exists to prevent, so making it up silently would be perverse —
    but the harness does not validate catalog membership either, and a route may
    serve a model newer than the list, so it stays available deliberately.

    The selection is **edited in place**, not replaced. The harness writes this
    section too and puts ``reasoningEffort`` in it, so replacing the mapping
    would silently drop an effort the user chose — and any key a later version
    adds. ``reasoning`` sets that effort explicitly; clearing one is the
    harness's own business (it knows which efforts a model has) and is left to
    the Settings UI or a hand edit.

    The harness watches the document, so this takes effect without a restart.
    """
    model = (model or "").strip()
    if not model:
        raise ValueError("model is required")
    with _document_lock():
        doc = read_document()
        _seed(doc)
        known = declared_models(doc)
        if model not in known:
            if not declare:
                raise ValueError(
                    f"{model!r} is not declared on the {PROVIDER_ID} route "
                    f"({', '.join(known) or 'none'}). Pass declare=true to add it.")
            provider = doc[PLUGIN_KEY]["providers"][PROVIDER_ID]
            provider.setdefault("models", []).append({"id": model})
        before = selection(doc)
        selected = doc[DEFAULT_MODEL_KEY]
        selected["provider"] = PROVIDER_ID
        selected["model"] = model
        if reasoning:
            selected["reasoningEffort"] = reasoning
        _write_document(doc)
    log.info("dsh: default model %s -> %s", before.get("model"), model)
    return {"changed": selection() != before, "previous": before.get("model"),
            "provider": PROVIDER_ID, "model": model,
            "selection": selection(), "models": declared_models()}
