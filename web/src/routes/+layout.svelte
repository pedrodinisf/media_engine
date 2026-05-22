<script lang="ts">
  import '../app.css';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { token } from '$lib/stores/token';

  type NavLink = { href: string; label: string };

  const links: readonly NavLink[] = [
    { href: '/ingest', label: 'Ingest' },
    { href: '/run', label: 'Run' },
    { href: '/jobs', label: 'Jobs' },
    { href: '/catalog', label: 'Catalog' },
    { href: '/search', label: 'Search' },
    { href: '/cost', label: 'Cost' },
    { href: '/profiles', label: 'Profiles' },
    { href: '/settings', label: 'Settings' },
  ];

  // Single-namespace per process (Phase 4 deployment.md contract).
  // Phase 6 reads it from the engine via /operations introspection or
  // a future /config endpoint; commit 39 ships a placeholder badge.
  const namespace = 'default';

  let { children } = $props();

  function isActive(href: string, pathname: string): boolean {
    if (href === '/') return pathname === '/' || pathname === '';
    return pathname.startsWith(href);
  }

  // Auth gate: send users without a token to /setup. The setup route
  // itself is exempt so the user can paste their token without
  // ping-ponging. Running in onMount avoids SSR-prerender side effects.
  onMount(() => {
    const unsub = token.subscribe((tok) => {
      const path = $page.url.pathname;
      const onSetup = path.startsWith('/setup');
      if (!tok && !onSetup) {
        void goto('/setup');
      }
    });
    return unsub;
  });

  let isSetupRoute = $derived($page.url.pathname.startsWith('/setup'));
</script>

<div class="min-h-screen flex flex-col">
  <header
    class="sticky top-0 z-50 h-12 px-5 flex items-center gap-4 text-text-inverse"
    style="background: var(--bg-header); border-bottom: 1px solid rgba(0,0,0,0.2);"
  >
    <a href={isSetupRoute ? '/setup' : '/'} class="flex items-baseline gap-2 whitespace-nowrap text-text-inverse hover:no-underline">
      <span class="font-mono text-sm font-semibold tracking-wider">media_engine</span>
      <span class="text-xs opacity-60 font-mono">v0.6.0</span>
    </a>

    {#if !isSetupRoute}
      <nav class="flex-1 flex items-center gap-1 ml-4">
        {#each links as link (link.href)}
          {@const active = isActive(link.href, $page.url.pathname)}
          <a
            href={link.href}
            class="px-3 py-1 text-sm rounded font-medium transition-colors"
            class:bg-accent-green-soft={active}
            style={active ? 'color: var(--text-inverse); background: rgba(255,255,255,0.12);' : 'color: rgba(245,245,240,0.78);'}
            aria-current={active ? 'page' : undefined}
          >
            {link.label}
          </a>
        {/each}
      </nav>

      <span
        class="text-xs font-mono px-2 py-1 rounded"
        style="color: var(--text-inverse); background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12);"
        title="Engine namespace (set via MEDIA_ENGINE_NAMESPACE; one engine process per namespace)"
      >
        ns: {namespace}
      </span>
    {:else}
      <span class="flex-1 text-xs font-mono opacity-60">setup</span>
    {/if}
  </header>

  <main class="flex-1 px-6 py-6" style="max-width: var(--max-w); width: 100%; margin: 0 auto;">
    {@render children()}
  </main>

  <footer
    class="px-6 py-3 text-xs"
    style="color: var(--text-muted); border-top: var(--rule);"
  >
    media_engine — local-first media-processing engine ·
    <a href="https://github.com/anthropics/claude-code/issues" target="_blank" rel="noopener">Feedback</a>
  </footer>
</div>
