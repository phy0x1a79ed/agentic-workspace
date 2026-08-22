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
 * The URL that serves one page as a live SVG. The service owns the encoding
 * and validates the page name, so an unknown page fails here rather than
 * becoming a 404 discovered after the image has been placed.
 */
export const viewUrl = (save: string, page?: string | null) =>
  drawio.fn<{ url: string; save: string; page: string | null }>(
    'view_url', page ? { save, page } : { save });

// --- autopublish links -----------------------------------------------------

export interface Link {
  id: string;
  save: string;
  target: string;
  format: string;
  page: string | null;
  scale: number;
  author: string;
  created_at: number;
  last_rev: string | null;
  last_published_at: number | null;
}

/** What one link's render attempt did. `published: false` is a REPORTED
 *  outcome on a resolved promise, not an exception — treating the absence of a
 *  throw as success is how a link that never rendered looks fine. */
export interface PublishResult {
  id: string;
  save: string;
  target: string;
  published: boolean;
  reason?: string;
  deleted?: boolean;
  bytes?: number;
  rev?: string | null;
}

export const autopublish = (
  save: string, target: string, page?: string | null, format = 'svg',
) => drawio.fn<Link & { first_publish: PublishResult }>('autopublish', {
  save, target, format, ...(page ? { page } : {}),
});

export const autopublishList = (save?: string) =>
  drawio.fn<{ links: Link[]; count: number }>(
    'autopublish_list', save ? { save } : {});

export const autopublishStop = (id: string) =>
  drawio.fn<Link & { stopped: boolean }>('autopublish_stop', { id });

export const autopublishNow = (id: string) =>
  drawio.fn<{ results: PublishResult[]; count: number; published: number }>(
    'autopublish_now', { id });

// --- clipboard -------------------------------------------------------------

/**
 * A drawio-importable fragment holding one image cell.
 *
 * Pasting plain text that starts with `<` runs drawio's `isCompatibleString`
 * → `importXml` → `importGraphModel` path, so this places as a cell with no
 * Insert dialog. The two structural cells are drawio's own copy format.
 *
 * `imageAspect=1` is deliberate: the client patch forces aspect preservation
 * for view images anyway, but that only re-runs when the source next changes —
 * until then the pasted geometry is what the user sees.
 */
export function imageCellXml(url: string, width: number, height: number): string {
  const style =
    `shape=image;verticalLabelPosition=bottom;verticalAlign=top;` +
    `imageAspect=1;aspect=fixed;image=${url};`;
  return (
    '<mxGraphModel><root>' +
    '<mxCell id="0" /><mxCell id="1" parent="0" />' +
    `<mxCell id="2" value="" style="${xmlAttr(style)}" vertex="1" parent="1">` +
    `<mxGeometry x="0" y="0" width="${Math.round(width)}" ` +
    `height="${Math.round(height)}" as="geometry" />` +
    '</mxCell></root></mxGraphModel>'
  );
}

function xmlAttr(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Resolve an origin-relative service URL against this page's origin — a
 *  pasted cell has to work from a phone on the LAN, not just the host. */
export const absolute = (url: string) => new URL(url, window.location.href).href;

/** The rendered page's own dimensions, so a pasted cell is not an arbitrary box.
 *  This is a real headless-Chrome render on a cold cache, hence slow and hence
 *  worth surfacing when it fails. Falls back to a plausible default size. */
export async function renderSize(
  url: string,
): Promise<{ width: number; height: number }> {
  const fallback = { width: 480, height: 320 };
  const response = await fetch(url, { headers: { Accept: 'image/svg+xml' } });
  if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
  const svg = new DOMParser()
    .parseFromString(await response.text(), 'image/svg+xml')
    .querySelector('svg');
  if (!svg) return fallback;

  const box = (svg.getAttribute('viewBox') ?? '').split(/[\s,]+/).map(Number);
  let width = parseFloat(svg.getAttribute('width') ?? '');
  let height = parseFloat(svg.getAttribute('height') ?? '');
  if (!(width > 0) || !(height > 0)) {
    width = box[2];
    height = box[3];
  }
  if (!(width > 0) || !(height > 0)) return fallback;

  // Scale down oversized renders but never up — a placed image that lands
  // bigger than the canvas is worse than one you have to enlarge.
  const max = 640;
  const factor = Math.min(1, max / width, max / height);
  return { width: width * factor, height: height * factor };
}

/**
 * Put text on the clipboard, reporting whether it landed.
 *
 * The Clipboard API needs a secure context, which a plain-HTTP gateway is not
 * — so failure is expected rather than exceptional here, and the caller shows
 * the text for manual copying instead. A silent no-op would be worse than
 * useless when the copy IS the feature.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

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
