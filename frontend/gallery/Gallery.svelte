<script lang="ts">
  import type { Component } from 'svelte';
  import Button from '../src/lib/components/primitives/Button.svelte';
  import Card from '../src/lib/components/primitives/Card.svelte';
  import CollapsibleSection from '../src/lib/components/primitives/CollapsibleSection.svelte';
  import Input from '../src/lib/components/primitives/Input.svelte';
  import PanelLabel from '../src/lib/components/primitives/PanelLabel.svelte';
  import Pill from '../src/lib/components/primitives/Pill.svelte';
  import Tag from '../src/lib/components/primitives/Tag.svelte';
  import Tooltip from '../src/lib/components/primitives/Tooltip.svelte';

  // Glob the primitives dir so the index stays auto-discovered. Each card uses
  // a hand-authored renderer below — primitives have heterogeneous prop
  // shapes (some need bound state, Tooltip needs a trigger snippet), so a
  // single default-props map can't render them all. The glob is the
  // ground truth for "what should be in the gallery"; missing renderers
  // surface as a banner so adding a primitive without a renderer is loud.
  const found = import.meta.glob<{ default: Component }>(
    '../src/lib/components/primitives/*.svelte',
    { eager: true },
  );
  const foundNames = Object.keys(found)
    .map((p) => p.split('/').pop()!.replace(/\.svelte$/, ''))
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

<main class="page">
  <header class="hdr">
    <PanelLabel>primitives</PanelLabel>
    <span class="count mono">{foundNames.length} components</span>
  </header>

  {#if missing.length}
    <aside class="missing mono">
      missing renderer for: {missing.join(', ')} — add a card in
      gallery/Gallery.svelte.
    </aside>
  {/if}

  <section class="grid">
    <article class="card">
      <h2 class="name mono">Button</h2>
      <div class="stage row">
        <Button kind="primary">primary</Button>
        <Button kind="ghost">ghost</Button>
        <Button kind="danger">danger</Button>
        <Button kind="ghost" disabled>disabled</Button>
      </div>
    </article>

    <article class="card">
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

    <article class="card">
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

    <article class="card">
      <h2 class="name mono">Input</h2>
      <div class="stage">
        <Input bind:value={inputValue} placeholder="type here…" />
      </div>
    </article>

    <article class="card">
      <h2 class="name mono">PanelLabel</h2>
      <div class="stage row wrap">
        <PanelLabel>dim</PanelLabel>
        <PanelLabel tone="atomizer">atomizer</PanelLabel>
        <PanelLabel tone="mgr">mgr</PanelLabel>
        <PanelLabel tone="peer">peer</PanelLabel>
      </div>
    </article>

    <article class="card">
      <h2 class="name mono">Pill</h2>
      <div class="stage row wrap">
        <Pill>neutral</Pill>
        <Pill tone="atomizer">atomizer</Pill>
        <Pill tone="danger">danger</Pill>
        <Pill disabled>disabled</Pill>
      </div>
    </article>

    <article class="card">
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

    <article class="card">
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
