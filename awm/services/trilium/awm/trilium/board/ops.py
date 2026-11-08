"""The Ops note: telemetry's roster rendered one way into the vault (fleet.md R147 items 3-7).

Telemetry holds the facts and this renders them. The page reads one JSON note, so the New agent
dropdown on the board and the Ops cards agree on the same roster. A render runs on each
``telemetry/roster`` event and never on a timer.
"""

from __future__ import annotations

import json
import os
from typing import Any

from awm.trilium.board import ids
from awm.trilium.board.fence import Fence
from awm.trilium.board.remote import Remote
from awm.trilium.board.render import text

#: Nodes that never take an agent from the New agent button (fleet.md R79, R88).
NEVER_AGENT_NODES = frozenset({"deneb"})
NEVER_AGENT_ROLES = frozenset({"station"})


def portals_from_env() -> dict[str, str]:
    """``AWM_TRILIUM_PORTALS=host=node,host=node``: each box's portal, for nodes whose
    telemetry row names none."""
    out: dict[str, str] = {}
    for pair in os.environ.get("AWM_TRILIUM_PORTALS", "").split(","):
        host, _, node = pair.partition("=")
        if host.strip() and node.strip():
            out[host.strip()] = node.strip()
    return out


def _node(row: dict[str, Any], portals: dict[str, str]) -> dict[str, Any]:
    """One ``telemetry nodes`` row, as the pages read it. ``uses`` are the tools the node can use."""
    name = text(row.get("node") or row.get("name"))
    portal = text(row.get("portal"))
    if not portal:
        host = next((h for h, n in portals.items() if n == name), "")
        portal = f"https://{host}/" if host else ""
    sessions = [{"name": text(s.get("name") or s.get("job")), "rc_url": text(s.get("link") or s.get("rc_url")),
                 "scope": text(s.get("scope")), "project": text(s.get("project")),
                 "task_id": text(s.get("task_id")), "kind": text(s.get("kind")),
                 "remote_control": bool(s.get("remote_control"))}
                for s in row.get("sessions") or [] if isinstance(s, dict)]
    uses = row.get("uses") if row.get("uses") is not None else row.get("services")
    return {"name": name, "role": text(row.get("role")), "portal": portal,
            "last_seen": row.get("last_seen"), "sessions": sessions,
            "sessions_7d": int(row.get("sessions_7d") or 0),
            "services": [{"service": text(u.get("tool") or u.get("service")),
                          "last_checked": u.get("last_checked"), "last_used": u.get("last_used")}
                         for u in uses or [] if isinstance(u, dict) and u.get("available", True)]}


def agent_nodes(nodes: list[dict[str, Any]]) -> list[str]:
    """The nodes the New agent dropdown lists: fewest sessions in 7 days first, no stations,
    no deneb."""
    ok = [n for n in nodes if n["name"] and n["name"] not in NEVER_AGENT_NODES
          and n["role"] not in NEVER_AGENT_ROLES]
    return [n["name"] for n in sorted(ok, key=lambda n: (n["sessions_7d"], n["name"]))]


def build_roster(nodes: list[dict], services: list[dict], seats: list[dict],
                 notices: list[dict], portals: dict[str, str]) -> dict[str, Any]:
    cards = [_node(n, portals) for n in nodes]
    return {
        "nodes": sorted(cards, key=lambda n: n["name"]),
        "agent_nodes": agent_nodes(cards),
        "services": [{"service": text(s.get("tool") or s.get("service")), "host": text(s.get("host")),
                      "health": text(s.get("health")) or "unknown",
                      "last_response": s.get("last_response"), "last_used": s.get("last_used")}
                     for s in services],
        "seats": [{"seat": text(s.get("seat") or s.get("id")),
                   "label": text(s.get("label") or s.get("name")) or text(s.get("seat") or s.get("id")),
                   "state": text(s.get("state")),
                   "remote_control": bool(s.get("remote_control")),
                   "rc_url": text(s.get("rc_url")), "node": text(s.get("node"))}
                  for s in seats],
        "notices": [{"kind": text(n.get("kind")), "node": text(n.get("node")),
                     "title": text(n.get("title")), "body": text(n.get("body")),
                     "ts": n.get("ts")} for n in notices],
    }


def portals_text(portals: dict[str, str], nodes: list[dict]) -> str:
    """The Portals note: one link per box, with no password (fleet.md R81)."""
    links = {n["name"]: n["portal"] for n in nodes if n["portal"]}
    for host, node in portals.items():
        links.setdefault(node, f"https://{host}/")
    rows = [f"- [{node}]({url})" for node, url in sorted(links.items())]
    return ("# Portals\n\nEach box's portal. Sign in there with your passkey.\n\n"
            + ("\n".join(rows) or "No portal is configured.") + "\n")


#: Which telemetry verb answers each part a ``telemetry/roster`` event can name.
REFRESH = {"nodes": "nodes", "sessions": "nodes", "usage": "nodes", "services": "services",
           "seats": "seats"}


class OpsRenderer:
    def __init__(self, fence: Fence, remote: Remote, portals: dict[str, str] | None = None) -> None:
        self.fence, self.remote = fence, remote
        self.portals = dict(portals if portals is not None else portals_from_env())
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def render(self, changed: list[str] | None = None) -> dict[str, Any]:
        """Read the changed parts of the roster and write the vault: the JSON note, Portals and one
        note per notice. A roster event names what changed, and only those verbs are called; a
        render with no ``changed`` (a fresh connection) reads everything."""
        wanted = {REFRESH[c] for c in changed or [] if c in REFRESH}
        for part in ("nodes", "services", "seats"):
            if changed is None or part in wanted or part not in self.rows:
                self.rows[part] = getattr(self.remote, part)()
        roster = build_roster(self.rows["nodes"], self.rows["services"], self.rows["seats"],
                              self.remote.ops_list(), self.portals)
        self._upsert(ids.ROSTER_ID, "roster", "application/json",
                     json.dumps(roster, sort_keys=True))
        self._upsert(ids.PORTALS_ID, "Portals", ids.MARKDOWN,
                     portals_text(self.portals, roster["nodes"]))
        self._notices(roster["notices"])
        return roster

    def _upsert(self, note_id: str, title: str, mime: str, content: str) -> None:
        if self.fence.note(note_id) is None:
            self.fence.create(ids.OPS_ID, title, "code", content, note_id=note_id, mime=mime)
        else:
            self.fence.update(note_id, title=title, mime=mime, content=content)

    def _notices(self, notices: list[dict]) -> None:
        """One managed note per kind and node, as the first Ops book kept them."""
        fixed = {ids.ROSTER_ID, ids.PORTALS_ID, ids.OPS_PAGE_ID}
        have = {k.get("title"): k.get("noteId") for k in self.fence.children(ids.OPS_ID)
                if k.get("noteId") not in fixed
                and any(a.get("name") == "agentManaged" for a in k.get("attributes") or [])}
        for n in notices:
            title = f"{n['kind']} - {n['node']}"
            body = f"## {n['title']}\n\n{n['body']}\n"
            if title in have:
                self.fence.update(have[title], content=body, mime=ids.MARKDOWN)
            else:
                self.fence.create(ids.OPS_ID, title, "code", body, mime=ids.MARKDOWN,
                                  labels={**ids.MANAGED, "swarmNotice": ""})
