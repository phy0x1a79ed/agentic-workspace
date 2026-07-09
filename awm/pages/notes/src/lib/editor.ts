// CodeMirror 6 editor for one open note. Replaces the former transparent-
// textarea + <pre> highlight overlay: CM highlights in-editor (per-language
// inside fenced code blocks via `codeLanguages`), auto-closes brackets, and
// continues list/blockquote markup on Enter — none of which a <textarea> can do.
//
// The document stays a plain string, so the collab 3-way merge (collab.ts) and
// dictation-at-caret (dictation.ts) are unchanged in substance; this module just
// exposes the imperative seam App.svelte drives them through.
//
// The load-bearing detail is the `Remote` annotation. Every transaction that
// carries programmatic text NOT originated by the local user — a collab merge or
// a note switch — is tagged `Remote`. The change listener re-emits only
// UN-tagged (genuinely local) edits back to the app, so a merged peer edit never
// echoes back out as a fresh local edit (which would feed-back-loop the room),
// while a dictated phrase (untagged, a real local edit) does persist.

import {
  EditorState,
  Annotation,
  Prec,
  type Extension,
  type ChangeSpec,
} from '@codemirror/state';
import { EditorView, keymap, placeholder, drawSelection } from '@codemirror/view';
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentWithTab,
} from '@codemirror/commands';
import {
  syntaxHighlighting,
  HighlightStyle,
  indentOnInput,
  bracketMatching,
} from '@codemirror/language';
import { closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete';
import { markdown, markdownLanguage } from '@codemirror/lang-markdown';
import { languages } from '@codemirror/language-data';
import { tags as t } from '@lezer/highlight';
import {
  diff_match_patch,
  DIFF_DELETE,
  DIFF_INSERT,
  DIFF_EQUAL,
} from 'diff-match-patch';

/** Marks a transaction as programmatic (collab merge / note switch), so the
 *  change listener does NOT re-report it as a local edit. */
const Remote = Annotation.define<boolean>();

// ---- theme + syntax colours (mapped to the page-local --nt-* tokens) --------

const theme = EditorView.theme({
  '&': {
    color: 'var(--nt-text)',
    backgroundColor: 'var(--nt-surface)',
    height: '100%',
  },
  '&.cm-focused': { outline: 'none' },
  '.cm-scroller': {
    fontFamily: 'var(--nt-mono)',
    fontSize: '0.94rem',
    lineHeight: '1.75',
    overflow: 'auto',
    scrollbarWidth: 'thin',
    scrollbarColor: 'var(--nt-faint) transparent',
  },
  '.cm-content': {
    padding: '1.6rem clamp(1.1rem, 6vw, 4rem)',
    caretColor: 'var(--nt-text)',
  },
  '.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--nt-text)' },
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
    { backgroundColor: 'var(--nt-select)' },
  '.cm-placeholder': { color: 'var(--nt-faint)', fontStyle: 'normal' },
  '.cm-matchingBracket': {
    backgroundColor: 'var(--nt-accent-soft)',
    outline: '1px solid color-mix(in oklab, var(--nt-accent) 40%, transparent)',
  },
  // Misspelling underline (spellcheck.ts marks words with this class).
  '.cm-spell-error': {
    textDecoration: 'underline wavy var(--nt-danger)',
    textDecorationSkipInk: 'none',
    textUnderlineOffset: '2px',
  },
});

const highlightStyle = HighlightStyle.define([
  // markdown structure — the palette the old .markdown-hl .hljs-* rules carried
  { tag: t.heading, color: 'var(--nt-accent)', fontWeight: '700' },
  { tag: t.strong, color: 'var(--nt-text)', fontWeight: '700' },
  { tag: t.emphasis, color: 'var(--nt-text2)', fontStyle: 'italic' },
  { tag: t.strikethrough, color: 'var(--nt-faint)', textDecoration: 'line-through' },
  { tag: t.list, color: 'var(--nt-accent)' },
  { tag: t.quote, color: 'var(--nt-text2)', fontStyle: 'italic' },
  { tag: [t.link, t.url], color: 'var(--nt-accent)' },
  { tag: t.monospace, color: 'var(--nt-live)' }, // inline code + fence markers
  { tag: t.contentSeparator, color: 'var(--nt-faint)' }, // ---
  // fenced code-block tokens (nested languages via codeLanguages) — kept in the
  // same --nt-* family so a code block reads as part of the page, not pasted in.
  { tag: t.keyword, color: 'var(--nt-accent)' },
  { tag: [t.string, t.special(t.string)], color: 'var(--nt-live)' },
  { tag: [t.number, t.bool, t.null, t.atom], color: 'var(--nt-live)' },
  { tag: t.comment, color: 'var(--nt-faint)', fontStyle: 'italic' },
  { tag: [t.function(t.variableName), t.function(t.propertyName)], color: 'var(--nt-text)' },
  { tag: [t.typeName, t.className, t.namespace], color: 'var(--nt-accent)' },
  { tag: [t.propertyName, t.attributeName], color: 'var(--nt-text2)' },
  { tag: [t.operator, t.punctuation, t.separator, t.bracket], color: 'var(--nt-text2)' },
  { tag: [t.tagName, t.angleBracket], color: 'var(--nt-accent)' },
  { tag: t.meta, color: 'var(--nt-faint)' },
  { tag: t.invalid, color: 'var(--nt-danger)' },
]);

