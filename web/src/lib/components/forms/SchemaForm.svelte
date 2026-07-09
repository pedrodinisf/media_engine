<script lang="ts">
  /**
   * Schema-driven form renderer.
   *
   * Walks the JSON Schema returned by `GET /operations/{name}` and emits
   * Tailwind-styled Svelte widgets per type. Honors readOnly (the
   * auto-derived *_sha fields from commit c5e6f5e). Helpers + types live
   * in $lib/components/forms/schema.ts so tests + sibling components
   * can import without touching the Svelte module/instance boundary.
   */
  import {
    unwrapNullable,
    isMultilineField,
    isModelField,
    type FieldValue,
    type ParamsSchema,
    type ParamsValue,
  } from './schema';
  import FloatInput from './FloatInput.svelte';
  import ModelSelect from './ModelSelect.svelte';

  type Props = {
    schema: ParamsSchema;
    value: ParamsValue;
    onChange: (next: ParamsValue) => void;
  };
  let { schema, value, onChange }: Props = $props();

  const fields = $derived(
    Object.entries(schema.properties ?? {})
      .map(([name, raw]) => {
        const { node, nullable } = unwrapNullable(raw);
        return { name, node, nullable, readOnly: !!node.readOnly };
      })
      .filter((f) => !f.readOnly),
  );

  function update(name: string, next: FieldValue): void {
    onChange({ ...value, [name]: next });
  }
</script>

<div class="grid grid-cols-1 gap-3">
  {#each fields as field (field.name)}
    {@const v = value[field.name]}
    <label class="block">
      <span class="block text-xs font-semibold mb-1" style="color: var(--text-secondary);">
        {field.node.title ?? field.name}
        {#if (schema.required ?? []).includes(field.name)}
          <span style="color: var(--accent-red);">*</span>
        {/if}
        {#if field.nullable}
          <span class="font-normal" style="color: var(--text-muted);">(optional)</span>
        {/if}
      </span>
      {#if field.node.description}
        <span class="block text-xs mb-1.5" style="color: var(--text-muted);">{field.node.description}</span>
      {/if}

      {#if field.node.enum && isModelField(field.name)}
        <ModelSelect
          value={typeof v === 'string' ? v : null}
          options={field.node.enum}
          nullable={field.nullable}
          onChange={(next) => update(field.name, next)}
        />
      {:else if field.node.enum}
        <select
          value={v ?? ''}
          onchange={(e) => update(field.name, (e.target as HTMLSelectElement).value)}
          class="w-full px-3 py-2 rounded text-sm font-mono"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
        >
          {#if field.nullable}
            <option value="">—</option>
          {/if}
          {#each field.node.enum as choice (choice)}
            <option value={String(choice)}>{choice}</option>
          {/each}
        </select>
      {:else if field.node.type === 'boolean'}
        <input
          type="checkbox"
          checked={v === true}
          onchange={(e) => update(field.name, (e.target as HTMLInputElement).checked)}
        />
      {:else if field.node.type === 'integer'}
        <input
          type="number"
          step="1"
          value={v ?? ''}
          oninput={(e) => {
            const t = e.target as HTMLInputElement;
            if (t.value === '') {
              update(field.name, field.nullable ? null : 0);
            } else {
              update(field.name, Number(t.value));
            }
          }}
          class="w-full px-3 py-2 rounded text-sm font-mono"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
        />
      {:else if field.node.type === 'number'}
        <FloatInput
          value={typeof v === 'number' ? v : (v === null ? null : 0)}
          nullable={field.nullable}
          onChange={(next) => update(field.name, next)}
        />
      {:else if isMultilineField(field.name)}
        <textarea
          value={typeof v === 'string' ? v : ''}
          oninput={(e) => update(field.name, (e.target as HTMLTextAreaElement).value)}
          rows="4"
          class="w-full px-3 py-2 rounded text-sm font-mono"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
        ></textarea>
      {:else}
        <input
          type="text"
          value={typeof v === 'string' ? v : ''}
          oninput={(e) => update(field.name, (e.target as HTMLInputElement).value)}
          class="w-full px-3 py-2 rounded text-sm font-mono"
          style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
          placeholder={field.node.format === 'path' ? '/path/to/file' : ''}
        />
      {/if}
    </label>
  {/each}

  {#if fields.length === 0}
    <p class="text-xs italic" style="color: var(--text-muted);">
      This op takes no settable params.
    </p>
  {/if}
</div>
