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
import type { Post } from '@awm/chat';

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
/**
 * Kinds we never surface as chat rows. `status` is pure stream bookkeeping;
 * `result` is the terminal turn echo whose text duplicates the final message —
 * hiding it kills the duplicate final line (its usage/session meta is still
 * persisted upstream, just not re-rendered). `partial` stays visible: it now
 * drives the single growing assistant bubble.
 */
const HIDDEN_KINDS = new Set(['status', 'result', 'partial-noise']);

/**
 * Project one act's kind onto the `Post.kind` `<TtsHistory>` understands.
 * `message`/`partial` become plain text rows (speakable, grouped as solo) and
 * are handled by the fold directly; tool acts keep their kind so tts-history
 * collapses them; `error` renders as a system row. (`result`/`status` are
 * hidden before they reach here.)
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

/** One folded entry: the rendered post plus its sort key (ts, then arrival). */
interface FoldEntry {
  ts: number;
  seq: number;
  post: Post;
}

/**
 * Stateful folder: feed it acts (from backfill and live frames, in any
 * interleaving) and read `.posts` for the current transcript view.
 *
 * It is an **idempotent, time-ordered upsert**. Each act maps to a stable
 * `key`: message/partial acts key on their shared `message_id` (so a turn's
 * partials and its finalized message all land on one row); every other act
 * keys on its own id. For a known key a later update replaces the row in place;
 * a stale or duplicate update (ts not newer) is a no-op; a new key is slotted
 * into the transcript at the position sorted by `ts`. The (key, ts) upsert is
 * what makes the stream dedupe by construction — a flood of partials, an
 * out-of-order frame, or a backfill/live overlap all converge to one stable
 * row.
 */
export class TranscriptFold {
  /** Visible posts, in render order (sorted by ts, then arrival). */
  posts: Post[] = [];

  /** key → folded entry (the upsert table). */
  private entries = new Map<string, FoldEntry>();

  /** Monotonic arrival counter — the tiebreak for equal-ts rows. */
  private seq = 0;

  /** The id of the most recent act folded, for the reconnect cursor on `lagged`. */
  lastId: string | undefined;
  /** ISO of the most recent act's ts, for the reconnect cursor on `lagged`. */
  lastTs: string | undefined;

  /**
   * Fold one act in. Returns the `Post` when a finalized assistant `message`
   * was folded (so the caller may auto-speak it — the caller dedupes repeat
   * speech by post id), else null. Partials never trigger speech; a stale /
   * duplicate update is a no-op.
   */
  push(act: AgentAct): Post | null {
    // Cursor tracking advances for every act (including hidden ones) so a
    // reconnect resumes past exactly what we've consumed.
    this.lastId = act.id;
    this.lastTs = tsToIso(act.ts);

    const kind = act.kind;
    if (HIDDEN_KINDS.has(kind)) return null;

    const isMsg = kind === 'message' || kind === 'partial';
    const key = isMsg ? coalesceKey(act) : act.id;
    const ts = typeof act.ts === 'number' && Number.isFinite(act.ts) ? act.ts : 0;

    const existing = this.entries.get(key);
    // Stale / out-of-order: an update older than what we already have is dropped.
    if (existing && ts < existing.ts) return null;

    const post: Post = {
      id: key,
      ts: tsToIso(act.ts),
      author: this.author,
      kind: isMsg ? 'text' : postKind(kind),
      body: actBody(act),
    };
    // Keep the original arrival slot on an in-place update; new keys append.
    const seq = existing ? existing.seq : this.seq++;
    this.entries.set(key, { ts, seq, post });
    this.render();

    // Speak only the finalized message (not partials). The caller dedupes
    // repeat speech by post id, so returning it on an idempotent re-fold is safe.
    return kind === 'message' ? post : null;
  }

  /** Rebuild `posts` time-ordered (ts asc, arrival tiebreak). */
  private render(): void {
    this.posts = [...this.entries.values()]
      .sort((a, b) => a.ts - b.ts || a.seq - b.seq)
      .map((e) => e.post);
  }

  private author: string;

  constructor(author: string) {
    this.author = author;
  }
}
