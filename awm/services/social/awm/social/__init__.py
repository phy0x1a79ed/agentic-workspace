"""AWM social service — multi-platform messaging connectors.

One service that sends and receives messages across external chat / mail
platforms (Discord, Slack today; Teams / Gmail / WeChat later) behind a single
``social_*`` MCP surface. It acts as one or more **named accounts** configured
in ``$AWM_DIR/social.toml`` and selected by an ``account`` parameter.
"""
