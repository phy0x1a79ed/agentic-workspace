<script lang="ts">
  // Recursive path-tree renderer. Folders collapse; leaves open on click and
  // carry a trash affordance. Kept presentational — all state lives in App.
  import Self from './NoteTree.svelte';
  import type { TreeNode } from './tree';

  interface Props {
    nodes: TreeNode[];
    currentId: string | null;
    collapsed: Set<string>;
    depth?: number;
    onOpen: (id: string) => void;
    onTrash: (id: string) => void;
    onToggle: (path: string) => void;
  }
  let { nodes, currentId, collapsed, depth = 0, onOpen, onTrash, onToggle }: Props = $props();
</script>

<ul class="tree" class:root={depth === 0}>
  {#each nodes as node (node.kind === 'folder' ? 'f:' + node.path : 'l:' + node.note.id)}
    {#if node.kind === 'folder'}
      <li class="folder">
        <button
          class="row folder-row"
          style="--depth: {depth}"
          onclick={() => onToggle(node.path)}
          aria-expanded={!collapsed.has(node.path)}
        >
          <span class="twist" class:closed={collapsed.has(node.path)}>▸</span>
          <span class="name">{node.name}</span>
        </button>
        {#if !collapsed.has(node.path)}
          <Self
            nodes={node.children}
            {currentId}
            {collapsed}
            depth={depth + 1}
            {onOpen}
            {onTrash}
            {onToggle}
          />
        {/if}
      </li>
    {:else}
      <li class="leaf">
        <div class="row leaf-row" class:active={node.note.id === currentId} style="--depth: {depth}">
          <button class="name leaf-name" onclick={() => onOpen(node.note.id)} title={node.note.path || '(untitled)'}>
            <span class="dot" aria-hidden="true"></span>{node.label}
          </button>
          <button
            class="trash-btn"
            title="Move to trash"
            aria-label="Move {node.label} to trash"
            onclick={() => onTrash(node.note.id)}
          >×</button>
        </div>
      </li>
    {/if}
  {/each}
</ul>

<style>
  .tree { list-style: none; margin: 0; padding: 0; }
  .row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
    padding: 0.2rem 0.5rem 0.2rem calc(0.5rem + var(--depth, 0) * 0.85rem);
    border: 0;
    background: none;
    color: var(--nt-text2);
    font: inherit;
    text-align: left;
    cursor: pointer;
    border-radius: 5px;
  }
  .folder-row:hover, .leaf-row:hover { background: var(--nt-hover); }
  .twist {
    font-size: 0.65rem;
    color: var(--nt-faint);
    transition: transform 0.12s ease;
    display: inline-block;
  }
  .twist.closed { transform: rotate(-90deg); }
  .folder-row .name {
    font-family: var(--nt-sans);
    font-size: 0.8rem;
    letter-spacing: 0.02em;
    color: var(--nt-text);
    font-weight: 550;
  }
  .leaf-row { justify-content: space-between; padding-right: 0.3rem; }
  .leaf-name {
    flex: 1 1 auto;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    min-width: 0;
    border: 0;
    background: none;
    color: var(--nt-text2);
    font-family: var(--nt-mono);
    font-size: 0.8rem;
    cursor: pointer;
    padding: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .dot {
    flex: 0 0 auto;
    width: 5px; height: 5px;
    border-radius: 50%;
    background: var(--nt-faint);
  }
  .leaf-row.active { background: var(--nt-active-bg); }
  .leaf-row.active .leaf-name { color: var(--nt-accent); }
  .leaf-row.active .dot { background: var(--nt-accent); }
  .trash-btn {
    flex: 0 0 auto;
    border: 0;
    background: none;
    color: transparent;
    font-size: 1rem;
    line-height: 1;
    cursor: pointer;
    padding: 0 0.25rem;
    border-radius: 4px;
  }
  .leaf-row:hover .trash-btn { color: var(--nt-faint); }
  .trash-btn:hover { color: var(--nt-danger) !important; background: var(--nt-hover); }
</style>
