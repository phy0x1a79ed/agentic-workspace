"""awm.events — the events feature service: cron/interval-driven schedules that
emit ``schedule.tick`` for the agents service to wake bots on cadence. The hook
funnel (normalising realm/effector events into agent-wake notifications) layers
in next; this package ships the timer half."""
