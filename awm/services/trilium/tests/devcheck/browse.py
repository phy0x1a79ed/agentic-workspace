"""Open the board, Ops and a note in the dev vault and assert what a person would see.

Env awm-queue (playwright). Needs the dev server on 37750, alcor_stub on 37751 and seed.py run.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

VAULT = "http://127.0.0.1:37750"
ALCOR = "http://127.0.0.1:37751"
SHOTS = Path(os.environ.get("SHOTS", "/tmp/awm-shots"))
SHOTS.mkdir(parents=True, exist_ok=True)
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))


def calls() -> list[dict]:
    return json.load(urllib.request.urlopen(f"{ALCOR}/_calls"))


def settle(page) -> None:
    """Trilium restores the tabs of earlier runs; keep only the active one so selectors are unique."""
    page.wait_for_selector(".fancytree-node", state="attached", timeout=60000)
    page.evaluate("glob.appContext.triggerCommand('closeOtherTabs', "
                  "{ntxId: glob.appContext.tabManager.getActiveContext().ntxId})")
    page.wait_for_timeout(700)


def main() -> int:
    lib = os.path.join(sys.prefix, "lib")
    env = {**os.environ, "LD_LIBRARY_PATH": ":".join(filter(None, [lib, os.environ.get("LD_LIBRARY_PATH")]))}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"], env=env)
        ctx = browser.new_context(viewport={"width": 1500, "height": 900})
        ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=VAULT)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # -- the board ----------------------------------------------------------------------
        page.goto(f"{VAULT}/#root/iF0A3SWEGqUx/awmSwarmBoard")
        settle(page)
        for _ in range(4):  # the dev server's on-demand bundler sometimes fails the first module fetch
            try:
                page.locator(".awm-card").locator("visible=true").first.wait_for(timeout=20000)
                break
            except Exception:
                page.screenshot(path=str(SHOTS / "retry-load.png"))
                page.reload()
                settle(page)
        page.locator(".awm-card").locator("visible=true").first.wait_for(timeout=30000)
        heads = [s.inner_text().strip() for s in page.query_selector_all(".awm-sections summary")]
        check("four sections, no Proposed", [h.split(" (")[0] for h in heads] == ["Needs you", "Working", "Stale", "History"], str(heads))
        try:
            urllib.request.urlopen(f"{VAULT}/etapi/notes/awmSwarmColTriage")
            gone = False
        except urllib.error.HTTPError as e:
            gone = e.code == 404
        check("no Swarm triage folder in the vault", gone)
        page.screenshot(path=str(SHOTS / "board-1.png"))

        page.click(".awm-card[data-task='stub-needs']")
        page.wait_for_selector(".awm-info .awm-id")
        info = page.inner_text(".awm-info")
        check("info panel does not repeat the title", "Card stub-needs" not in info, info[:80])
        check("no 'null' in the panel or on the cards", "null" not in info.lower() and "null" not in page.inner_text(".awm-sections").lower())
        chips = page.eval_on_selector(".awm-card[data-task='stub-needs']", "e => [...e.querySelectorAll('.awm-chip')].map(c => c.textContent)")
        check("chips are status and node (plus the approval count)", "blocked" in chips and "mizar" in chips, str(chips))
        links = page.eval_on_selector_all(".awm-info table a", "a => a.map(x => x.href)")
        check("sessions table links to claude.ai", any(h.startswith("https://claude.ai/") for h in links), str(links))
        appr = page.eval_on_selector_all(".awm-appr .awm-chip", "e => e.map(x => x.textContent)")
        check("approvals: pending first, then history", appr[:2] == ["pending", "approved"], str(appr))
        page.click(".awm-info .awm-id")
        clip = page.evaluate("navigator.clipboard.readText()")
        check("clicking the id copies it", clip == "stub-needs", clip)

        before = page.eval_on_selector(".awm-sections", "e => e.getBoundingClientRect().width")
        box = page.eval_on_selector(".awm-divider", "e => { const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; }")
        page.mouse.move(*box)
        page.mouse.down()
        page.mouse.move(box[0] - 220, box[1], steps=8)
        page.mouse.up()
        after = page.eval_on_selector(".awm-sections", "e => e.getBoundingClientRect().width")
        check("the divider resizes rows and panel", after < before - 150, f"{before:.0f} -> {after:.0f}")

        opts = page.eval_on_selector_all(".awm-node option", "o => o.map(x => x.textContent)")
        check("New agent dropdown: fewest 7-day sessions first, no stations, no deneb",
              [o.split(" ")[0] for o in opts] == ["lyra", "altair", "mizar"], str(opts))

        # History: open, scroll the sentinel into view, more than one page arrives.
        page.click(".awm-sections details[data-section='history'] summary")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "history.png"))
        page.wait_for_function("document.querySelectorAll('details[data-section=history] .awm-card').length >= 50")
        # The sentinel is in view as soon as History opens, so the second page follows the first.
        page.wait_for_function("document.querySelectorAll('details[data-section=history] .awm-card').length >= 72")
        n = page.eval_on_selector_all("details[data-section=history] .awm-card", "e => e.length")
        check("History pages in older cards as the sentinel shows (50 per page, 72 cards)", n == 72, str(n))

        # New agent: first press has no cookie -> 401 -> sign-in frame -> retry.
        page.select_option(".awm-node", "lyra")
        seen_frames: list[str] = []
        page.on("framenavigated", lambda f: seen_frames.append(f.url))
        page.click(".awm-newbtn")
        page.wait_for_function("[...document.querySelectorAll('.awm-toast')].some(t => t.textContent.includes('Agent starting'))", timeout=15000)
        check("a 401 opens the sign-in frame, then the call is repeated",
              any("/signin?frame=1" in u for u in seen_frames), str(seen_frames))
        agent = [c for c in calls() if c.get("path") == "/api/agent"]
        check("New agent posts {node} to alcor", agent and agent[-1]["body"] == {"node": "lyra"}, str(agent))

        # Approve opens the modal frame; the modal tells the page.
        page.click(".awm-appr button:text-is('Approve')")
        page.wait_for_selector(".awm-shade iframe")
        src = page.get_attribute(".awm-shade iframe", "src")
        check("Approve opens alcor's modal", "/modal/approve/req-1" in src, src)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "modal.png"))
        print("shade:", page.evaluate("""() => { const s = document.querySelector('.awm-shade'); const c = getComputedStyle(s);
            const r = s.querySelector('iframe').getBoundingClientRect(); return [c.position, c.display, c.zIndex, r.width, r.height, r.x, r.y]; }"""))
        print("frames:", [f.url for f in page.frames], "shades:", page.eval_on_selector_all(".awm-shade", "e => e.length"))
        page.frame_locator(".awm-shade iframe").locator("#go").click()
        page.wait_for_function("!document.querySelector('.awm-shade')")
        check("the modal's postMessage closes it", True)

        page.click(".awm-appr button:text-is('Reject')")
        page.click(".awm-info >> button:text-is('Close')")
        page.wait_for_timeout(500)
        paths = [c.get("path") for c in calls()]
        check("Reject and Close are fetches to the contract routes",
              "/api/request/req-1/reject" in paths and "/api/card/stub-needs/close" in paths, str(paths))
        page.screenshot(path=str(SHOTS / "board-2.png"))
        page.click("button[data-count='2']")
        page.wait_for_selector(".awm-shade iframe")
        check("2fa strip opens the twofa modal", "/modal/twofa?count=2" in page.get_attribute(".awm-shade iframe", "src"))
        page.frame_locator(".awm-shade iframe").locator("#go").click()
        page.wait_for_function("!document.querySelector('.awm-shade')")

        # -- Ops --------------------------------------------------------------------------
        page.goto(f"{VAULT}/#root/iF0A3SWEGqUx/awmSwarmOps")
        settle(page)
        page.wait_for_selector(".awm-ops .awm-node", timeout=30000)
        nodes = page.eval_on_selector_all(".awm-ops .awm-node", "e => e.map(x => x.dataset.node)")
        check("Ops: one card per node", sorted(nodes) == ["altair", "capella", "deneb", "lyra", "mizar"], str(nodes))
        check("Ops: node cards show sessions with claude.ai links",
              page.eval_on_selector_all(".awm-node[data-node=mizar] a", "a => a.some(x => x.href.startsWith('https://claude.ai/'))"))
        check("Ops: seats and services sections",
              page.query_selector_all(".awm-seats tr[data-seat]") and page.inner_text(".awm-services").count("down") >= 1)
        page.fill(".awm-ops .search", "mizr")
        shown = page.eval_on_selector_all(".awm-ops .awm-node", "e => e.map(x => x.dataset.node)")
        check("Ops: the fuzzy search forgives a typo", shown == ["mizar"], str(shown))
        page.fill(".awm-ops .search", "")
        page.check("tr[data-seat='acct/222'] input")
        page.wait_for_timeout(800)
        seat = [c for c in calls() if c.get("path") == "/api/seat/remote_control"]
        check("Ops: the seat switch posts {seat, on}", seat and seat[-1]["body"] == {"seat": "acct/222", "on": True}, str(seat))
        page.screenshot(path=str(SHOTS / "ops.png"), full_page=True)

        # -- the bot button ----------------------------------------------------------------------
        page.goto(f"{VAULT}/#root/iF0A3SWEGqUx/qUFGgLTobIn8/QisB6tefNykl")
        page.wait_for_timeout(1500)
        row = page.locator(".fancytree-node", has_text="Federation update").first
        row.hover()
        page.wait_for_selector(".awm-bot-button", state="attached", timeout=15000)
        check("the bot button sits beside the create-child button",
              page.evaluate("""() => { const b = document.querySelector('.awm-bot-button'); const a = b.parentNode.querySelector('.add-note-button');
                                       return !!a && b.nextElementSibling === a; }"""))
        row.locator(".awm-bot-button").click(force=True)
        page.wait_for_selector(".awm-menu .item")
        items = page.eval_on_selector_all(".awm-menu .item span:first-child", "e => e.map(x => x.textContent)")
        check("the bot dropdown lists the same nodes", items == ["lyra", "altair", "mizar"], str(items))
        page.screenshot(path=str(SHOTS / "bot-menu.png"))
        page.click(".awm-menu .item >> text=altair")
        page.wait_for_timeout(600)
        bot = [c for c in calls() if c.get("path") == "/api/agent" and c["body"].get("note_id")]
        check("the press posts {node, note_id}", bot and bot[-1]["body"]["node"] == "altair" and bot[-1]["body"]["note_id"] == "QisB6tefNykl", str(bot))

        page.goto(f"{VAULT}/#root/iF0A3SWEGqUx/awmSwarmTasks/awmSwarmColNeedsYou/swc737475622d6e65656473")
        page.wait_for_timeout(1500)
        page.locator(".fancytree-node", has_text="Card stub-needs").first.hover()
        page.wait_for_timeout(1500)
        inside = page.evaluate("""() => { const n = [...document.querySelectorAll('.fancytree-node')].find(e => e.textContent.includes('Card stub-needs'));
                                           return n ? n.querySelectorAll('.awm-bot-button').length : -1; }""")
        check("a card's note has no bot button", inside == 0, str(inside))
        check("no page errors", not [e for e in errors if "awm" in e.lower() or "swarm" in e.lower()], str(errors[:5]))
        browser.close()
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)} passed, {len(bad)} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
