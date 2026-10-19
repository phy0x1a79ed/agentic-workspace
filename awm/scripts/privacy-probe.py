"""Check, from this node, that a project on a peer is visible or hidden to peer calls.

Every check reaches the peer the way an agent here does: through this node's
gateway, which either fans the call out (``search``) or hands back the peer's
edge address, which the probe then dials with this node's peer credential.
Setup reads that must see the project whatever the mark says (a journal id, a
scope, a file) run on the peer itself over ssh, as a local caller there.

A refusal (HTTP 404, the gateway's unknown-tool answer to a peer naming a
private target) counts as hidden. With ``--expect visible`` it is a failure.
Run ``privacy-probe.sh --help`` for the options.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from awm import gatewayclient

FREE_TEXT = {"title", "snippet", "text", "body", "content", "rendered", "description",
             "detail", "message", "query", "objective", "note", "error", "reason",
             "modified_sample", "untracked_sample", "claimed_author"}
TIMEOUT = httpx.Timeout(120.0, connect=10.0)


class Refused(Exception):
    """The peer answered as for an unknown tool, verb or path."""


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
        self.warnings = 0

    # -- transport -----------------------------------------------------------

    def local_invoke(self, name: str, args: dict, *, redirect: bool = False) -> Any:
        headers = {"X-Awm-Peer-Redirect": "1"} if redirect else {}
        resp = httpx.post(f"{self.base}/invoke", json={"name": name, "args": args},
                          headers=headers, timeout=TIMEOUT)
        if resp.status_code == 404:
            raise Refused(resp.text[:300])
        if resp.status_code >= 400:
            raise RuntimeError(f"local gateway {name}: HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def peer_call(self, tool: str, verb: str, args: dict, *, timeout: float = 120.0) -> Any:
        """``tool.verb(args)`` on the peer, routed by the local gateway's peer redirect."""
        data = self.local_invoke(tool, {"verb": verb, "args": args, "peer": self.peer},
                                 redirect=True)
        entry = data.get("peer_redirect") if isinstance(data, dict) else None
        if not entry or entry.get("peer") != self.peer:
            raise RuntimeError(f"{tool}.{verb} with peer={self.peer} did not route to the peer: {data!r:.300}")
        self.entry = entry
        edge = entry["edge_url"].rstrip("/")
        body = {"name": tool, "args": {"verb": verb, "args": args}}

        def send(bearer: str) -> httpx.Response:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                              verify=gatewayclient._peer_ca()) as cli:
                return cli.post(f"{edge}/invoke", json=body,
                                headers={"Authorization": f"Bearer {bearer}"})

        resp = gatewayclient.peer_send_sync(self.peer, entry, send)
        if resp.status_code == 404:
            raise Refused(resp.text[:300])
        if resp.status_code >= 400:
            raise RuntimeError(f"{tool}.{verb} on {self.peer}: HTTP {resp.status_code}: {resp.text[:300]}")
        return parse_result(resp.json().get("result"))

    def on_peer(self, tool: str, verb: str, args: dict) -> Any:
        """``tool.verb(args)`` run on the peer as a local caller there, over ssh."""
        alias = self.peer_entry().get("ssh_alias") or self.peer
        body = json.dumps({"name": tool, "args": {"verb": verb, "args": args}})
        cmd = (f"curl -sS -X POST http://127.0.0.1:{self.o.peer_port}/invoke "
               "-H 'content-type: application/json' --data-binary @-")
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", alias, cmd], input=body,
                              capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"ssh {alias}: {proc.stderr.strip()[:300]}")
        reply = json.loads(proc.stdout)
        if "result" not in reply:
            raise RuntimeError(f"{tool}.{verb} on {alias}: {proc.stdout[:300]}")
        return parse_result(reply["result"])

    def peer_entry(self) -> dict:
        if not self.entry:
            self.entry = gatewayclient.resolve_peer(self.peer)
        return self.entry

    # -- matching ------------------------------------------------------------

    def names_project(self, project: Any, scope: Any = None) -> bool:
        p = self.project
        if project in (p, f"task:{p}"):
            return True
        return not project and scope in (f"project:{p}", f"user:proj:{p}")

    def under_project(self, path: Any) -> bool:
        if not isinstance(path, str) or not path:
            return False
        if not path.startswith("/"):
            path = f"{self.root}/{path}"
        return path == self.project_dir or path.startswith(self.project_dir + "/")

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

    def check(self, label: str, run: Callable[[], tuple[bool, Any, str]], *,
              may_be_vacuous: bool = False) -> None:
        """Run one check. ``run`` returns (saw the project, the data, what it saw).

        A ``may_be_vacuous`` check that sees nothing in a visible run warns instead
        of failing: the peer may simply hold nothing that matches.
        """
        if label.split(".")[0].split()[0] in (self.o.skip or []):
            print(f"SKIP {label}")
            return
        try:
            seen, data, what = run()
        except Refused as exc:
            if self.hidden:
                print(f"PASS {label}: refused ({str(exc)[:80]})")
                return
            self.fail(label, f"refused: {exc}")
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
                self.warnings += 1
                print(f"WARN {label}: {self.project} is mentioned in free text only")
            print(f"PASS {label}: no trace of {self.project}")
        else:
            if not seen and may_be_vacuous:
                self.warnings += 1
                print(f"WARN {label}: did not see {self.project}, so its hidden run proves little ({what})")
                return
            if not seen:
                self.fail(label, f"did not see {self.project}: {what}")
            print(f"PASS {label}: sees {self.project} ({what})")

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
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.facts, indent=2) + "\n")
        print(f"INFO setup: doc {self.facts['doc_id']}, scope {self.facts['scope']}, "
              f"file {self.facts['file']}, workspace {self.root}")

    def ssh(self, cmd: str) -> str:
        alias = self.peer_entry().get("ssh_alias") or self.peer
        proc = subprocess.run(["ssh", "-o", "BatchMode=yes", alias, cmd],
                              capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"ssh {alias}: {proc.stderr.strip()[:300]}")
        return proc.stdout.strip()

    def learn_on_peer(self) -> None:
        if not self.o.peer_workspace:
            self.facts["workspace"] = self.ssh('echo "${AWM_WORKSPACE:-$HOME/agentic_workspace}"')
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
            out = self.ssh(
                f"find {self.facts['workspace']}/projects/{self.project}/{self.facts['scope']}"
                " -maxdepth 2 -type f \\( -name '*.md' -o -name '*.txt' -o -name '*.toml' \\)"
                " -not -path '*/.awm/*' -not -path '*/.git/*' | sort | head -1")
            if out:
                self.facts["file"] = out

    # -- checks --------------------------------------------------------------

    def search_hits(self, query: str) -> tuple[bool, Any, str]:
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
        return self.own_hits(hits)

    def transcript_hits(self, query: str) -> tuple[bool, Any, str]:
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
        return self.own_hits(out.get("hits", []))

    def own_hits(self, hits: list[dict]) -> tuple[bool, Any, str]:
        own = [h for h in hits if self.names_project(h.get("project"), h.get("scope"))
               or self.slug_of_project(h.get("project"))
               or h.get("id") == self.facts.get("doc_id")]
        return bool(own), hits, f"{len(own)} of {len(hits)} hits from {self.peer}"

    def search_get(self) -> tuple[bool, Any, str]:
        args = {"source": "journals", "id": self.facts["doc_id"], "node": self.peer}
        try:
            out = parse_result(self.local_invoke("search", {"verb": "get", "args": args})["result"])
        except RuntimeError as exc:
            if re.search(r"no document|not found|unknown", str(exc), re.I):
                return False, {}, f"not found ({str(exc)[:80]})"
            raise
        if isinstance(out, dict) and out.get("error"):
            return False, {}, f"error {out['error']!r:.80}"
        doc = out.get("document", out) if isinstance(out, dict) else {}
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

    def journal_fetch(self, **args: Any) -> tuple[bool, Any, str]:
        out = self.peer_call("journal", "fetch", {"kind": "journal", "limit": 500, **args})
        own = [p for p in out.get("posts", []) if self.names_project(p.get("project"), p.get("scope"))]
        return bool(own), out, f"{len(own)} of {len(out.get('posts', []))} posts"

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

    def static_file(self) -> tuple[bool, Any, str]:
        url = "/files" + self.facts["file"]
        try:
            local = gatewayclient.fetch_peer_file_sync(self.peer, url, entry=self.peer_entry())
        except gatewayclient.PeerError as exc:
            if "has no file" in str(exc):
                raise Refused(f"404 for {url}")
            raise
        size = Path(local).stat().st_size
        Path(local).unlink(missing_ok=True)
        return True, {}, f"{url} served, {size} bytes"

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
        terms = [self.project, *(self.o.term or [])]
        if self.facts.get("title"):
            terms.append(self.facts["title"])
        # A visible run needs only the project name to find it; a hidden run tries every term.
        for i, term in enumerate(terms if self.hidden else terms[:1]):
            label = f"1.{i + 1} search query {term[:40]!r}"
            self.check(label, lambda t=term: self.search_hits(t))
            self.check(f"{label} sources=transcripts",
                       lambda t=term: self.transcript_hits(t), may_be_vacuous=True)
        self.check("2 search get journal doc", self.search_get)
        self.check("3.1 scope search (all projects)", self.scope_search)
        self.check(f"3.2 scope search project={self.project}",
                   lambda: self.scope_search(project=self.project))
        self.check(f"3.3 scope resolve {self.project}/{self.facts['scope']}", self.scope_resolve)
        self.check("4.1 journal fetch (no project)", self.journal_fetch)
        self.check(f"4.2 journal fetch project={self.project}",
                   lambda: self.journal_fetch(project=self.project))
        self.check(f"4.3 journal render {self.project}/{self.facts['scope']}", self.journal_render)
        self.check("5 dvc coverage", self.dvc_coverage)
        self.check(f"6 /files{self.facts['file']}", self.static_file)
        if self.o.post:
            self.check("7 journal/posts subscription", self.posts_topic)
        else:
            print("SKIP 7 journal/posts subscription: pass --post to post on the peer")
        print(f"OK {self.peer} {self.project} {self.o.expect}"
              f"{f' ({self.warnings} warnings)' if self.warnings else ''}")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="privacy-probe.sh", description=__doc__.split("\n\n")[0])
    ap.add_argument("peer")
    ap.add_argument("project")
    ap.add_argument("--expect", choices=("visible", "hidden"), default="hidden")
    ap.add_argument("--doc-id", help="a journal post id of the project on the peer")
    ap.add_argument("--scope", help="a scope of the project on the peer")
    ap.add_argument("--file", help="an absolute path of a file under the project on the peer")
    ap.add_argument("--term", action="append", help="another search term (repeatable)")
    ap.add_argument("--post", action="store_true",
                    help="post a probe message on the peer and watch the journal/posts topic")
    ap.add_argument("--no-ssh", dest="ssh", action="store_false",
                    help="never ssh to the peer; use the cache and the options")
    ap.add_argument("--peer-port", default="7819", help="the peer gateway's local port")
    ap.add_argument("--peer-workspace", help="the peer's workspace root (default: this node's)")
    ap.add_argument("--skip", action="append", metavar="N",
                    help="skip check group N, such as 5 (repeatable)")
    ap.add_argument("--wait", type=float, default=15.0,
                    help="seconds to wait for posts on the topic")
    Probe(ap.parse_args(argv)).run()


if __name__ == "__main__":
    main(sys.argv[1:])
