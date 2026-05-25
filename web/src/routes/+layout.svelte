<script lang="ts">
  import '../app.css';
  import { base } from '$app/paths';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { token } from '$lib/stores/token';
  import Logo from '$lib/components/brand/Logo.svelte';

  type NavLink = { href: string; label: string };

  // Path values are stored *without* the SvelteKit ``paths.base`` ('/ui')
  // prefix; templates compose them as ``{base}{link.href}`` so the
  // rendered href is the full server-side URL. SvelteKit's link handler
  // does NOT auto-prepend the base for absolute-looking hrefs — `<a
  // href="/ingest">` is treated as truly absolute and navigates to
  // ``/ingest`` (which the FastAPI side 404s). Be explicit.
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
    const full = `${base}${href}`;
    if (href === '/') return pathname === base || pathname === `${base}/`;
    return pathname.startsWith(full);
  }

  // Auth gate: send users without a token to /setup. The setup route
  // itself is exempt so the user can paste their token without
  // ping-ponging. Running in onMount avoids SSR-prerender side effects.
  onMount(() => {
    const unsub = token.subscribe((tok) => {
      const path = $page.url.pathname;
      const onSetup = path.startsWith(`${base}/setup`);
      if (!tok && !onSetup) {
        void goto(`${base}/setup`);
      }
    });
    return unsub;
  });

  let isSetupRoute = $derived($page.url.pathname.startsWith(`${base}/setup`));

  // Pulled from /health (unauthenticated) so the banner always tracks
  // the running engine's `media_engine.__version__` instead of drifting
  // when the wheel ships ahead of the SPA. Falls back to a sentinel
  // when the engine is unreachable (e.g. dev preview, network blip) so
  // the header still renders.
  let engineVersion = $state('…');
  onMount(() => {
    // /health is unauthenticated + served at the FastAPI root, not under
    // the SvelteKit base path ('/ui') — fetch the bare path.
    void fetch('/health')
      .then((r) => (r.ok ? r.json() : null))
      .then((body) => {
        if (body && typeof body.version === 'string') engineVersion = body.version;
      })
      .catch(() => {
        engineVersion = '?';
      });
  });
</script>

<div class="min-h-screen flex flex-col">
  <header
    class="sticky top-0 z-50 h-12 px-5 flex items-center gap-4 text-text-inverse"
    style="background: var(--bg-header); border-bottom: 1px solid rgba(0,0,0,0.2);"
  >
    <a href={isSetupRoute ? `${base}/setup` : `${base}/`} class="flex items-center gap-2.5 whitespace-nowrap text-text-inverse hover:no-underline">
      <Logo size={22} class="opacity-90" />
      <span class="flex items-baseline gap-2">
        <span class="font-mono text-sm font-semibold tracking-wider">media_engine</span>
        <span class="text-xs opacity-60 font-mono">v{engineVersion}</span>
      </span>
    </a>

    {#if !isSetupRoute}
      <nav class="flex-1 flex items-center gap-1 ml-4">
        {#each links as link (link.href)}
          {@const active = isActive(link.href, $page.url.pathname)}
          <a
            href={`${base}${link.href}`}
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
    media_engine — local-first media-processing engine
  </footer>
</div>
