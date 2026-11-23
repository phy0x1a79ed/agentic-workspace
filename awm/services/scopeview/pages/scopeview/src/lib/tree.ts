/** The left tree and the lineage strip, both derived from the graph result on every read. */
import type { Graph, GraphNode, Stage } from './api.ts';

export const STAGE_LABEL: Record<Stage, string> = {
  table: 'Raw tables',
  question: 'Questions',
  figure: 'Figures',
  interpretation: 'Interpretations',
  point: 'Result points',
  prose: 'Prose',
};

export interface TreeItem {
  /** Unique in the tree; a node that shows under several parents has one key per place. */
  key: string;
  label: string;
  stage: Stage;
  /** Absent on a group row (stage, dataset, subject). */
  node?: GraphNode;
  /** A child shown under its parent: the same node as its own row in its stage group. */
  link?: boolean;
  /** Nodes below a group row. */
  count?: number;
  /** A group row that starts collapsed. */
  closed?: boolean;
  children: TreeItem[];
}

const byId = (g: Graph) => new Map(g.nodes.map((n) => [n.id, n]));

/**
 * Stage groups in stage order. Each node has one row in its stage group, and under it one link
 * row per direct child, so a node with several parents shows under each of them.
 */
export function buildTree(g: Graph, filter = ''): TreeItem[] {
  const ids = byId(g);
  const q = filter.trim().toLowerCase();
  const hit = (n: GraphNode) => !q || n.title.toLowerCase().includes(q) || n.id.toLowerCase().includes(q);
  const row = (n: GraphNode): TreeItem => ({
    key: n.id,
    label: n.title,
    stage: n.stage,
    node: n,
    children: n.used_by.flatMap((c) => {
      const child = ids.get(c);
      return child ? [{ key: `${n.id}>${c}`, label: child.title, stage: child.stage, node: child, link: true, children: [] }] : [];
    }),
  });
  const groups = g.stages.map((stage): TreeItem => {
    const nodes = g.nodes.filter((n) => n.stage === stage && hit(n));
    return {
      key: `stage:${stage}`,
      label: STAGE_LABEL[stage],
      stage,
      count: nodes.length,
      children: stage === 'table' ? tables(nodes, row, !q) : nodes.map(row),
    };
  });
  return q ? groups.filter((s) => s.count) : groups;
}

function tables(nodes: GraphNode[], row: (n: GraphNode) => TreeItem, foldProducts: boolean): TreeItem[] {
  const nest = new Map<string, Map<string, GraphNode[]>>();
  for (const n of nodes) {
    const subjects = nest.get(n.dataset ?? 'other') ?? new Map<string, GraphNode[]>();
    nest.set(n.dataset ?? 'other', subjects);
    subjects.set(n.subject ?? 'other', [...(subjects.get(n.subject ?? 'other') ?? []), n]);
  }
  const pooledFirst = (a: string, b: string) => Number(b === 'tables') - Number(a === 'tables') || a.localeCompare(b);
  return [...nest].map(([dataset, subjects]) => ({
    key: `dataset:${dataset}`,
    label: dataset,
    stage: 'table' as const,
    count: [...subjects.values()].reduce((sum, ns) => sum + ns.length, 0),
    children: [...subjects.keys()].sort(pooledFirst).map((subject) => ({
      key: `subject:${dataset}/${subject}`,
      label: subject,
      stage: 'table' as const,
      count: subjects.get(subject)!.length,
      closed: foldProducts && subject !== 'tables',
      children: subjects.get(subject)!.map(row),
    })),
  }));
}

/** The node and everything it was built from, by stage, raw tables first. */
export function lineage(g: Graph, id: string): { stage: Stage; nodes: GraphNode[] }[] {
  const ids = byId(g);
  const seen = new Set<string>();
  const walk = (i: string) => {
    const n = ids.get(i);
    if (!n || seen.has(i)) return;
    seen.add(i);
    n.from?.forEach(walk);
  };
  walk(id);
  return g.stages
    .map((stage) => ({ stage, nodes: g.nodes.filter((n) => n.stage === stage && seen.has(n.id)) }))
    .filter((s) => s.nodes.length);
}
