"""Backwards-compatibility shim — re-exports from scopes.py.

All functionality has moved to awm.services.scopes. This module exists
so that tests and any legacy code importing 'tasks' continue to work.
"""

from awm.services.scopes import (  # noqa: F401
    create_scope as create_task,
    update_scope as update_task,
    delete_scope as delete_task,
    list_scopes as list_tasks,
)
