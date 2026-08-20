"""The harness's model route, seeded once and then left alone.

The harness ships with no usable provider on this node: its default is DeepSeek
direct, and the credential this workspace actually holds is an OpenRouter key.
So the service declares OpenRouter as a custom ``openai-completions`` provider
in ``$DSH_HOME/settings.yaml`` on first start.

**Seeded, not owned.** The file is the user's — the GUI writes to it, and so
does anyone who edits it. This module adds the provider block when it is absent
and otherwise changes nothing, so a route tuned in the GUI survives every
restart and every deploy. Removing the block is therefore a way to say "don't".

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
from typing import Any

import yaml

from awm.dsh.harness import API_KEY_ENV, DSH_HOME, PROVIDER_ID, SETTINGS_FILE

log = logging.getLogger("awm.dsh.settings")

#: Settings sections are keyed by the *plugin id* in the composed profile, which
#: is where these two names come from — ``dsh --profile web --dump-config``
#: lists them. Guessing either produces a file the harness reads and ignores.
PLUGIN_KEY = "llm-pi-ai"
DEFAULT_MODEL_KEY = "agent-default-model"
BASE_URL = "https://openrouter.ai/api/v1"

#: What the model picker offers. The first is the workspace's habitual default
#: (opencode points at the same id); the others are a stronger DeepSeek and a
#: non-DeepSeek fallback, so a provider-side outage is not a dead harness.
MODELS = [
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "anthropic/claude-sonnet-5",
]


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
    """Add the OpenRouter provider if it is not already declared.

    Two sections, seeded independently: the provider route, and the default
    model selection that points at it.

    Returns what happened, so ``status`` can say whether the route was seeded on
    this start or was already there.
    """
    try:
        existing = yaml.safe_load(SETTINGS_FILE.read_text()) or {}
    except FileNotFoundError:
        existing = {}
    except (OSError, yaml.YAMLError) as exc:
        # A settings file this module cannot parse is a file it must not
        # rewrite: the harness may still read it, and clobbering someone's
        # hand-edit to fix our own convenience is the wrong trade.
        log.warning("dsh: leaving %s alone — cannot parse it: %s",
                    SETTINGS_FILE, exc)
        return {"changed": False, "error": f"{type(exc).__name__}: {exc}"}

    if not isinstance(existing, dict):
        log.warning("dsh: leaving %s alone — its top level is not a mapping",
                    SETTINGS_FILE)
        return {"changed": False, "error": "top level is not a mapping"}

    plugin = existing.setdefault(PLUGIN_KEY, {}) or {}
    providers = plugin.setdefault("providers", {}) or {}
    changed = []
    if PROVIDER_ID not in providers:
        providers[PROVIDER_ID] = provider_block()
        plugin["providers"] = providers
        existing[PLUGIN_KEY] = plugin
        changed.append("provider")

    # Declaring a route is not the same as selecting one. The profile ships a
    # default of deepseek-official/deepseek-v4-flash, and a harness left on it
    # fails every request with MISSING_CREDENTIAL against a key this workspace
    # does not hold — a working provider list and a dead harness.
    if DEFAULT_MODEL_KEY not in existing:
        existing[DEFAULT_MODEL_KEY] = {"provider": PROVIDER_ID, "model": MODELS[0]}
        changed.append("default_model")

    if not changed:
        return {"changed": False, "provider": PROVIDER_ID}

    DSH_HOME.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(yaml.safe_dump(existing, sort_keys=False))
    log.info("dsh: seeded %s in %s", " and ".join(changed), SETTINGS_FILE)
    return {"changed": True, "seeded": changed, "provider": PROVIDER_ID,
            "models": MODELS}
