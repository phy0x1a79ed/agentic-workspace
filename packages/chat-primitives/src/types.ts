/**
 * Wire types mirroring awm.exposed's /rooms surface. Vendored from
 * frontend/src/lib/api/client.ts so this package has no dependency on the
 * monolith — both the monolith and any stripe import these from here.
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

export interface SlashCommandInfo {
  /** Leading slash, e.g. "/restart". */
  name: string;
  /** Display string, e.g. "[mode]". */
  args: string;
  description: string;
}

export interface SlashCatalog {
  server: SlashCommandInfo[];
  /** Bare claude command names (no leading slash). */
  claude: string[];
}

export interface AgentSlashResult {
  handled: boolean;
  result: string;
}
