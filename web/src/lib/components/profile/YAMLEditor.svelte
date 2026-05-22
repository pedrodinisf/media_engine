<script lang="ts">
  /**
   * CodeMirror 6 editor for profile YAML.
   *
   * Composes the YAML language pack with op-name autocomplete fed by
   * `GET /operations`. Two-way bound via `value` + `onChange`. We
   * deliberately don't `bind:` the raw view — Svelte 5 + CodeMirror's
   * imperative event model don't play well — instead we own the
   * EditorView and forward `dispatch` updates upward.
   */
  import { onDestroy, onMount } from 'svelte';
  import { EditorState, type Extension } from '@codemirror/state';
  import { EditorView, keymap, lineNumbers, highlightActiveLine } from '@codemirror/view';
  import { defaultKeymap, history, historyKeymap } from '@codemirror/commands';
  import { yaml as yamlLang } from '@codemirror/lang-yaml';
  import {
    autocompletion,
    type CompletionContext,
    type CompletionResult,
  } from '@codemirror/autocomplete';
  import { bracketMatching, foldGutter, indentOnInput } from '@codemirror/language';

  type Props = {
    value: string;
    onChange: (next: string) => void;
    /** Op-name completion list — from `GET /operations`. */
    opNames?: readonly string[];
    /** 1-based line number to highlight (e.g. a YAML parse error). */
    errorLine?: number | null;
    /** Min height of the editor surface; defaults to a generous workspace pane. */
    minHeight?: string;
  };

  let {
    value,
    onChange,
    opNames = [],
    errorLine = null,
    minHeight = '50vh',
  }: Props = $props();

  let container: HTMLDivElement | undefined = $state();
  let view: EditorView | null = null;

  function opCompletion(ctx: CompletionContext): CompletionResult | null {
    // Trigger only when the cursor follows `op:` or `op: <partial>`.
    const before = ctx.state.doc.sliceString(0, ctx.pos);
    const match = before.match(/op:\s*([a-zA-Z0-9_.]*)$/);
    if (!match) return null;
    const partial = match[1] ?? '';
    return {
      from: ctx.pos - partial.length,
      options: opNames.map((name) => ({ label: name, type: 'function' })),
      validFor: /^[a-zA-Z0-9_.]*$/,
    };
  }

  function extensions(): Extension[] {
    return [
      lineNumbers(),
      foldGutter(),
      highlightActiveLine(),
      history(),
      bracketMatching(),
      indentOnInput(),
      keymap.of([...defaultKeymap, ...historyKeymap]),
      yamlLang(),
      autocompletion({ override: [opCompletion] }),
      EditorView.theme({
        '&': {
          fontSize: '13px',
          fontFamily: 'var(--font-mono)',
          height: '100%',
          minHeight,
          backgroundColor: 'var(--bg-page)',
          color: 'var(--text-primary)',
        },
        '.cm-scroller': { overflow: 'auto' },
        '.cm-gutters': {
          backgroundColor: 'var(--bg-card)',
          color: 'var(--text-muted)',
          border: 'none',
          borderRight: '1px solid var(--border-soft)',
        },
        '.cm-activeLineGutter, .cm-activeLine': {
          backgroundColor: 'var(--accent-green-soft)',
        },
        '.cm-line.error-line': {
          backgroundColor: 'rgba(220, 38, 38, 0.08)',
        },
      }),
      EditorView.updateListener.of((u) => {
        if (u.docChanged) {
          onChange(u.state.doc.toString());
        }
      }),
    ];
  }

  onMount(() => {
    if (!container) return;
    view = new EditorView({
      state: EditorState.create({ doc: value, extensions: extensions() }),
      parent: container,
    });
  });

  onDestroy(() => {
    view?.destroy();
    view = null;
  });

  // Push external `value` changes back into the view. Guarded so the
  // upstream `onChange` callback doesn't ping-pong (only update when
  // the current doc differs).
  $effect(() => {
    const _v = value;
    if (!view) return;
    const current = view.state.doc.toString();
    if (current !== _v) {
      view.dispatch({
        changes: { from: 0, to: current.length, insert: _v },
      });
    }
  });

  // Decorate the error line. CodeMirror's preferred path is a
  // line-decoration field; for v1 a simple class on the affected line
  // suffices and stays small. Cleared by passing errorLine=null.
  $effect(() => {
    const _line = errorLine;
    if (!view) return;
    const all = view.dom.querySelectorAll('.cm-line.error-line');
    all.forEach((el) => el.classList.remove('error-line'));
    if (_line && _line > 0) {
      // Map 1-based line number to a DOM element.  CM doesn't expose
      // this directly via DOM, but the order of `.cm-line` matches
      // the document line order.
      const lines = view.dom.querySelectorAll('.cm-line');
      const target = lines[_line - 1] as HTMLElement | undefined;
      target?.classList.add('error-line');
    }
  });
</script>

<div
  bind:this={container}
  class="rounded overflow-hidden"
  style="border: 1px solid var(--border-soft); min-height: {minHeight};"
></div>
