// The bot button (queue.md R63 item 3): a New agent button beside Trilium's create-child button on
// every ordinary note. It opens the node dropdown, then asks alcor to start an agent with the note
// as context. A card's own note and the writer's notes get none. A MutationObserver adds the button
// when the tree draws a row, so nothing here waits on a clock.

const origin = await swarmOrigin();
const SKIP_LABELS = ["swarmCard", "awmMarker", "agentManaged", "swarmBoard", "swarmOps"];

function hasSkipLabel(note) {
  return !note || SKIP_LABELS.some((l) => note.hasLabel(l));
}

function underTasks(node) {
  for (let p = node; p; p = p.parent) if (p.data && p.data.noteId === TASKS_ID) return true;
  return false;
}

let menu = null;
function closeMenu() { if (menu) { menu.remove(); menu = null; } }
document.addEventListener("mousedown", (ev) => { if (menu && !menu.contains(ev.target)) closeMenu(); }, true);

async function openMenu(anchor, noteId) {
  closeMenu();
  const nodes = await nodeChoices();
  const rect = anchor.getBoundingClientRect();
  menu = el("div", { class: "awm-menu", role: "menu" }, el("div", { class: "head" }, "New agent on…"),
    ...(nodes.length ? nodes.map((n) => el("div", { class: "item", role: "menuitem", onclick: async () => {
      closeMenu();
      try {
        const out = await swarmCall(origin, "/api/agent", { node: n.name, note_id: noteId });
        toast(`Agent starting on ${n.name}${out.task_id ? ` (${out.task_id})` : ""}`);
      } catch (e) { toast(e.message, true); }
    } }, el("span", {}, n.name), el("small", {}, `${n.sessions} in 7d`)))
      : [el("div", { class: "head" }, "No node in the roster.")]));
  menu.style.left = `${Math.min(rect.left, window.innerWidth - 220)}px`;
  menu.style.top = `${rect.bottom + 2}px`;
  document.body.append(menu);
}

function decorate(add) {
  const row = add.closest(".fancytree-node, li");
  if (!row || row.querySelector(".awm-bot-button")) return;
  let node = null;
  try { node = window.$.ui.fancytree.getNode(add); } catch (e) { /* not a tree row */ }
  const noteId = node && node.data && node.data.noteId;
  if (!noteId || underTasks(node)) return;
  const bot = el("span", { class: "tree-item-button tn-icon awm-bot-button bx bx-bot", title: "New agent with this note as context" });
  bot.addEventListener("mousedown", (ev) => { ev.stopPropagation(); ev.preventDefault(); });
  bot.addEventListener("click", (ev) => {
    ev.stopPropagation();
    ev.preventDefault();
    openMenu(bot, noteId);
  });
  add.parentNode.insertBefore(bot, add);
  // The label check needs the note, which may not be in the cache yet; drop the button if it is managed.
  api.getNote(noteId).then((note) => { if (hasSkipLabel(note)) bot.remove(); });
}

function scan(root) {
  if (root.nodeType !== 1) return;
  if (root.matches && root.matches(".add-note-button")) decorate(root);
  if (root.querySelectorAll) for (const add of root.querySelectorAll(".add-note-button")) decorate(add);
}

new MutationObserver((records) => {
  for (const r of records) for (const added of r.addedNodes) scan(added);
}).observe(document.body, { childList: true, subtree: true });
scan(document.body);
