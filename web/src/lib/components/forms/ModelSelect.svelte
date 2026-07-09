<script lang="ts">
  /**
   * Provider-aware model dropdown.
   *
   * Renders a model-id enum grouped into Local / Cloud `<optgroup>`s with a
   * cloud/local badge next to the current value — the direct answer to "am I
   * about to use a cloud API or a local model?". An off-list (custom) id set
   * via the YAML pane is preserved as a "(custom)" option so it isn't lost.
   *
   * Used by SchemaForm for any model-typed enum field, so it lands in both the
   * profile per-node editor and the /run panel.
   */
  import { classifyModelProvider, type ModelProvider } from './schema';

  type Props = {
    value: string | null;
    options: readonly (string | number)[];
    nullable?: boolean;
    onChange: (next: string | null) => void;
  };
  let { value, options, nullable = false, onChange }: Props = $props();

  const current = $derived(typeof value === 'string' ? value : '');
  const provider = $derived<ModelProvider>(
    current ? classifyModelProvider(current) : 'unknown',
  );

  const optionStrings = $derived(options.map(String));
  const isCustom = $derived(current !== '' && !optionStrings.includes(current));

  const groups = $derived.by(() => {
    const local: string[] = [];
    const cloud: string[] = [];
    const other: string[] = [];
    for (const s of optionStrings) {
      const p = classifyModelProvider(s);
      if (p === 'local') local.push(s);
      else if (p === 'cloud') cloud.push(s);
      else other.push(s);
    }
    return { local, cloud, other };
  });

  const badge = $derived(
    provider === 'cloud' ? '☁ cloud' : provider === 'local' ? '⌂ local' : '',
  );
  const badgeColor = $derived(
    provider === 'cloud'
      ? 'var(--accent-amber)'
      : provider === 'local'
        ? 'var(--accent-green)'
        : 'var(--text-muted)',
  );
</script>

<div class="flex items-center gap-2">
  <select
    value={current}
    onchange={(e) => onChange((e.target as HTMLSelectElement).value || null)}
    class="flex-1 px-3 py-2 rounded text-sm font-mono"
    style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
  >
    {#if nullable}
      <option value="">—</option>
    {/if}
    {#if isCustom}
      <option value={current}>{current} (custom)</option>
    {/if}
    {#if groups.local.length}
      <optgroup label="Local · on-device">
        {#each groups.local as m (m)}<option value={m}>{m}</option>{/each}
      </optgroup>
    {/if}
    {#if groups.cloud.length}
      <optgroup label="Cloud · needs API key">
        {#each groups.cloud as m (m)}<option value={m}>{m}</option>{/each}
      </optgroup>
    {/if}
    {#if groups.other.length}
      <optgroup label="Other">
        {#each groups.other as m (m)}<option value={m}>{m}</option>{/each}
      </optgroup>
    {/if}
  </select>
  {#if badge}
    <span
      class="text-xs font-semibold whitespace-nowrap"
      style="color: {badgeColor};"
      data-testid="model-provider-badge"
    >{badge}</span>
  {/if}
</div>
