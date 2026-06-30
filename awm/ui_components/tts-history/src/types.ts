/**
 * Wire shape for a rendered transcript row. Structurally compatible with the
 * scope channel's `ScopePost` (from `@awm/client`) and the agents service's
 * acts, so posts/acts from either side drop straight into `<TtsHistory>`.
 * Vendored here so any page or component that renders a transcript imports
 * `Post` from one place.
 */

export interface Post {
  id?: string;
  ts: string;
  /** Author scope identifier — backend field name is `author`. */
  author: string;
  /** Recipient — single scope or list. Backend may omit on broadcast. */
  to_scope?: string | string[];
  /** Post text — backend field name is `body`. */
  body: string;
  kind?: string;
  /**
   * Lifecycle state for a chip that has one. Two disjoint vocabularies keyed
   * off the value: a human turn is `sending | sent | received | failed`; a tool
   * call is `running | done | error`. Absent on rows that have no lifecycle
   * (agent text, system, membership).
   */
  status?: string;
  [k: string]: unknown;
}
