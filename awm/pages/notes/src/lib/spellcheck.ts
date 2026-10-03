// Spellchecking for the CodeMirror editor — a Hunspell (nspell) checker rendered
// as underline decorations, with a right-click menu offering suggestions,
// "Add to dictionary", and "Ignore". This is the piece a <textarea> could never
// do (no way to underline a single word or attach a per-word menu).
//
// The custom dictionary IS the note vocabulary (the same `vocab_*` store that
// feeds whisper dictation hotwords): terms already in it are never flagged, and
// "Add to dictionary" routes through `onAddWord` so the two stay one list.
//
// Single-editor assumption: the page shows one editor at a time, so the parsed
// nspell instance and the personal-word set are module singletons (the ~550KB
// dictionary is parsed once, lazily, on first use).

import nspell from 'nspell';
import affUrl from './dict/en.aff?url';
import dicUrl from './dict/en.dic?url';
import {
  EditorView,
  ViewPlugin,
  Decoration,
  type DecorationSet,
  type ViewUpdate,
} from '@codemirror/view';
import { RangeSetBuilder, StateEffect, type Extension } from '@codemirror/state';
import { syntaxTree } from '@codemirror/language';
import type { SyntaxNode } from '@lezer/common';

/** The nspell instance type (the package uses `export =`, so derive it). */
type Spell = ReturnType<typeof nspell>;

// ---- shared dictionary state (one editor at a time) -------------------------

let spell: Spell | null = null;
let spellPromise: Promise<Spell> | null = null;
/** Words the checker must accept: the note vocabulary + session "ignore"s. */
const personal = new Set<string>();
/** The mounted editor, so vocab/dictionary changes can trigger a re-scan. */
let activeView: EditorView | null = null;

function loadSpell(): Promise<Spell> {
  if (!spellPromise) {
    spellPromise = Promise.all([
      fetch(affUrl).then((r) => r.text()),
      fetch(dicUrl).then((r) => r.text()),
    ]).then(([aff, dic]) => {
      const s = nspell(aff, dic);
      for (const w of personal) s.add(w);
      spell = s;
      return s;
    });
  }
  return spellPromise;
}

/** Straight-quote normalize + trim edge punctuation so "don’t" / "word," check. */
function normalize(word: string): string {
  return word.replace(/[’]/g, "'").replace(/^['-]+|['-]+$/g, '');
}
function accepted(word: string): boolean {
  return personal.has(word) || personal.has(word.toLowerCase());
}
function addPersonal(word: string) {
  const n = normalize(word);
  if (!n) return;
  personal.add(n);
  spell?.add(n);
}

// ---- decoration scan --------------------------------------------------------

