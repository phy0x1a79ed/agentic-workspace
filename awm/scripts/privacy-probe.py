"""Check, from this node, that a project on a peer is visible or hidden to peer calls.

Every check reaches the peer the way an agent here does: through this node's
gateway (the ``search`` fan-out), or straight at the peer's edge with this
node's peer credential. Setup reads that must see the project whatever the mark
says (a journal id, a scope, a file) run on the peer itself over ssh, as a local
caller there.

Only the gate's own refusal counts as hidden: ``Unknown verb '<v>' for domain
'<d>'``, ``Unknown tool: <name>``, ``{"error": "not found"}`` or a ``/files``
404. It counts only when the same call naming a control project passed earlier
in the run, so a service that is down or lacks the verb fails instead of passing.
Run ``privacy-probe.sh --help`` for the options.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import httpx

from awm import gatewayclient

FREE_TEXT = {"title", "snippet", "text", "body", "content", "rendered", "description",
             "detail", "message", "query", "objective", "note", "error", "reason",
             "modified_sample", "untracked_sample", "claimed_author", "context",
             "question", "decision", "fallback", "stop_line", "noise", "cmdline"}
# Search sources whose documents carry no project field, so no mark can hide them.
OUT_OF_SCOPE_SOURCES = {"precedence", "kb", "writing", "notes", "trilium"}
SVC_NOT_FOUND = {"error": "not found"}
TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class Refused(Exception):
    """The peer answered with the gate's refusal, or with a not-found that proves as much."""


class Mismatch(Exception):
    """A check saw what its expectation forbids."""


