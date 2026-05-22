<script lang="ts">
  import { base } from '$app/paths';

  // Placeholder dashboard. Commits 41–49 turn the panel cards into live
  // navigation tiles with cache hit counts, job summaries, and a recent
  // activity tail. Commit 39 ships the shell only so the rest of the
  // phase has somewhere to land.

  type Panel = {
    title: string;
    description: string;
    href: string;
    cta: string;
  };

  const panels: readonly Panel[] = [
    {
      title: 'Ingest',
      description: 'Drop a file, paste a URL, or capture a livestream.',
      href: '/ingest',
      cta: 'New artifact',
    },
    {
      title: 'Run',
      description: 'Pick an op, fill the form, watch SSE events live.',
      href: '/run',
      cta: 'Run an op',
    },
    {
      title: 'Catalog',
      description: 'Browse 14 artifact kinds with per-kind preview affordances.',
      href: '/catalog',
      cta: 'Open catalog',
    },
    {
      title: 'Profiles',
      description: 'Visual DAG composer + YAML editor + bundled examples.',
      href: '/profiles',
      cta: 'Open workspace',
    },
    {
      title: 'Search',
      description: 'fulltext · semantic · hybrid over the cache.',
      href: '/search',
      cta: 'Search now',
    },
    {
      title: 'Cost',
      description: 'Spend ledger across ops, backends, namespaces.',
      href: '/cost',
      cta: 'See ledger',
    },
  ];
</script>

<section class="mb-6">
  <h1 class="text-2xl font-semibold mb-2" style="color: var(--text-primary);">Welcome.</h1>
  <p class="text-sm" style="color: var(--text-secondary); max-width: 60ch;">
    media_engine factors media-processing capabilities into one substrate so
    future applications are written as <em>profiles (data)</em>, not new programs.
    This is the local web UI — every panel below maps to a
    <code class="font-mono text-xs px-1.5 py-0.5 rounded" style="background: var(--bg-alt); border: 1px solid var(--border-soft);">med</code>
    CLI verb.
  </p>
</section>

<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
  {#each panels as panel (panel.href)}
    <a
      href={`${base}${panel.href}`}
      class="block p-4 rounded transition-shadow hover:no-underline"
      style="background: var(--bg-card); border: 1px solid var(--border-soft); color: var(--text-primary);"
    >
      <h2 class="text-base font-semibold mb-1">{panel.title}</h2>
      <p class="text-xs mb-3" style="color: var(--text-secondary); min-height: 2.5em;">
        {panel.description}
      </p>
      <span
        class="text-xs font-mono"
        style="color: var(--accent-green);"
      >
        → {panel.cta}
      </span>
    </a>
  {/each}
</div>