// ---- diff → minimal ChangeSet ----------------------------------------------

const dmp = new diff_match_patch();

/** Convert `a → b` into minimal CM changes in ORIGINAL-doc coordinates, so CM
 *  maps the selection/caret through them (this is what replaces the hand-rolled
 *  mapCaret). Adjacent delete+insert are merged into one replacement range. */
function diffToChanges(a: string, b: string): ChangeSpec[] {
  const diffs = dmp.diff_main(a, b);
  dmp.diff_cleanupSemantic(diffs);
  const changes: ChangeSpec[] = [];
  let pos = 0;
  for (let i = 0; i < diffs.length; i++) {
    const [op, data] = diffs[i];
    if (op === DIFF_EQUAL) {
      pos += data.length;
    } else if (op === DIFF_DELETE) {
      const next = diffs[i + 1];
      if (next && next[0] === DIFF_INSERT) {
        changes.push({ from: pos, to: pos + data.length, insert: next[1] });
        i++; // consume the paired insert
      } else {
        changes.push({ from: pos, to: pos + data.length, insert: '' });
      }
      pos += data.length;
    } else {
      // pure insert (no preceding delete) — anchor at the current original pos
      changes.push({ from: pos, to: pos, insert: data });
    }
  }
  return changes;
}

// ---- public API -------------------------------------------------------------

export interface NoteEditorOptions {
  /** Initial document. */
  doc?: string;
  /** Placeholder shown when the doc is empty. */
  placeholder?: string;
  /** Fires on every doc change. `local` is false for programmatic (Remote)
   *  changes — the app should only treat `local === true` as a user edit. */
  onChange: (text: string, local: boolean) => void;
  /** Extra extensions (e.g. the spellchecker) appended to the base set. */
  extensions?: Extension[];
}

export interface NoteEditor {
  view: EditorView;
  /** Replace the whole document (note switch), caret to start. Remote. */
  setDoc(text: string): void;
  /** Fold authoritative text in as a minimal diff (collab merge). Remote. */
  applyText(next: string): void;
  /** Insert at the caret (dictation) — a genuine LOCAL edit. */
  insertAtCaret(raw: string): void;
  getText(): string;
  focus(): void;
  /** Re-measure after being shown again (edit⇄preview toggle un-hides it). */
  remeasure(): void;
  destroy(): void;
}

export function createNoteEditor(parent: HTMLElement, opts: NoteEditorOptions): NoteEditor {
  const baseExtensions = (): Extension[] => [
    history(),
    drawSelection(),
    EditorView.lineWrapping,
    indentOnInput(),
    bracketMatching(),
    closeBrackets(),
    markdown({ base: markdownLanguage, codeLanguages: languages }),
    syntaxHighlighting(highlightStyle),
    theme,
    placeholder(opts.placeholder ?? ''),
    // markdown()'s own keymap (list continuation) is installed by addKeymap;
    // give closeBrackets + history + defaults the remaining bindings.
    Prec.high(keymap.of(closeBracketsKeymap)),
    keymap.of([...historyKeymap, indentWithTab, ...defaultKeymap]),
    EditorView.updateListener.of((update) => {
      if (!update.docChanged) return;
      // Transaction-less updates (e.g. an external setState) are programmatic;
      // otherwise a change is local only if no transaction is Remote-annotated.
      const isRemote =
        update.transactions.length === 0 ||
        update.transactions.some((tr) => tr.annotation(Remote));
      opts.onChange(update.state.doc.toString(), !isRemote);
    }),
    ...(opts.extensions ?? []),
  ];

  const makeState = (doc: string) =>
    EditorState.create({ doc, extensions: baseExtensions() });

  const view = new EditorView({ state: makeState(opts.doc ?? ''), parent });

  return {
    view,

    setDoc(text: string) {
      // A Remote-annotated full replace (not setState) so the change listener
      // fires exactly once and sees the annotation → not a local edit. Caret to
      // the start of the freshly-loaded note.
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: text },
        selection: { anchor: 0 },
        annotations: Remote.of(true),
      });
    },

    applyText(next: string) {
      const cur = view.state.doc.toString();
      if (cur === next) return;
      view.dispatch({
        changes: diffToChanges(cur, next),
        annotations: Remote.of(true),
      });
    },

    insertAtCaret(raw: string) {
      const sel = view.state.selection.main;
      const before = sel.from > 0 ? view.state.doc.sliceString(sel.from - 1, sel.from) : '';
      let insert = raw;
      if (before && !/\s/.test(before) && !/^\s/.test(insert)) insert = ' ' + insert;
      // No Remote annotation → reported as a local edit, so it persists/streams.
      view.dispatch(view.state.replaceSelection(insert));
      view.focus();
    },

    getText() {
      return view.state.doc.toString();
    },

    focus() {
      view.focus();
    },

    remeasure() {
      view.requestMeasure();
    },

    destroy() {
      view.destroy();
    },
  };
}
