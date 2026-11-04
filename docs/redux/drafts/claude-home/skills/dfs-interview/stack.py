"""Edit a dfs-interview discussion folder: its STACK.md, PREFERENCES.md and topic directories.

STACK.md format, one line per entry, two spaces of indent per level of nesting:
  - **<slug>** (<date>): <the user's words>     a topic; a child topic sits one level under its parent
  - [ ] <question>                              a queued question, directly under its topic
  - [x] <question> (answered <date>)            an answered question
Any other bullet under a topic is a note and stays with it.
The top topic is the first topic, in file order, with no child topic under it.
"""
import argparse
import datetime
import os
import re
import sys

SECTIONS = ["Purpose", "Current topic", "Stack", "Parked", "Closed"]
TOPIC = re.compile(r"^(?P<ind> *)- \*\*(?P<slug>[a-z0-9][a-z0-9-]*)\*\* \((?P<date>[0-9-]+)\): (?P<words>.*)$")
OPEN_Q = re.compile(r"^(?P<ind> *)- \[ \] (?P<q>.*)$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TODAY = datetime.date.today().isoformat()


def die(msg):
    sys.exit(f"stack.py: {msg}")


def indent(line):
    return len(line) - len(line.lstrip(" "))


class Stack:
    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, "STACK.md")
        if not os.path.exists(self.path):
            die(f"{self.path} does not exist; run init first")
        self.head, self.sections = [], {}
        current = None
        for line in open(self.path, encoding="utf-8").read().split("\n"):
            m = re.match(r"^## (.+)$", line)
            if m and m.group(1) in SECTIONS:
                current = m.group(1)
                self.sections[current] = []
            elif current is None:
                self.head.append(line)
            else:
                self.sections[current].append(line)
        missing = [s for s in SECTIONS if s not in self.sections]
        if missing:
            die(f"{self.path} lacks the sections {missing}")
        # Blank lines carry no meaning inside the stack and break block extents.
        self.sections["Stack"] = [l for l in self.sections["Stack"] if l.strip()]

    def save(self):
        out = list(self.head)
        for name in SECTIONS:
            body = self.sections[name]
            if name == "Stack":
                body = [""] + body + [""]
            out += [f"## {name}"] + body
        text = "\n".join(out).rstrip("\n") + "\n"
        text = re.sub(r"\n{3,}", "\n\n", text)
        open(self.path, "w", encoding="utf-8").write(text)

    @property
    def lines(self):
        return self.sections["Stack"]

    def find(self, slug):
        hits = [i for i, l in enumerate(self.lines) if (m := TOPIC.match(l)) and m["slug"] == slug]
        if not hits:
            die(f"no open topic '{slug}' in the stack")
        if len(hits) > 1:
            die(f"topic '{slug}' appears {len(hits)} times; slugs must be unique")
        return hits[0]

    def block_end(self, i):
        j = i + 1
        while j < len(self.lines) and indent(self.lines[j]) > indent(self.lines[i]):
            j += 1
        return j

    def children(self, i):
        return [j for j in range(i + 1, self.block_end(i))
                if TOPIC.match(self.lines[j]) and indent(self.lines[j]) == indent(self.lines[i]) + 2]

    def body_end(self, i):
        """Index after the topic's own questions and notes, before its first child topic."""
        kids = self.children(i)
        return kids[0] if kids else self.block_end(i)

    def ancestry(self, i):
        chain, level = [TOPIC.match(self.lines[i])["slug"]], indent(self.lines[i])
        for j in range(i - 1, -1, -1):
            m = TOPIC.match(self.lines[j])
            if m and indent(self.lines[j]) < level:
                chain.insert(0, m["slug"])
                level = indent(self.lines[j])
        return chain

    def top(self):
        for i, l in enumerate(self.lines):
            if TOPIC.match(l) and not self.children(i):
                return i
        return None

    def topic_dir(self, i):
        return os.path.join(self.folder, *self.ancestry(i))

    def set_current(self, i):
        if i is None:
            self.sections["Current topic"] = ["", "(none: the stack is empty)", ""]
            return
        chain = self.ancestry(i)
        parent = f", under **{chain[-2]}**" if len(chain) > 1 else ""
        self.sections["Current topic"] = ["", f"**{chain[-1]}**{parent}. Its result: `{'/'.join(chain)}/README.md`.", ""]