const spellMark = Decoration.mark({ class: 'cm-spell-error' });
const refreshEffect = StateEffect.define<null>();
// Words: letters with internal apostrophes/hyphens; no digits (skips ids/hex).
const WORD_RE = /[A-Za-z](?:[A-Za-z'’-]*[A-Za-z])?/g;
// Markdown/code nodes whose text must not be spellchecked.
const SKIP_NODES = new Set([
  'FencedCode', 'CodeBlock', 'CodeText', 'InlineCode', 'CodeMark', 'CodeInfo',
  'URL', 'Autolink', 'LinkMark', 'Image', 'HTMLBlock', 'HTMLTag', 'Comment',
  'Entity', 'Escape', 'Table',
]);

function inSkippedNode(view: EditorView, pos: number): boolean {
  let node: SyntaxNode | null = syntaxTree(view.state).resolveInner(pos, 1);
  for (; node; node = node.parent) if (SKIP_NODES.has(node.name)) return true;
  return false;
}

function buildDecorations(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  if (!spell) return builder.finish();
  for (const range of view.visibleRanges) {
    let pos = range.from;
    while (pos <= range.to) {
      const line = view.state.doc.lineAt(pos);
      WORD_RE.lastIndex = 0;
      let m: RegExpExecArray | null;
      while ((m = WORD_RE.exec(line.text))) {
        const wFrom = line.from + m.index;
        const wTo = wFrom + m[0].length;
        if (wTo < range.from) continue;
        if (wFrom > range.to) break;
        const norm = normalize(m[0]);
        if (norm.length < 2) continue;
        if (accepted(norm) || spell.correct(norm)) continue;
        if (inSkippedNode(view, wFrom)) continue;
        builder.add(wFrom, wTo, spellMark);
      }
      pos = line.to + 1;
    }
  }
  return builder.finish();
}

/** Find the word (letters/apostrophes/hyphens) surrounding a document position. */
function wordAt(view: EditorView, pos: number): { from: number; to: number; word: string } | null {
  const line = view.state.doc.lineAt(pos);
  const col = pos - line.from;
  const isW = (c: string) => /[A-Za-z'’-]/.test(c);
  let a = col;
  let b = col;
  const text = line.text;
  while (a > 0 && isW(text[a - 1])) a--;
  while (b < text.length && isW(text[b])) b++;
  if (b <= a) return null;
  return { from: line.from + a, to: line.from + b, word: text.slice(a, b) };
}

// ---- right-click menu (rendered to body; styled in styles.css) --------------

let currentMenu: HTMLElement | null = null;
function removeMenu() {
  if (currentMenu) {
    currentMenu.remove();
    currentMenu = null;
    document.removeEventListener('mousedown', onDocDown, true);
    document.removeEventListener('keydown', onEsc, true);
  }
}
function onDocDown(e: MouseEvent) {
  if (currentMenu && !currentMenu.contains(e.target as Node)) removeMenu();
}
function onEsc(e: KeyboardEvent) {
  if (e.key === 'Escape') removeMenu();
}

// ---- public controller ------------------------------------------------------

export interface SpellController {
  extension: Extension;
  /** Seed/refresh the personal dictionary from the note vocabulary. */
  setVocab(terms: string[]): void;
  destroy(): void;
}

export function createSpellchecker(opts: {
  /** Persist a word to the shared vocabulary (whisper hotwords + dictionary). */
  onAddWord: (word: string) => void | Promise<void>;
}): SpellController {
  function refresh() {
    activeView?.dispatch({ effects: refreshEffect.of(null) });
  }

  async function addToDictionary(word: string) {
    addPersonal(word);
    refresh();
    try { await opts.onAddWord(normalize(word)); } catch { /* persist best-effort */ }
  }
  function ignore(word: string) {
    addPersonal(word); // session-only: not persisted to the vocabulary
    refresh();
  }

  function openMenu(view: EditorView, event: MouseEvent, from: number, to: number, word: string) {
    event.preventDefault();
    removeMenu();
    const menu = document.createElement('div');
    menu.className = 'nt-spell-menu';
    menu.style.left = `${event.clientX}px`;
    menu.style.top = `${event.clientY}px`;

    const suggestions = (spell?.suggest(word) ?? []).slice(0, 7);
    if (suggestions.length) {
      for (const s of suggestions) {
        const b = document.createElement('button');
        b.className = 'nt-spell-item';
        b.textContent = s;
        b.onclick = () => {
          view.dispatch({ changes: { from, to, insert: s } });
          removeMenu();
          view.focus();
        };
        menu.appendChild(b);
      }
    } else {
      const empty = document.createElement('div');
      empty.className = 'nt-spell-empty';
      empty.textContent = 'No suggestions';
      menu.appendChild(empty);
    }

    const sep = document.createElement('div');
    sep.className = 'nt-spell-sep';
    menu.appendChild(sep);

    const add = document.createElement('button');
    add.className = 'nt-spell-item nt-spell-action';
    add.textContent = 'Add to dictionary';
    add.onclick = () => { removeMenu(); void addToDictionary(word); view.focus(); };
    menu.appendChild(add);

    const ign = document.createElement('button');
    ign.className = 'nt-spell-item nt-spell-action';
    ign.textContent = 'Ignore';
    ign.onclick = () => { removeMenu(); ignore(word); view.focus(); };
    menu.appendChild(ign);

    document.body.appendChild(menu);
    currentMenu = menu;
    // Defer listener attach so this same click doesn't immediately close it.
    setTimeout(() => {
      document.addEventListener('mousedown', onDocDown, true);
      document.addEventListener('keydown', onEsc, true);
    });
  }

  const plugin = ViewPlugin.fromClass(
    class {
      view: EditorView;
      decorations: DecorationSet = Decoration.none;
      constructor(view: EditorView) {
        this.view = view;
        activeView = view;
        if (spell) {
          this.decorations = buildDecorations(view);
        } else {
          void loadSpell().then(() => view.dispatch({ effects: refreshEffect.of(null) }));
        }
      }
      update(u: ViewUpdate) {
        const forced = u.transactions.some((tr) =>
          tr.effects.some((e) => e.is(refreshEffect)),
        );
        if (u.docChanged || u.viewportChanged || forced) {
          this.decorations = buildDecorations(u.view);
        }
      }
      destroy() {
        if (activeView === this.view) activeView = null;
        removeMenu();
      }
    },
    {
      decorations: (v) => v.decorations,
      eventHandlers: {
        contextmenu(event: MouseEvent, view: EditorView) {
          if (!spell) return false;
          const pos = view.posAtCoords({ x: event.clientX, y: event.clientY });
          if (pos == null) return false;
          const w = wordAt(view, pos);
          if (!w) return false;
          const norm = normalize(w.word);
          if (norm.length < 2 || accepted(norm) || spell.correct(norm)) return false;
          if (inSkippedNode(view, w.from)) return false;
          openMenu(view, event, w.from, w.to, norm);
          return true;
        },
      },
    },
  );

  return {
    extension: plugin,
    setVocab(terms: string[]) {
      let added = false;
      for (const term of terms) {
        for (const word of term.split(/\s+/)) {
          const n = normalize(word);
          if (n && !personal.has(n)) { addPersonal(n); added = true; }
        }
      }
      if (added) refresh();
    },
    destroy() {
      removeMenu();
      activeView = null;
    },
  };
}
