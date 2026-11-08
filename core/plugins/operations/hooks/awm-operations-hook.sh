#!/bin/sh
# Claude Code hook: the operations binary reports the session's plan, todos and prompts.
exec "${AWM_DIR:-$HOME/agentic_workspace/.awm}/bin/operations" hook
