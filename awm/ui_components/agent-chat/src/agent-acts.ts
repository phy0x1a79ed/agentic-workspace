/**
 * AgentAct → Post mapping + transcript folding.
 *
 * The agents service streams rich acts (message / partial / tool_use /
 * tool_result / status / result / error). `@awm/tts-history`'s `<TtsHistory>`
 * renders a flat `Post[]` keyed by `kind` (it has special grouping for
 * `tool_use`/`tool_result` and membership; everything else is a "solo" text
 * row). This module is the pure mapping layer between the two — it owns:
 *
 *   - which act kinds become visible posts (and how their `kind` projects onto
 *     the `Post` shape so `<TtsHistory>` collapses tool activity);
 *   - coalescing streamed `partial` acts into the single growing assistant
 *     message they belong to, so the chat shows one live-updating bubble per
 *     turn instead of a flood of partial rows;
 *   - dedupe by act `id` (the backfill/live overlap is intentional — the same
 *     act arrives once in the opening `backfill` frame and possibly again as a
 *     live `act` frame).
 *
 * It is deliberately framework-free (no Svelte) so it's unit-testable and the
 * component just drives it.
 */

import type { AgentAct } from '@awm/client';
import type { Post } from '@awm/tts-history';

/** Author label for every agent-origin row, given the connected scope. */
export function agentAuthor(project: string, scope: string): string {
  return `agent:${project}/${scope}`;
}

/**
 * The logical message id a `partial`/`message` act coalesces under. The agents
 * service tags streamed partials and their final message with a shared id in
 * `meta` (`message_id` / `msg_id` / `turn_id`) so the growing bubble can be
 * replaced in place. When absent we fall back to the act's own id, which makes
 * each partial its own row — degraded but never wrong.
 */
function coalesceKey(act: AgentAct): string {
  const meta = act.meta ?? {};
  const k = meta['message_id'] ?? meta['msg_id'] ?? meta['turn_id'];
  return typeof k === 'string' && k ? k : act.id;
}

/** A tool act renders collapsed/secondary via tts-history's tool grouping. */
const TOOL_KINDS = new Set(['tool_use', 'tool_result']);
/** Kinds we never surface as chat rows (pure stream bookkeeping). */
const HIDDEN_KINDS = new Set(['status', 'partial-noise']);

/**
 * Project one act's kind onto the `Post.kind` `<TtsHistory>` understands.
 * `message`/`partial`/`result` become plain text rows (speakable, grouped as
 * solo); tool acts keep their kind so tts-history collapses them; `error`
 * renders as a system row.
 */
function postKind(actKind: string): string {
  if (TOOL_KINDS.has(actKind)) return actKind;
  if (actKind === 'error') return 'system';
  return 'text';
}

function tsToIso(ts: number | undefined): string {
  if (typeof ts === 'number' && Number.isFinite(ts)) return new Date(ts).toISOString();
  return new Date().toISOString();
}

/** Best-effort rendered body for an act, falling back across shapes. */
function actBody(act: AgentAct): string {
  if (typeof act.body === 'string' && act.body) return act.body;
  const meta = act.meta ?? {};
  const t = meta['text'] ?? meta['name'] ?? meta['tool'];
  return typeof t === 'string' ? t : '';
}

/**
 * Stateful folder: feed it acts (from backfill and live frames, in any
 * interleaving) and read `.posts` for the current transcript view. Dedupes by
 * act id, coalesces partials/final-message by their shared message id, and maps
 * each surviving act to a `Post`. Insertion order is transcript order (acts
 * arrive ordered by (ts, id) within a frame; across frames the live tail is
 * always newer than the backfill).
 */
export class TranscriptFold {
  /** Visible posts, in render order. The component binds this to <TtsHistory>. */
  posts: Post[] = [];

  /** Act ids already folded — the dedupe set across backfill/live overlap. */
  private seenIds = new Set<string>();

  /** coalesceKey → index into `posts` for the growing message bubble. */
  private msgIndex = new Map<string, number>();

  /** The highest act id seen, for the reconnect cursor on `lagged`. */
  lastId: string | undefined;
  /** ISO of the highest-ts act seen, for the reconnect cursor on `lagged`. */
  lastTs: string | undefined;

  /**
   * Fold one act in. Returns the `Post` if a NEW, speakable assistant
   * `message` row was produced (so the caller can auto-speak it), else null.
   * Partials never trigger speech (only the finalized message does), and an
   * already-seen id is a no-op.
   */
  push(act: AgentAct): Post | null {
    if (this.seenIds.has(act.id)) return null;
    this.seenIds.add(act.id);
    this.lastId = act.id;
    this.lastTs = tsToIso(act.ts);

    const kind = act.kind;
    if (HIDDEN_KINDS.has(kind)) return null;

    // message / partial → coalesce into one bubble per logical message.
    if (kind === 'message' || kind === 'partial') {
      const key = coalesceKey(act);
      const post: Post = {
        id: key,
        ts: tsToIso(act.ts),
        author: this.author,
        kind: 'text',
        body: actBody(act),
      };
      const at = this.msgIndex.get(key);
      if (at !== undefined) {
        this.posts[at] = post;
        this.posts = [...this.posts];
        return null; // in-place update of an existing bubble — never re-speak
      }
      this.msgIndex.set(key, this.posts.length);
      this.posts = [...this.posts, post];
      // Speak only finalized messages, not the first partial of a turn.
      return kind === 'message' ? post : null;
    }

    // Everything else (tool_use / tool_result / result / error) is its own row.
    const post: Post = {
      id: act.id,
      ts: tsToIso(act.ts),
      author: this.author,
      kind: postKind(kind),
      body: actBody(act),
    };
    this.posts = [...this.posts, post];
    return null;
  }

  constructor(private author: string) {}
}