def procedure():
    """The steps of § Work the stack, read from the skill doc next to this script."""
    here = os.path.dirname(os.path.abspath(__file__))
    for doc in (os.path.join(here, "SKILL.md"), here + ".md"):
        if os.path.exists(doc):
            m = re.search(r"^## Work the stack\n(.*?)(?=^## )", open(doc, encoding="utf-8").read(), re.S | re.M)
            if m:
                return m.group(1).strip()
    die("cannot find § Work the stack in the skill doc next to this script")


def enter(s, i):
    """Make topic i current: create its directory and its README.md."""
    s.set_current(i)
    if i is None:
        return None
    d = s.topic_dir(i)
    os.makedirs(d, exist_ok=True)
    readme = os.path.join(d, "README.md")
    if not os.path.exists(readme):
        open(readme, "w", encoding="utf-8").write(f"# {s.ancestry(i)[-1]}\n")
    return readme


def cmd_init(a):
    os.makedirs(a.folder, exist_ok=True)
    stack, prefs = os.path.join(a.folder, "STACK.md"), os.path.join(a.folder, "PREFERENCES.md")
    if os.path.exists(stack):
        die(f"{stack} already exists")
    name = os.path.basename(os.path.normpath(a.folder))
    open(stack, "w", encoding="utf-8").write(
        f"# {name}: stack\n\n## Purpose\n\n{a.purpose}\n\n"
        f"Procedure, from dfs-interview § Work the stack:\n\n{procedure()}\n\n"
        "## Current topic\n\n(none: the stack is empty)\n\n## Stack\n\n## Parked\n\n## Closed\n")
    if not os.path.exists(prefs):
        open(prefs, "w", encoding="utf-8").write(
            f"# {name}: preferences\n\n## Purpose\n\n"
            "This file holds the preferences the user set for this discussion, each with its date. "
            "A decision that holds beyond this discussion becomes a precedent post in the journal.\n\n## Preferences\n\n")
    print(f"created {stack} and {prefs}")


def cmd_push(a):
    if not SLUG.match(a.slug):
        die("a slug is lowercase letters, digits and hyphens")
    s = Stack(a.folder)
    if any((m := TOPIC.match(l)) and m["slug"] == a.slug for l in s.lines):
        die(f"topic '{a.slug}' is already on the stack")
    if a.under:
        p = s.find(a.under)
        level = indent(s.lines[p]) + 2
        at = s.block_end(p) if a.bottom else s.body_end(p)
    else:
        level, at = 0, len(s.lines) if a.bottom else 0
    s.lines.insert(at, f"{' ' * level}- **{a.slug}** ({TODAY}): {a.words}")
    s.save()
    where = f"under {a.under}" if a.under else "on the stack"
    print(f"pushed {a.slug} {where}, {'at the bottom' if a.bottom else 'on top'}")


def cmd_ask(a):
    s = Stack(a.folder)
    i = s.find(a.slug)
    s.lines.insert(s.body_end(i), f"{' ' * (indent(s.lines[i]) + 2)}- [ ] {a.question}")
    s.save()
    print(f"queued under {a.slug}: {a.question}")


def cmd_answer(a):
    s = Stack(a.folder)
    i = s.find(a.slug)
    level = indent(s.lines[i]) + 2
    open_qs = [j for j in range(i + 1, s.body_end(i)) if (m := OPEN_Q.match(s.lines[j])) and len(m["ind"]) == level]
    if a.match.isdigit():
        n = int(a.match)
        if not 1 <= n <= len(open_qs):
            die(f"{a.slug} has {len(open_qs)} open questions")
        hits = [open_qs[n - 1]]
    else:
        hits = [j for j in open_qs if a.match.lower() in s.lines[j].lower()]
    if len(hits) != 1:
        die(f"'{a.match}' matches {len(hits)} open questions of {a.slug}; pass its number instead")
    j = hits[0]
    q = OPEN_Q.match(s.lines[j])["q"]
    s.lines[j] = f"{' ' * level}- [x] {q} (answered {TODAY})"
    s.save()
    print(f"answered: {q}\nopen questions left under {a.slug}: {len(open_qs) - 1}")


