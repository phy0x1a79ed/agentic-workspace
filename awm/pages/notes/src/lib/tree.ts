// Build a path tree from the flat note list. A note's `path` is a slash-joined
// title (e.g. "research/avarice/log"); every segment but the last is a folder,
// the last is the note's leaf label. Empty paths become an "(untitled)" leaf so
// a brand-new note still shows up. Folders sort before leaves, both A→Z.

import type { NoteMeta } from './api';

export interface TreeLeaf {
  kind: 'leaf';
  note: NoteMeta;
  label: string;
}

export interface TreeFolder {
  kind: 'folder';
  name: string;
  path: string; // full folder path, for stable collapse keys
  children: TreeNode[];
}

export type TreeNode = TreeFolder | TreeLeaf;

function splitPath(path: string): string[] {
  return (path || '')
    .split('/')
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
}

export function buildTree(notes: NoteMeta[]): TreeNode[] {
  const root: TreeFolder = { kind: 'folder', name: '', path: '', children: [] };

  for (const note of notes) {
    const segs = splitPath(note.path);
    const label = segs.length ? segs[segs.length - 1] : '(untitled)';
    const folders = segs.slice(0, -1);

    let cursor = root;
    let acc = '';
    for (const name of folders) {
      acc = acc ? `${acc}/${name}` : name;
      let next = cursor.children.find(
        (c): c is TreeFolder => c.kind === 'folder' && c.name === name,
      );
      if (!next) {
        next = { kind: 'folder', name, path: acc, children: [] };
        cursor.children.push(next);
      }
      cursor = next;
    }
    cursor.children.push({ kind: 'leaf', note, label });
  }

  sortNodes(root.children);
  return root.children;
}

function sortNodes(nodes: TreeNode[]): void {
  nodes.sort((a, b) => {
    if (a.kind !== b.kind) return a.kind === 'folder' ? -1 : 1;
    const an = a.kind === 'folder' ? a.name : a.label;
    const bn = b.kind === 'folder' ? b.name : b.label;
    return an.localeCompare(bn);
  });
  for (const n of nodes) if (n.kind === 'folder') sortNodes(n.children);
}