def slug(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def parse_result(result: Any) -> Any:
    if isinstance(result, str) and result.lstrip()[:1] in ("{", "["):
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return result
    return result


def gate_refused(resp: Any, refusal: str) -> bool:
    try:
        body = json.loads(resp.text)
    except ValueError:
        return False
    return body == SVC_NOT_FOUND or (isinstance(body, dict) and body.get("detail") == refusal)


class Probe:
    def __init__(self, opts: argparse.Namespace) -> None:
        self.o = opts
        self.peer, self.project = opts.peer, opts.project
        self.hidden = opts.expect == "hidden"
        self.base = gatewayclient.hub_base_url()
        self.entry: dict[str, Any] = {}
        self.root = self.project_dir = ""
        cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        self.cache_path = cache_home / "awm" / f"privacy-probe-{self.peer}-{self.project}.json"
        self.facts: dict[str, Any] = {}
        self.control: dict[str, Any] = {}
        self.controls_passed: set[str] = set()
        self.skipped: list[str] = []
        self.warnings = 0
        self.label = ""

    # -- transport -----------------------------------------------------------

    def local_invoke(self, name: str, args: dict) -> Any:
        resp = httpx.post(f"{self.base}/invoke", json={"name": name, "args": args}, timeout=TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"local gateway {name}: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def edge_invoke(self, name: str, args: dict, refusal: str, timeout: float) -> Any:
        entry = self.peer_entry()
        edge = entry["edge_url"].rstrip("/")

        def send(bearer: str) -> httpx.Response:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                              verify=gatewayclient._peer_ca()) as cli:
                return cli.post(f"{edge}/invoke", json={"name": name, "args": args},
                                headers={"Authorization": f"Bearer {bearer}"})

        resp = gatewayclient.peer_send_sync(self.peer, entry, send)
        if resp.status_code == 404 and gate_refused(resp, refusal):
            raise Refused(refusal)
        if resp.status_code >= 400:
            raise RuntimeError(f"{name} on {self.peer}: HTTP {resp.status_code}: {resp.text[:300]}")
        return parse_result(resp.json().get("result"))

    def peer_call(self, tool: str, verb: str, args: dict, *, timeout: float = 120.0) -> Any:
        """``tool.verb(args)`` on the peer's edge."""
        return self.edge_invoke(tool, {"verb": verb, "args": args},
                                f"Unknown verb {verb!r} for domain {tool!r}", timeout)

    def peer_flat(self, name: str, args: dict, *, timeout: float = 120.0) -> Any:
        """The flat tool ``name`` on the peer's edge, for verbs the domain surface hides."""
        return self.edge_invoke(name, args, f"Unknown tool: {name}", timeout)

    def peer_file(self, path: str) -> SimpleNamespace:
        """The status of ``/files<path>`` on the peer; the body is read only when it is short."""
        entry = self.peer_entry()
        url = entry["edge_url"].rstrip("/") + "/files" + urllib.parse.quote(path)

        def send(bearer: str) -> SimpleNamespace:
            with httpx.Client(timeout=TIMEOUT, verify=gatewayclient._peer_ca(),
                              follow_redirects=False) as cli:
                with cli.stream("GET", url, headers={"Authorization": f"Bearer {bearer}"}) as resp:
                    text = "" if resp.status_code == 200 else resp.read().decode(errors="replace")[:300]
                    return SimpleNamespace(status_code=resp.status_code, text=text,
                                           size=resp.headers.get("content-length", "?"))

        return gatewayclient.peer_send_sync(self.peer, entry, send)

    def on_peer(self, tool: str, verb: str, args: dict) -> Any:
        """``tool.verb(args)`` run on the peer as a local caller there, over ssh."""
        body = json.dumps({"name": tool, "args": {"verb": verb, "args": args}})
        cmd = (f"curl -sS -X POST http://127.0.0.1:{self.o.peer_port}/invoke "
               "-H 'content-type: application/json' --data-binary @-")
        out = self.ssh(cmd, stdin=body, timeout=120)
        reply = json.loads(out)
        if "result" not in reply:
            raise RuntimeError(f"{tool}.{verb} on {self.peer} over ssh: {out[:300]}")
        return parse_result(reply["result"])

    def ssh(self, cmd: str, *, stdin: str | None = None, timeout: int = 60) -> str:
        alias = self.peer_entry().get("ssh_alias") or self.peer
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", alias, cmd], input=stdin,
                              capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(f"ssh {alias}: {proc.stderr.strip()[:300]}")
        return proc.stdout.strip()

    def peer_entry(self) -> dict:
        if not self.entry:
            self.entry = gatewayclient.resolve_peer(self.peer)
        return self.entry

    # -- matching ------------------------------------------------------------

    def names_project(self, project: Any, scope: Any = None, name: str | None = None) -> bool:
        p = name or self.project
        if project in (p, f"task:{p}"):
            return True
        return not project and scope in (f"project:{p}", f"user:proj:{p}")

    def under_project(self, path: Any) -> bool:
        if not isinstance(path, str) or not path:
            return False
        if not path.startswith("/"):
            path = f"{self.root}/{path}"
        return path == self.project_dir or path.startswith(self.project_dir + "/")

    def path_mentioned(self, text: Any) -> bool:
        return isinstance(text, str) and re.search(
            re.escape(self.project_dir) + r"(?![A-Za-z0-9_.-])", text) is not None

    def slug_of_project(self, name: Any) -> bool:
        prefix = slug(self.project_dir)
        return isinstance(name, str) and (name == prefix or name.startswith(prefix + "-"))

    def leak(self, data: Any) -> str | None:
        """Where the project name shows outside free text, or None."""
        pattern = re.compile(re.escape(self.project), re.I)

        def walk(node: Any, path: str) -> str | None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k in FREE_TEXT:
                        continue
                    if pattern.search(str(k)):
                        return f"{path}.{k}"
                    hit = walk(v, f"{path}.{k}")
                    if hit:
                        return hit
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    hit = walk(v, f"{path}[{i}]")
                    if hit:
                        return hit
            elif isinstance(node, str) and pattern.search(node):
                return f"{path}={node[:80]!r}"
            return None

        return walk(data, "$")

    def mentions(self, data: Any) -> bool:
        return re.search(re.escape(self.project), json.dumps(data), re.I) is not None

    # -- reporting -----------------------------------------------------------

    def skipping(self, label: str) -> bool:
        if label.split()[0].split(".")[0] in (self.o.skip or []):
            self.skip(label, "--skip")
            return True
        return False

    def skip(self, label: str, why: str) -> None:
        if label.split()[0] not in self.skipped:
            self.skipped.append(label.split()[0])
        print(f"SKIP {label}: {why}")

    def warn(self, why: str) -> None:
        self.warnings += 1
        print(f"WARN {self.label}: {why}")

    def check(self, label: str, run: Callable[[], tuple[bool, Any, str]], *,
              control: str | None = None, may_be_vacuous: bool = False) -> None:
        """Run one check. ``run`` returns (saw the project, the data, what it saw).

        A refusal passes a hidden run only for a check whose ``control`` passed
        earlier. A ``may_be_vacuous`` check that sees nothing in a visible run
        warns instead of failing: the peer may simply hold nothing that matches.
        """
        if self.skipping(label):
            return
        self.label = label
        try:
            seen, data, what = run()
        except Refused as exc:
            if not self.hidden:
                self.fail(label, f"refused: {exc}")
            if control is None:
                self.fail(label, f"refused, but the call names nothing private: {exc}")
            if control not in self.controls_passed:
                self.fail(label, f"refused ({exc}), but control {control!r} did not pass, "
                                 "so the refusal proves nothing")
            print(f"PASS {label}: refused ({str(exc)[:80]}); control {control} passed")
            return
        except Mismatch as exc:
            self.fail(label, str(exc))
        except Exception as exc:  # noqa: BLE001 — a broken check is a failed check
            self.fail(label, f"{type(exc).__name__}: {exc}")
        if self.hidden:
            if seen:
                self.fail(label, f"saw {self.project}: {what}")
            where = self.leak(data)
            if where:
                self.fail(label, f"{self.project} appears at {where}")
            if self.mentions(data):
                self.warn(f"{self.project} is mentioned in free text only")
            print(f"PASS {label}: no trace of {self.project} ({what})")
        else:
            if not seen and may_be_vacuous:
                self.warn(f"did not see {self.project}, so its hidden run proves little ({what})")
                return
            if not seen:
                self.fail(label, f"did not see {self.project}: {what}")
            print(f"PASS {label}: sees {self.project} ({what})")

    def check_control(self, key: str, label: str, run: Callable[[], tuple[bool, str]]) -> None:
        """The same call naming the control project must answer, or the run fails."""
        if self.skipping(label):
            return
        try:
            ok, what = run()
        except Exception as exc:  # noqa: BLE001 — a failed control fails the run
            self.fail(label, f"{type(exc).__name__}: {exc}")
        if not ok:
            self.fail(label, what)
        self.controls_passed.add(key)
        print(f"PASS {label}: {what}")

    def fail(self, label: str, why: str) -> None:
        print(f"FAIL {label}: {why}")
        sys.exit(1)

    # -- setup ---------------------------------------------------------------

    def load_facts(self) -> None:
        try:
            self.facts = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            self.facts = {}
        if self.o.doc_id:
            self.facts.update(doc_id=self.o.doc_id, title=None)
        if self.o.scope:
            self.facts["scope"] = self.o.scope
        if self.o.file:
            self.facts["file"] = self.o.file
        if self.o.peer_workspace:
            self.facts["workspace"] = self.o.peer_workspace
        if self.o.ssh:
            self.learn_on_peer()
        self.root = (self.facts.get("workspace") or str(Path.home() / "agentic_workspace")).rstrip("/")
        self.project_dir = f"{self.root}/projects/{self.project}"
        missing = [k for k in ("doc_id", "scope", "file") if not self.facts.get(k)]
        if missing:
            self.fail("setup", f"no {', '.join(missing)} for {self.project}: run once with "
                               "ssh to the peer, or pass --doc-id/--scope/--file")
        self.learn_control()
        self.save_facts()
        print(f"INFO setup: doc {self.facts['doc_id']}, scope {self.facts['scope']}, "
              f"file {self.facts['file']}, workspace {self.root}")
        print(f"INFO setup: journal db {self.facts.get('journal_db') or 'none'}, "
              f"transcript {self.facts.get('transcript_file') or 'none'}")
        c = self.control
        print(f"INFO control: {c['project']}/{c['scope']}, doc {c['doc_id']}, file {c['file']}")

    def save_facts(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.facts, indent=2) + "\n")

    def learn_on_peer(self) -> None:
        if not self.o.peer_workspace:
            self.facts["workspace"] = self.ssh('echo "${AWM_WORKSPACE:-$HOME/agentic_workspace}"')
        ws = self.facts["workspace"]
        if not self.o.doc_id:
            posts = self.on_peer("journal", "fetch", {"project": self.project,
                                                      "kind": "journal", "limit": 1})["posts"]
            if posts:
                meta = posts[0].get("meta") or {}
                self.facts.update(doc_id=posts[0]["id"],
                                  title=meta.get("title") or posts[0]["body"].splitlines()[0])
        if not self.o.scope:
            scopes = self.on_peer("scope", "search", {"project": self.project,
                                                      "status": "all", "limit": 1})["scopes"]
            if scopes:
                self.facts["scope"] = scopes[0]["scope"]
        if not self.o.file and self.facts.get("scope"):
            self.facts["file"] = self.first_file(f"{ws}/projects/{self.project}/{self.facts['scope']}")
        db = f"{ws}/.awm/services/journal/journal.db"
        self.facts["journal_db"] = db if self.ssh(f"test -f {db} && echo yes || true") else None
        prefix = slug(f"{ws.rstrip('/')}/projects/{self.project}")
        self.facts["transcript_file"] = self.ssh(
            f"for d in ~/.claude/projects/{prefix} ~/.claude/projects/{prefix}-*; do"
            " [ -d \"$d\" ] && find \"$d\" -maxdepth 1 -type f -name '*.jsonl' | sort | head -1;"
            " done | head -1") or None

    def first_file(self, directory: str) -> str | None:
        return self.ssh(
            f"find {directory} -maxdepth 2 -type f \\( -name '*.md' -o -name '*.txt' -o -name '*.toml' \\)"
            " -not -path '*/.*/*' | sort | head -1") or None

    def learn_control(self) -> None:
        """A project the peer shows its peers, to prove each refused call works for it."""
        cached = self.facts.get("control") or {}
        if (cached.get("project") and self.o.control in (None, cached["project"])
                and all(cached.get(k) for k in ("scope", "doc_id", "file"))):
            self.control = cached
            return
        name = self.o.control
        if not name:
            listed = self.peer_call("scope", "search", {"status": "all", "limit": 500})
            names = [p.get("name") for p in listed.get("projects") or []]
            names += [s.get("project") for s in listed.get("scopes") or []]
            names = [n for n in names if n and n != self.project]
            if not names:
                self.fail("setup", f"{self.peer} shows its peers no project to use as a control")
            name = "awm" if "awm" in names else names[0]
        scopes = [s for s in self.peer_call("scope", "search", {"project": name, "status": "all",
                                                                "limit": 50})["scopes"]
                  if s.get("project") == name and s.get("worktree")]
        posts = self.peer_call("journal", "fetch", {"project": name, "kind": "journal",
                                                    "limit": 1})["posts"]
        if not scopes or not posts:
            self.fail("setup", f"control project {name} on {self.peer} shows peers "
                               f"{len(scopes)} scopes with a worktree and {len(posts)} journal posts")
        scope = next((s for s in scopes if s["scope"] == "dev"), scopes[0])
        file = cached.get("file") if cached.get("project") == name else None
        if not file and self.o.ssh:
            file = self.first_file(scope["worktree"])
        if not file:
            self.fail("setup", f"no file for control project {name}: run once with ssh to the peer")
        self.control = {"project": name, "scope": scope["scope"], "doc_id": posts[0]["id"],
                        "file": file}
        self.facts["control"] = self.control

    # -- controls ------------------------------------------------------------

    def run_controls(self) -> None:
        c = self.control
        name = c["project"]
        self.check_control("get", f"0.1 control search get {name} journal doc", lambda: (
            self.names_project(self.get_doc(c["doc_id"]).get("project"), name=name),
            f"document of {name} served"))

        def scope_search() -> tuple[bool, str]:
            rows = self.peer_call("scope", "search", {"project": name, "status": "all"})["scopes"]
            return any(s.get("project") == name for s in rows), f"{len(rows)} scopes of {name}"
        self.check_control("scope_search", f"0.2 control scope search project={name}", scope_search)
        self.check_control("scope_resolve", f"0.3 control scope resolve {name}/{c['scope']}", lambda: (
            bool(self.peer_call("scope", "resolve", {"project": name, "scope": c["scope"]})
                 .get("exists")), f"{name}/{c['scope']} exists"))

        def fetch() -> tuple[bool, str]:
            posts = self.peer_call("journal", "fetch", {"project": name, "kind": "journal",
                                                        "limit": 5})["posts"]
            return any(self.names_project(p.get("project"), p.get("scope"), name) for p in posts), \
                f"{len(posts)} posts of {name}"
        self.check_control("fetch", f"0.4 control journal fetch project={name}", fetch)
        self.check_control("render", f"0.5 control journal render {name}/{c['scope']}", lambda: (
            "content" in self.peer_call("journal", "render", {"project": name, "scope": c["scope"],
                                                              "write": False}), "rendered"))

        def files() -> tuple[bool, str]:
            got = self.peer_file(c["file"])
            return got.status_code == 200, f"HTTP {got.status_code} {got.text[:80]}".strip()
        self.check_control("files", f"0.6 control /files{c['file']}", files)

    # -- checks --------------------------------------------------------------

    def search_hits(self, query: str, terms: list[str]) -> tuple[bool, Any, str]:
        """The local search fanned out to the peer."""
        args = {"query": query, "peers": self.peer, "limit": 100}
        for _ in range(3):
            out = parse_result(self.local_invoke("search", {"verb": "query", "args": args})["result"])
            answer = (out.get("peers") or {}).get(self.peer, {})
            if answer.get("status") == "ok":
                break
        else:
            raise Mismatch(f"peer {self.peer} answered the fan-out with {answer!r:.200}")
        hits = [h for h in out.get("hits", []) if (h.get("origin") or {}).get("node") == self.peer]
        return self.judge_hits(hits, terms)

    def transcript_hits(self, query: str, terms: list[str]) -> tuple[bool, Any, str]:
        """The peer's search over its transcripts, asked directly: the fan-out hides a source that failed."""
        args = {"query": query, "sources": ["transcripts"], "limit": 100}
        for _ in range(3):
            out = self.peer_call("search", "query", args)
            status = ((out.get("sources") or {}).get("transcripts") or {}).get("status", "ok")
            if status != "error":
                break
        else:
            raise Mismatch(f"the transcripts source on {self.peer} failed three times "
                           f"({out['sources']['transcripts']}), so nothing is proved")
        return self.judge_hits(out.get("hits", []), terms)

    def judge_hits(self, hits: list[dict], terms: list[str]) -> tuple[bool, Any, str]:
        """Own hits are seen. In a hidden run a hit from another project naming a term in its
        title or snippet warns, or fails under ``--strict-mentions`` unless ``--allow-hit`` names it."""
        seen, data, what = self.own_hits(hits)
        if not self.hidden or seen:
            return seen, data, what
        pattern = re.compile("|".join(re.escape(t) for t in terms), re.I)
        named = [h for h in hits if pattern.search(f"{h.get('title') or ''}\n{h.get('snippet') or ''}")]
        out_of_scope = [h for h in named if h.get("source") in OUT_OF_SCOPE_SOURCES]
        allowed = [h for h in named if h not in out_of_scope and any(
            fnmatch.fnmatchcase(f"{h.get('source')}:{h.get('id')}", g) for g in self.o.allow_hit or [])]
        bad = [h for h in named if h not in out_of_scope and h not in allowed]
        if bad and self.o.strict_mentions:
            raise Mismatch(f"{len(bad)} hits name a probe term in title or snippet: " + ", ".join(
                f"{h.get('source')}:{h.get('id')} ({h.get('project')}/{h.get('scope')})" for h in bad[:5]))
        if bad:
            self.warn(f"{len(bad)} hits from other projects mention a probe term; a mark hides the "
                      "project's own records, not mentions elsewhere: " + ", ".join(
                f"{h.get('source')}:{h.get('id')} ({h.get('project')}/{h.get('scope')})" for h in bad[:5]))
        if out_of_scope:
            sources = sorted({h["source"] for h in out_of_scope})
            self.warn(f"{len(out_of_scope)} hits from {', '.join(sources)} name a probe term in "
                      "free text; those sources carry no project field, out of the mark's scope")
        if allowed:
            self.warn(f"{len(allowed)} hits waived by --allow-hit name a probe term in free text")
        rest = [h for h in hits if h not in out_of_scope and h not in allowed]
        return False, rest, what

    def own_hits(self, hits: list[dict]) -> tuple[bool, Any, str]:
        own = [h for h in hits if self.names_project(h.get("project"), h.get("scope"))
               or self.slug_of_project(h.get("project"))
               or h.get("id") == self.facts.get("doc_id")]
        return bool(own), hits, f"{len(own)} of {len(hits)} hits from {self.peer}"

    def get_doc(self, doc_id: str) -> dict:
        """One journal document from the peer through the local search, or Refused."""
        args = {"source": "journals", "id": doc_id, "node": self.peer}
        try:
            out = parse_result(self.local_invoke("search", {"verb": "get", "args": args})["result"])
        except RuntimeError as exc:
            msg = str(exc)
            if f"no document journals:{doc_id}" in msg:
                raise Refused(f"no document journals:{doc_id}") from None
            if re.search(r"HTTP 404: .*(Unknown tool: search_get|"
                         r"Unknown verb 'get' for domain 'search')", msg):
                raise Refused("the gate refused search get") from None
            raise
        if not isinstance(out, dict) or not isinstance(out.get("document"), dict):
            raise RuntimeError(f"search get answered {out!r:.200}")
        return out["document"]

    def search_get(self) -> tuple[bool, Any, str]:
        doc = self.get_doc(self.facts["doc_id"])
        return self.names_project(doc.get("project"), doc.get("scope")), doc, \
            f"document project {doc.get('project')!r}"

    def scope_search(self, **args: Any) -> tuple[bool, Any, str]:
        out = self.peer_call("scope", "search", {"status": "all", "limit": 500, **args})
        scopes = [s for s in out.get("scopes", []) if s.get("project") == self.project
                  or self.under_project(s.get("worktree"))]
        projects = [p for p in out.get("projects") or [] if p.get("name") == self.project]
        if args.get("project") is None and not self.hidden and not projects:
            raise Mismatch(f"project list lacks {self.project}")
        seen = bool(scopes) if args.get("project") else bool(scopes or projects)
        return seen, out, f"{len(scopes)} scopes, {len(projects)} project rows"

    def scope_resolve(self) -> tuple[bool, Any, str]:
        out = self.peer_call("scope", "resolve", {"project": self.project,
                                                   "scope": self.facts["scope"]})
        return bool(out.get("exists")), {k: v for k, v in out.items()
                                         if k not in ("project", "scope")}, \
            f"exists={out.get('exists')}"

    def own_rows(self, rows: list, what: str) -> tuple[bool, Any, str]:
        own = [r for r in rows if isinstance(r, dict) and (
            self.names_project(r.get("project"), r.get("scope")) or self.under_project(r.get("cwd")))]
        return bool(own), rows, f"{len(own)} of {len(rows)} {what}"

    def journal_fetch(self, **args: Any) -> tuple[bool, Any, str]:
        out = self.peer_call("journal", "fetch", {"limit": 500, **args})
        return self.own_rows(out.get("posts", []), "posts")

    def journal_goals(self) -> tuple[bool, Any, str]:
        out = self.peer_call("journal", "goal_read", {})
        return self.own_rows(out.get("goals", []), "goals")

    def journal_export(self) -> tuple[bool, Any, str]:
        docs: list[dict] = []
        cursor = None
        while True:
            page = self.peer_flat("journal_export", {"cursor": cursor, "limit": 1000}
                                  if cursor else {"limit": 1000}, timeout=300.0)
            docs += page.get("documents", [])
            if page.get("done"):
                break
            cursor = page["next_cursor"]
        seen, _, what = self.own_rows(docs, "documents")
        if self.facts["doc_id"] in (page.get("ids") or []):
            seen, what = True, f"{what}; the id set holds {self.facts['doc_id']}"
        return seen, [{k: d.get(k) for k in ("id", "project", "scope", "kind", "title")} for d in docs], what

    def journal_render(self) -> tuple[bool, Any, str]:
        out = self.peer_call("journal", "render", {"project": self.project,
                                                    "scope": self.facts["scope"], "write": False})
        content = out.get("content", "")
        seen = bool(re.search(r"^\*\*\[[0-9a-f-]{36}\]", content, re.M))
        return seen, {k: v for k, v in out.items() if k not in ("content", "path")}, \
            f"{'entries' if seen else 'no entries'} in the render"

    def dvc_coverage(self) -> tuple[bool, Any, str]:
        out = self.peer_call("dvc", "coverage", {"all": True}, timeout=900.0)
        rows = [w for w in out.get("worktrees", []) if self.under_project(w.get("scope"))]
        return bool(rows), out, f"{len(rows)} of {len(out.get('worktrees', []))} worktrees"

    def static_file(self, path: str) -> tuple[bool, Any, str]:
        got = self.peer_file(path)
        if got.status_code == 404 and got.text.strip() == "not found":
            raise Refused(f"404 for /files{path}")
        if got.status_code != 200:
            raise RuntimeError(f"/files{path}: HTTP {got.status_code}: {got.text[:200]}")
        return True, {}, f"/files{path} served, {got.size} bytes"

    def outside_file(self, label: str, key: str) -> None:
        """A file outside the project folder that holds its data: refused only in a hidden run."""
        path = self.facts.get(key)
        if not path:
            self.skip(label, f"{self.peer} holds no such file for {self.project}")
        elif not self.hidden:
            if not self.skipping(label):
                got = self.peer_file(path)
                print(f"SKIP {label} {path}: n/a in a visible run (HTTP {got.status_code})")
        else:
            self.check(f"{label} {path}", lambda: self.static_file(path), control="files")

    def precedence(self) -> tuple[bool, Any, str]:
        found: dict[str, dict] = {}
        for args in ({"keyword": f'"{self.project}"', "k": 50}, {"tag": [self.project], "k": 50}):
            for row in self.peer_call("precedence", "search", args).get("results", []):
                found[row["id"]] = row
        rows = list(found.values())
        tagged = [r for r in rows if self.project.lower() in (t.lower() for t in r.get("tags") or [])]
        return bool(tagged), rows, f"{len(tagged)} of {len(rows)} decisions tagged {self.project}"

    def cx_list(self) -> tuple[bool, Any, str]:
        return self.own_rows(self.peer_call("reflection", "list_other", {}).get("sessions", []),
                             "sessions")

    def transcripts_direct(self) -> tuple[bool, Any, str]:
        out = self.peer_flat("transcripts_search", {"pattern": re.escape(self.project),
                                                    "limit": 200}, timeout=600.0)
        hits = out.get("hits", [])
        own = [h for h in hits if self.slug_of_project(h.get("project"))
               or self.under_project(h.get("cwd"))]
        return bool(own), hits, f"{len(own)} of {len(hits)} hits under {self.project}"

    def archive_counts(self) -> tuple[bool, Any, str]:
        listed = self.peer_call("search", "sources", {}).get("sources", [])
        counts = {"sources": next((s.get("count") for s in listed if s.get("name") == "journals"), None),
                  "status": (self.peer_call("search", "status", {}).get("sources") or {}).get("journals")}
        if None in counts.values():
            raise Mismatch(f"no journals count from {self.peer}: {counts}")
        if not self.hidden:
            self.facts["baseline_counts"] = counts
            self.save_facts()
            return True, {}, f"journals {counts}, cached as the baseline"
        base = self.facts["baseline_counts"]
        grown = [k for k in counts if counts[k] >= base[k]]
        return bool(grown), {}, f"journals {counts} against the visible baseline {base}"

    def compute_decisions(self) -> tuple[bool, Any, str]:
        rows = self.peer_flat("compute_decisions", {"limit": 1000}).get("decisions", [])
        own = [r for r in rows if self.path_mentioned(r.get("cmdline")) or self.under_project(r.get("cwd"))
               or self.under_project((r.get("detail") or {}).get("cwd"))]
        return bool(own), rows, f"{len(own)} of {len(rows)} decisions under {self.project_dir}"

    def posts_topic(self) -> tuple[bool, Any, str]:
        token = uuid.uuid4().hex[:12]
        got: list[dict] = []
        ready = threading.Event()
        stop = threading.Event()
        errors: list[BaseException] = []

        async def listen() -> None:
            stream = gatewayclient.subscribe_peer(self.peer, "journal", "posts",
                                                  on_connect=ready.set)
            try:
                async for payload in stream:
                    if isinstance(payload, dict):
                        got.append(payload)
                    if stop.is_set():
                        break
            finally:
                await stream.aclose()

        def run() -> None:
            try:
                asyncio.run(asyncio.wait_for(listen(), timeout=self.o.wait + 30))
            except (asyncio.TimeoutError, TimeoutError):
                pass
            except BaseException as exc:  # noqa: BLE001 — reported by the check
                errors.append(exc)
                ready.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        if not ready.wait(30) or errors:
            raise Mismatch(f"could not subscribe to journal/posts on {self.peer}: {errors}")
        body = f"privacy-probe {token}"
        self.on_peer("journal", "post", {"project": "", "scope": "user:privacy-probe",
                                         "author": "user:privacy-probe", "kind": "message",
                                         "body": f"{body} control"})
        self.on_peer("journal", "post", {"project": self.project, "scope": self.facts["scope"],
                                         "author": "user:privacy-probe", "kind": "message",
                                         "body": body})
        deadline = time.monotonic() + self.o.wait
        mine = lambda: [p for p in got if token in json.dumps(p)]  # noqa: E731
        while time.monotonic() < deadline and len(mine()) < 2:
            time.sleep(0.2)
        stop.set()
        seen = mine()
        control = [p for p in seen if p.get("scope") == "user:privacy-probe"]
        own = [p for p in seen if self.names_project(p.get("project"), p.get("scope"))
               or self.project in json.dumps(p)]
        if not control:
            raise Mismatch(f"the control post never arrived within {self.o.wait}s; "
                           "the subscription proves nothing")
        return bool(own), [p for p in got if p not in own], \
            f"control received, {len(own)} {self.project} posts received"

    def run(self) -> None:
        self.load_facts()
        self.run_controls()
        terms = [self.project, *(self.o.term or [])]
        if self.facts.get("title"):
            terms.append(self.facts["title"])
        # A visible run needs only the project name to find it; a hidden run tries every term.
        for i, term in enumerate(terms if self.hidden else terms[:1]):
            label = f"1.{i + 1} search query {term[:40]!r}"
            self.check(label, lambda t=term: self.search_hits(t, terms))
            self.check(f"14.{i + 1} search query {term[:40]!r} sources=transcripts",
                       lambda t=term: self.transcript_hits(t, terms), may_be_vacuous=True)
        self.check("2 search get journal doc", self.search_get, control="get")
        self.check("3.1 scope search (all projects)", self.scope_search)
        self.check(f"3.2 scope search project={self.project}",
                   lambda: self.scope_search(project=self.project), control="scope_search")
        self.check(f"3.3 scope resolve {self.project}/{self.facts['scope']}", self.scope_resolve,
                   control="scope_resolve")
        self.check("4.1 journal fetch kind=journal (no project)",
                   lambda: self.journal_fetch(kind="journal"))
        self.check(f"4.2 journal fetch project={self.project}",
                   lambda: self.journal_fetch(project=self.project, kind="journal"), control="fetch")
        self.check(f"4.3 journal render {self.project}/{self.facts['scope']}", self.journal_render,
                   control="render")
        self.check(f"4.4 journal fetch query={self.project} (no project)",
                   lambda: self.journal_fetch(query=self.project, limit=100), may_be_vacuous=True)
        self.check("4.5 journal goal_read (no project)", self.journal_goals, may_be_vacuous=True)
        self.check("4.6 journal export", self.journal_export)
        self.check("5 dvc coverage", self.dvc_coverage)
        self.check(f"6 /files{self.facts['file']}", lambda: self.static_file(self.facts["file"]),
                   control="files")
        if self.o.post:
            self.check("7 journal/posts subscription", self.posts_topic)
        elif not self.skipping("7 journal/posts subscription"):
            self.skip("7 journal/posts subscription", "pass --post to post on the peer")
        self.outside_file("8.1 /files journal db", "journal_db")
        self.outside_file("8.2 /files transcript", "transcript_file")
        self.check(f"9 precedence search {self.project}", self.precedence, may_be_vacuous=True)
        self.check("10 reflection list_other", self.cx_list, may_be_vacuous=True)
        self.check(f"11 transcripts search {self.project}", self.transcripts_direct,
                   may_be_vacuous=True)
        if self.hidden and not self.facts.get("baseline_counts"):
            if not self.skipping("12 search sources/status"):
                self.skip("12 search sources/status", "no visible baseline cached; "
                                                      "run --expect visible first")
        else:
            self.check("12 search sources/status journals count", self.archive_counts)
        self.check("13 compute decisions", self.compute_decisions, may_be_vacuous=True)
        tail = f" ({self.warnings} warnings)" if self.warnings else ""
        if self.skipped:
            print(f"OK ({len(self.skipped)} skipped: {', '.join(self.skipped)}) "
                  f"{self.peer} {self.project} {self.o.expect}{tail}")
            sys.exit(2 if self.o.strict else 0)
        print(f"OK {self.peer} {self.project} {self.o.expect}{tail}")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="privacy-probe.sh", description=__doc__.split("\n\n")[0])
    ap.add_argument("peer")
    ap.add_argument("project")
    ap.add_argument("--expect", choices=("visible", "hidden"), default="hidden")
    ap.add_argument("--doc-id", help="a journal post id of the project on the peer")
    ap.add_argument("--scope", help="a scope of the project on the peer")
    ap.add_argument("--file", help="an absolute path of a file under the project on the peer")
    ap.add_argument("--term", action="append", help="another search term (repeatable)")
    ap.add_argument("--control", help="a project the peer shows its peers, whose calls must "
                                      "answer for a refusal to count (default: awm, else the first listed)")
    ap.add_argument("--strict-mentions", action="store_true",
                    help="in a hidden run, fail on a hit from another project that names a probe "
                         "term in its title or snippet")
    ap.add_argument("--allow-hit", action="append", metavar="SOURCE:ID",
                    help="with --strict-mentions, warn instead of failing on this hit "
                         "(a glob over source:id, repeatable)")
    ap.add_argument("--post", action="store_true",
                    help="post a probe message on the peer and watch the journal/posts topic")
    ap.add_argument("--no-ssh", dest="ssh", action="store_false",
                    help="never ssh to the peer; use the cache and the options")
    ap.add_argument("--peer-port", default="7819", help="the peer gateway's local port")
    ap.add_argument("--peer-workspace", help="the peer's workspace root (default: this node's)")
    ap.add_argument("--skip", action="append", metavar="N",
                    help="skip check group N, such as 5 (repeatable)")
    ap.add_argument("--strict", action="store_true", help="exit 2 when any check was skipped")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for posts on the topic")
    opts = ap.parse_args(argv)
    if opts.control == opts.project:
        ap.error("--control must name a project other than the one probed")
    Probe(opts).run()


if __name__ == "__main__":
    main(sys.argv[1:])
