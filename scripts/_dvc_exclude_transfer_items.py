#!/usr/bin/env python3
"""Partition a directory tree into the minimal set of transfer items that
covers everything except an explicit set of excluded paths.

Globus Transfer filter_rules only match by item *name*, not full path, so a
name like "assembly" or "hosts" can't be excluded without also excluding
unrelated directories elsewhere in the tree that happen to share that name.
This script does it precisely instead: walk top-down, and at each directory
either emit it whole (nothing excluded beneath it), skip it whole (it IS an
excluded path), or recurse into it (something excluded lies beneath it).

Reads a JSON array of absolute excluded paths on stdin, plus WS_ROOT and
DEST_PREFIX as argv. Writes a JSON array of
{source_path, destination_path, recursive} to stdout.
"""
import json
import os
import sys


def partition(root, excluded, dest_prefix):
    items = []
    excluded = {os.path.normpath(p) for p in excluded}
    # directories that have an excluded path somewhere beneath them
    ancestors_of_excluded = set()
    for p in excluded:
        d = os.path.dirname(p)
        while d and d != root and d not in ancestors_of_excluded:
            ancestors_of_excluded.add(d)
            d = os.path.dirname(d)
        ancestors_of_excluded.add(root)

    def walk(dir_path):
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except FileNotFoundError:
            return
        for entry in entries:
            full = os.path.normpath(entry.path)
            if full in excluded:
                continue
            rel = os.path.relpath(full, root)
            dest = os.path.join(dest_prefix, rel)
            if entry.is_dir(follow_symlinks=False):
                if full in ancestors_of_excluded:
                    walk(full)
                else:
                    items.append({
                        "source_path": full + "/",
                        "destination_path": dest + "/",
                        "recursive": True,
                    })
            else:
                items.append({
                    "source_path": full,
                    "destination_path": dest,
                    "recursive": False,
                })

    walk(root)
    return items


if __name__ == "__main__":
    ws_root = os.path.normpath(sys.argv[1])
    dest_prefix = sys.argv[2]
    excluded = json.load(sys.stdin)
    result = partition(ws_root, excluded, dest_prefix)
    json.dump(result, sys.stdout)
