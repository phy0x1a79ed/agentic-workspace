<script lang="ts">
  /**
   * The diagram tree, recursive on itself.
   *
   * Checkouts hang off the diagram they came from, so an agent's in-flight
   * work is visible at a glance rather than being something you have to go
   * looking for — and each one is clickable, so a person can open the working
   * copy and review it *before* it lands.
   */
  import { ago, size, type TreeNode } from './api';
  import DiagramTree from './DiagramTree.svelte';

  interface Props {
    nodes: TreeNode[];
    selected: string | null;
    depth?: number;
    onselect: (save: string) => void;
    onopen: (save: string) => void;
    onopencheckout: (handle: string) => void;
  }

  let { nodes, selected, depth = 0, onselect, onopen, onopencheckout }: Props = $props();
</script>

<ul style:--depth={depth}>
  {#each nodes as node (node.path)}
    <li>
      {#if node.diagram}
        {@const d = node.diagram}
        <div class="row" class:selected={selected === d.save}>
          <button class="name" onclick={() => onselect(d.save)} ondblclick={() => onopen(d.save)}>
            <span class="leaf">{node.name}</span>
            <span class="meta">
              {d.pages.length} page{d.pages.length === 1 ? '' : 's'}
              · {size(d.bytes)}
              · {ago(d.modified)}
              {#if d.author}· {d.author}{/if}
            </span>
          </button>
          <div class="badges">
            {#if d.editors > 0}
              <span class="badge live" title="{d.editors} editor tab(s) open">
                ● {d.editors}
              </span>
            {/if}
            <button class="open" onclick={() => onopen(d.save)}>open</button>
          </div>
        </div>
        {#each d.checkouts as c (c.id)}
          <div class="checkout" class:conflicted={c.state === 'conflicted'}>
            <button onclick={() => onopencheckout(c.id)}>
              <span class="handle">{c.id}</span>
              <span class="meta">
                {c.author} · {ago(c.updated)}
                {#if c.state === 'conflicted'}
                  · {c.conflicts} conflict{c.conflicts === 1 ? '' : 's'}
                {/if}
              </span>
            </button>
          </div>
        {/each}
      {:else}
        <div class="folder">{node.name}/</div>
        <DiagramTree
          nodes={node.children}
          {selected}
          depth={depth + 1}
          {onselect}
          {onopen}
          {onopencheckout}
        />
      {/if}
    </li>
  {/each}
</ul>

<style>
  ul {
    list-style: none;
    margin: 0;
    padding: 0 0 0 calc(var(--depth, 0) > 0 ? 0.9rem : 0);
  }
  li { margin: 0; }

  .folder {
    font-size: 0.8rem;
    letter-spacing: 0.04em;
    color: var(--text3, #888);
    padding: 0.35rem 0 0.15rem;
  }

  .row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    border-radius: 4px;
    padding: 0.1rem 0.35rem;
  }
  .row:hover { background: var(--surface2, #1c1c1c); }
  .row.selected { background: var(--surface2, #222); outline: 1px solid var(--border, #333); }

  button {
    background: none;
    border: none;
    color: inherit;
    font: inherit;
    text-align: left;
    cursor: pointer;
    padding: 0;
  }

  .name {
    flex: 1 1 auto;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
    padding: 0.3rem 0;
    min-width: 0;
  }
  .leaf {
    color: var(--text, #ddd);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .meta { font-size: 0.75rem; color: var(--text3, #888); }

  .badges { display: flex; align-items: center; gap: 0.4rem; flex: 0 0 auto; }
  .badge {
    font-size: 0.7rem;
    padding: 0.05rem 0.35rem;
    border-radius: 999px;
    border: 1px solid var(--border, #333);
  }
  .badge.live { color: #7bd88f; border-color: #2f5d3a; }

  .open {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border: 1px solid var(--border, #333);
    border-radius: 3px;
    color: var(--text2, #bbb);
  }
  .open:hover { background: var(--surface3, #2a2a2a); color: var(--text, #ddd); }

  .checkout {
    margin-left: 0.9rem;
    border-left: 2px solid var(--border, #333);
    padding-left: 0.5rem;
  }
  .checkout button {
    display: flex;
    flex-direction: column;
    gap: 0.05rem;
    padding: 0.15rem 0;
    width: 100%;
  }
  .checkout .handle { font-family: var(--mono, monospace); font-size: 0.78rem; color: var(--text2, #bbb); }
  .checkout.conflicted { border-left-color: #a8552f; }
  .checkout.conflicted .meta { color: #d68b5f; }
</style>
