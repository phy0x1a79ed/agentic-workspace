<script lang="ts">
  import type { Component } from 'svelte';
  import Button from './Button.svelte';
  import Card from './Card.svelte';
  import CollapsibleSection from './CollapsibleSection.svelte';
  import Input from './Input.svelte';
  import PanelLabel from './PanelLabel.svelte';
  import Pill from './Pill.svelte';
  import Tag from './Tag.svelte';
  import Tooltip from './Tooltip.svelte';

  // Glob sibling primitives so the index stays auto-discovered. Each card uses
  // a hand-authored renderer below — primitives have heterogeneous prop
  // shapes (some need bound state, Tooltip needs a trigger snippet), so a
  // single default-props map can't render them all. The glob is ground truth
  // for "what should be in the gallery"; missing renderers surface as a
  // banner so adding a primitive without a renderer is loud.
  const found = import.meta.glob<{ default: Component }>('./*.svelte', {
    eager: true,
  });
  const foundNames = Object.keys(found)
    .map((p) => p.split('/').pop()!.replace(/\.svelte$/, ''))
    .filter((n) => n !== 'Gallery')
    .sort();

  const renderers = [
    'Button',
    'Card',
    'CollapsibleSection',
    'Input',
    'PanelLabel',
    'Pill',
    'Tag',
    'Tooltip',
  ];
  const missing = foundNames.filter((n) => !renderers.includes(n));

  let inputValue = $state('');
  let sectionOpen = $state(true);
</script>

<div class="shell">
  <nav class="toc">
    <PanelLabel>primitives</PanelLabel>
    <span class="count mono">{foundNames.length} components</span>
    <ul class="jumps mono">
      {#each renderers as r}
        <li><a href="#p-{r}">{r}</a></li>
      {/each}
    </ul>
  </nav>

  <main class="page">
    {#if missing.length}
      <aside class="missing mono">
        missing renderer for: {missing.join(', ')} — add a card in
        gallery/Gallery.svelte.
      </aside>
    {/if}

    <section class="list">
    <article class="card" id="p-Button">
      <h2 class="name mono">Button</h2>
      <div class="stage row">
        <Button kind="primary">primary</Button>
        <Button kind="ghost">ghost</Button>
        <Button kind="danger">danger</Button>
        <Button kind="ghost" disabled>disabled</Button>
      </div>
    </article>

    <article class="card" id="p-Card">
      <h2 class="name mono">Card</h2>
      <div class="stage col">
        <Card rail="manager">
          <span class="card-body mono">rail: manager</span>
        </Card>
        <Card rail="peer">
          <span class="card-body mono">rail: peer</span>
        </Card>
        <Card rail="warn" flash>
          <span class="card-body mono">rail: warn (flash)</span>
        </Card>
      </div>
    </article>

    <article class="card" id="p-CollapsibleSection">
      <h2 class="name mono">CollapsibleSection</h2>
      <div class="stage col">
        <CollapsibleSection
          label="section label"
          rail="plain"
          bind:open={sectionOpen}
        >
          <p class="body mono">collapsed/expanded body content.</p>
        </CollapsibleSection>
      </div>
    </article>

    <article class="card" id="p-Input">
      <h2 class="name mono">Input</h2>
      <div class="stage">
        <Input bind:value={inputValue} placeholder="type here…" />
      </div>
    </article>

    <article class="card" id="p-PanelLabel">
      <h2 class="name mono">PanelLabel</h2>
      <div class="stage row wrap">
        <PanelLabel>dim</PanelLabel>
        <PanelLabel tone="atomizer">atomizer</PanelLabel>
        <PanelLabel tone="mgr">mgr</PanelLabel>
        <PanelLabel tone="peer">peer</PanelLabel>
      </div>
    </article>

    <article class="card" id="p-Pill">
      <h2 class="name mono">Pill</h2>
      <div class="stage row wrap">
        <Pill>neutral</Pill>
        <Pill tone="atomizer">atomizer</Pill>
        <Pill tone="danger">danger</Pill>
        <Pill disabled>disabled</Pill>
      </div>
    </article>

    <article class="card" id="p-Tag">
      <h2 class="name mono">Tag</h2>
      <div class="stage row wrap">
        <Tag>neutral</Tag>
        <Tag tone="ok">ok</Tag>
        <Tag tone="warn">warn</Tag>
        <Tag tone="danger">danger</Tag>
        <Tag tone="atomizer">atomizer</Tag>
        <Tag tone="mgr">mgr</Tag>
      </div>
    </article>

    <article class="card" id="p-Tooltip">
      <h2 class="name mono">Tooltip</h2>
      <div class="stage">
        <Tooltip content="hover-activated tooltip">
          {#snippet trigger({ props })}
            <button class="ttip-target mono" {...props}>hover me</button>
          {/snippet}
        </Tooltip>
      </div>
    </article>
    </section>
  </main>
</div>
