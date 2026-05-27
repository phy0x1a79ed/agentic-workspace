"""Per-peer Discord bot for one-shot login DMs.

Opt-in: the bot only starts if ``$AWM_DIR/discord.toml`` exists and
contains a bot token. When started, it registers a ``/login`` slash
command. On invocation, the user's Discord ID is checked against the
``discord_operators`` table; if whitelisted, the daemon mints a
single-use login nonce and DMs the user the bootstrap URL.

See :mod:`awm.services.discord.bot` for the bot itself,
:mod:`awm.services.discord.operators` for the whitelist CRUD, and
:mod:`awm.services.discord.config` for the config-file shape.
"""
