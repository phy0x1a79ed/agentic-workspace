/**
 * Wire types mirroring awm.exposed's /rooms surface. Vendored here so any
 * page or component that talks to /rooms imports `Post` from one place.
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
  [k: string]: unknown;
}
