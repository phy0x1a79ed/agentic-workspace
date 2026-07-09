"""AWM notes service — markdown notebook backend.

Each note is a uuid-named ``.md`` file on disk; the DB owns the title-as-path
tree, the FTS/embedding search indexes, a 30-day soft-delete trash, and the
custom dictation vocabulary. See :mod:`awm.notes.notes` for the application
logic and :mod:`awm.notes.hub_adapter` for the gateway surface.
"""
