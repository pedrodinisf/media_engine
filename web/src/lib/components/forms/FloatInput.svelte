<script lang="ts">
  /*
   * Locale-safe float input (B-004).
   *
   * Native <input type="number"> renders its value attribute using the OS
   * locale's decimal separator — on pt-PT a default of 0.2 displays as 0,2,
   * which then fails server-side JSON validation. This component keeps a
   * local text buffer so the rendered value uses a period regardless of
   * locale, accepts commas as decimal separators (European keyboards), and
   * defers the parent update during in-progress edits ("0." → "0.5") so
   * controlled-input round-trips don't eat trailing dots.
   */
  import { untrack } from 'svelte';
  import { isIntermediate, parseFloatInput } from './float_input';

  type Props = {
    value: number | null;
    nullable: boolean;
    onChange: (next: number | null) => void;
  };
  let { value, nullable, onChange }: Props = $props();

  // Text buffer the input renders. Seed from `value` on mount (via untrack
  // so the $state initializer doesn't subscribe to the prop), then sync
  // through the effect below. String() always renders with a period
  // regardless of locale; NaN renders as "NaN" which the parser then
  // rejects (treated like null).
  function serialize(v: number | null): string {
    if (v === null || !Number.isFinite(v)) return '';
    return String(v);
  }

  let text = $state(untrack(() => serialize(value)));

  $effect(() => {
    const parsed = parseFloatInput(text);
    // Re-sync only when the parent's value diverges from our local parse.
    // Avoids clobbering an in-progress "0." while the user is mid-type.
    // Guard with Number.isFinite on `value` — a parent passing NaN would
    // otherwise loop forever (NaN !== NaN, so the parsed/value comparison
    // never settles).
    const valueComparable = Number.isFinite(value) ? value : null;
    if (parsed !== valueComparable && !isIntermediate(text)) {
      text = serialize(value);
    }
  });

  function handleInput(e: Event): void {
    const t = e.target as HTMLInputElement;
    text = t.value;
    const parsed = parseFloatInput(text);
    if (parsed === null) {
      // Empty string commits null/0; in-progress strings (".", "-") wait.
      if (text.replace(',', '.').trim() === '') {
        onChange(nullable ? null : 0);
      }
      return;
    }
    onChange(parsed);
  }
</script>

<input
  type="text"
  inputmode="decimal"
  value={text}
  oninput={handleInput}
  class="w-full px-3 py-2 rounded text-sm font-mono"
  style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
/>
