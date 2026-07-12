// Typed wrappers over the notes service surface (/svc/notes/fn/*), built on the
// shared @awm/client. The hub is loopback + unauthenticated, so there is no
// login flow to thread — apiFetch handles JSON encoding and error typing.

import { svc } from '@awm/client';

const notes = svc('notes');

export interface NoteMeta {
  id: string;
  path: string;
  created: string;
  modified: string;
  deleted_at: string | null;
  file_path: string;
  score?: number | null;
  snippet?: string;
}

export interface Note extends NoteMeta {
  content: string;
}

export interface Tree {
  active: NoteMeta[];
  trashed: NoteMeta[];
}

export interface SearchResult {
  count: number;
  results: NoteMeta[];
}

export const notesApi = {
  tree: (includeTrashed = true) =>
    notes.fn<Tree>('tree', { include_trashed: includeTrashed }),

  get: (id: string) => notes.fn<Note>('get', { id, include_content: true }),

  create: (path = '', content = '') =>
    notes.fn<Note>('create', { path, content }),

  save: (id: string, patch: { content?: string; path?: string }) =>
    notes.fn<NoteMeta>('save', { id, ...patch }),

  trash: (id: string) => notes.fn<{ trashed: string }>('trash', { id }),

  restore: (id: string) => notes.fn<NoteMeta>('restore', { id }),

  purge: (id: string) => notes.fn<{ purged: string[] }>('purge', { id }),

  search: (q: { query?: string; fuzzy?: string; semantic?: string; k?: number }) =>
    notes.fn<SearchResult>('search', q),

  vocabList: () => notes.fn<{ terms: string[] }>('vocab_list', {}),
  vocabAdd: (term: string) => notes.fn<{ terms: string[] }>('vocab_add', { term }),
  vocabRemove: (term: string) => notes.fn<{ terms: string[] }>('vocab_remove', { term }),
};
