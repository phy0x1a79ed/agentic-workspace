/**
 * Typed view of the drawio service's verb surface.
 *
 * Everything goes through `svc('drawio').fn(...)` — the standard gateway
 * client — so the awm_session cookie and the caller identity header travel
 * uniformly, and the page never hand-rolls a fetch.
 */

import { svc } from '@awm/client';

const drawio = svc('drawio');

export interface Page {
  id: string | null;
  name: string | null;
  cells: number;
}

export interface Checkout {
  id: string;
  save: string;
  base_rev: string | null;
  author: string;
  created: number;
  updated: number;
  state: 'clean' | 'conflicted';
  conflicts: number;
  note: string;
}

export interface Diagram {
  save: string;
  bytes: number;
  pages: Page[];
  rev: string | null;
  author: string | null;
  label: string | null;
  modified: number | null;
  checkouts: Checkout[];
  editors: number;
  error?: string;
}

export interface Revision {
  rev: string;
  author: string;
  label: string;
  when: number;
}

export interface CheckProblem {
  path: string;
  problem: 'missing' | 'masked';
  fix: string;
}

export interface CheckReport {
  save: string;
  references: number;
  problems: CheckProblem[];
  ok: boolean;
}

export const list = () => drawio.fn<{ diagrams: Diagram[]; count: number }>('list');

export const info = (save: string) => drawio.fn<Diagram & { url: string }>('info', { save });

export const history = (save: string, limit = 50) =>
  drawio.fn<{ save: string; revisions: Revision[] }>('history', { save, limit });

export const restore = (save: string, rev: string) =>
  drawio.fn<{ save: string; rev: string }>('restore', { save, rev });

export const create = (save: string) =>
  drawio.fn<{ save: string; url: string }>('create', { save });

export const remove = (save: string) => drawio.fn<unknown>('remove', { save });

export const check = (save: string) => drawio.fn<CheckReport>('check', { save });

export const editorUrl = (save: string) =>
  drawio.fn<{ url: string }>('url', { save }).then((r) => r.url);

export const checkoutUrl = (handle: string) =>
  drawio.fn<{ url: string }>('url', { handle }).then((r) => r.url);

export const discardCheckout = (handle: string) =>
  drawio.fn<unknown>('discard', { handle });

/**
 * A diagram path split into folders plus a leaf, which is how the tree is
 * built. The store's paths are already canonical POSIX, so this is a plain
 * split rather than any kind of normalization.
 */
export interface TreeNode {
  name: string;
  path: string;
  children: TreeNode[];
  diagram?: Diagram;
}

export function buildTree(diagrams: Diagram[]): TreeNode[] {
  const root: TreeNode = { name: '', path: '', children: [] };
  for (const diagram of diagrams) {
    const parts = diagram.save.split('/');
    let node = root;
    parts.forEach((part, i) => {
      const isLeaf = i === parts.length - 1;
      const path = parts.slice(0, i + 1).join('/');
      let next = node.children.find((c) => c.name === part && !!c.diagram === isLeaf);
      if (!next) {
        next = { name: part, path, children: [] };
        node.children.push(next);
      }
      if (isLeaf) next.diagram = diagram;
      node = next;
    });
  }
  // Folders before files, alphabetical within each — stable regardless of the
  // order the service happened to list them in.
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      const folder = Number(!!b.diagram) - Number(!!a.diagram);
      return folder !== 0 ? folder : a.name.localeCompare(b.name);
    });
    nodes.forEach((n) => sort(n.children));
  };
  sort(root.children);
  return root.children;
}

export function ago(seconds: number | null): string {
  if (!seconds) return '—';
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return 'just now';
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

export function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