def cmd_current(a):
    s = Stack(a.folder)
    i = s.find(a.slug) if a.slug else s.top()
    if i is None:
        die("the stack is empty")
    readme = enter(s, i)
    s.save()
    print(f"current topic: {'/'.join(s.ancestry(i))}\nresult doc: {readme}")


def cmd_close(a):
    s = Stack(a.folder)
    i = s.find(a.slug)
    block = s.lines[i:s.block_end(i)]
    if s.children(i):
        die(f"{a.slug} still has open child topics; close them first")
    if any(OPEN_Q.match(l) for l in block):
        die(f"{a.slug} still has open questions")
    rel = "/".join(s.ancestry(i))
    del s.lines[i:s.block_end(i)]
    closed = [l for l in s.sections["Closed"] if l.strip()]
    s.sections["Closed"] = [""] + closed + [f"- **{a.slug}** (closed {TODAY}): `{rel}/README.md`", ""]
    nxt = s.top()
    readme = enter(s, nxt)
    s.save()
    print(f"closed {rel}")
    print(f"current topic: {'/'.join(s.ancestry(nxt))}\nresult doc: {readme}" if nxt is not None else "the stack is empty")


def cmd_prefer(a):
    path = os.path.join(a.folder, "PREFERENCES.md")
    if not os.path.exists(path):
        die(f"{path} does not exist; run init first")
    text = open(path, encoding="utf-8").read().rstrip("\n")
    gap = "\n\n" if text.split("\n")[-1].startswith("## ") else "\n"
    open(path, "w", encoding="utf-8").write(f"{text}{gap}- ({TODAY}) {a.text}\n")
    print(f"recorded in {path}")


def cmd_show(a):
    s = Stack(a.folder)
    print("Current topic:", "\n".join(l for l in s.sections["Current topic"] if l.strip()))
    t = s.top()
    if t is None:
        print("The stack is empty.")
        return
    print(f"\nTop topic: {'/'.join(s.ancestry(t))}")
    print("\n".join(s.lines[t:s.block_end(t)]))
    rest = [TOPIC.match(l)["slug"] for l in s.lines if TOPIC.match(l)]
    print(f"\nOpen topics, in file order: {', '.join(rest)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", help="the discussion folder, such as docs/fleet-network")
    sub = ap.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("init", help="create the folder with STACK.md and PREFERENCES.md")
    p.add_argument("--purpose", required=True, help="the goal of the discussion")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("push", help="push a topic onto the stack")
    p.add_argument("slug")
    p.add_argument("words", help="the user's words for the topic, verbatim")
    p.add_argument("--under", help="nest the topic under this parent topic")
    p.add_argument("--bottom", action="store_true", help="put it last, for a topic the user says runs later")
    p.set_defaults(fn=cmd_push)

    p = sub.add_parser("ask", help="queue a question under a topic")
    p.add_argument("slug")
    p.add_argument("question")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("answer", help="tick a queued question, by its number or by text it contains")
    p.add_argument("slug")
    p.add_argument("match")
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("current", help="make a topic current and create its directory (default: the top topic)")
    p.add_argument("slug", nargs="?")
    p.set_defaults(fn=cmd_current)

    p = sub.add_parser("close", help="move a topic with no open questions or child topics to Closed")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("prefer", help="record a preference for this discussion in PREFERENCES.md")
    p.add_argument("text")
    p.set_defaults(fn=cmd_prefer)

    p = sub.add_parser("show", help="print the current topic, the top topic and the open topics")
    p.set_defaults(fn=cmd_show)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
