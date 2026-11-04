"""awm.dvcdata — the DVC data layer a scope's code and data share.

``data_dvc`` wires a project's worktrees to the shared cache and keeps their
data checked out; ``cachetag`` reads the marker that opts a directory out.
The scopes and dvc services each pin this library in their own env rather
than importing one another.
"""
