<script lang="ts">
  /**
   * Profile workspace — visual DAG composer (left) + YAML editor
   * (right), live-validated against `POST /profiles/validate`.
   *
   * YAML is the canonical source of truth. The composer renders from
   * the parsed graph view; clicking a palette op appends a new node
   * to the YAML AST (preserving comments + key order via the `yaml`
   * lib's `Document` model). The editor + composer both write through
   * the same `yaml = $state(...)` so they stay in sync.
   */
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { stringify as yamlStringify } from 'yaml';
  import { ApiError, api } from '$lib/api/client';
  import { deleteProfile, getProfile, saveProfile, validateProfile } from '$lib/profile/api';
  import {
    addNode,
    loadDocument,
    mutateNode,
    parseProfileText,
    serializeDocument,
  } from '$lib/profile/parse';
  import type { CompiledNode, ValidateProfileResponse } from '$lib/profile/types';
  import ProfileComposer from '$lib/components/profile/ProfileComposer.svelte';
  import YAMLEditor from '$lib/components/profile/YAMLEditor.svelte';
  import SourcesPicker from '$lib/components/profile/SourcesPicker.svelte';

  type OperationSummary = {
    name: string;
    version: string;
    input_kinds: string[];
    output_kinds: string[];
    default_backend: string | null;
    variadic_inputs: boolean;
  };

  let yamlText = $state('');
  let originalYaml = $state('');
  let sourcePath = $state<string | null>(null);
  // Bundled-vs-user comes from the server now (GET /profiles ?
  // ProfileSummary.source). Set during the initial load; falls back
  // to "user" so the delete affordance is enabled when discovery
  // hasn't returned yet — the server's DELETE handler is the
  // authoritative gate.
  let profileSource = $state<'bundled' | 'user' | null>(null);

  let validation = $state<ValidateProfileResponse | null>(null);
  let validating = $state(false);

  let ops = $state<OperationSummary[]>([]);

  let selectedNodeId = $state<string | null>(null);

  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let loadError = $state<string | null>(null);
  let deleting = $state(false);
  let running = $state(false);
  let runError = $state<string | null>(null);
  let showSourcesPicker = $state(false);

  /** Debounced view of the YAML text that drives heavy derived
   *  consumers (composer dagre layout, validate fetch). Keeps the
   *  editor responsive on every keystroke while the canvas + footer
   *  validation only relayouts/refetches when typing pauses. */
  let yamlForLayout = $state('');

  const name = $derived($page.params.name ?? '');
  /** Parsed view for the per-node editor + composer composition.
   *  Reads the debounced text so re-renders don't fire per keystroke
   *  — the editor itself is fed `yamlText` directly. */
  const parsed = $derived(parseProfileText(yamlForLayout));
  const isPipeline = $derived(parsed.kind === 'pipeline');
  const isBundled = $derived(profileSource === 'bundled');
  const isDirty = $derived(yamlText !== originalYaml);

  const invalidNodeIds = $derived.by(() => {
    // A node is "invalid" when validate's compiled_nodes doesn't
    // include it. Simple, conservative — surfaces typos in op name
    // or missing required fields per-node.
    if (!validation) return new Set<string>();
    if (validation.ok) return new Set<string>();
    const compiled = new Set(validation.compiled_nodes.map((c: CompiledNode) => c.id));
    return new Set(parsed.nodes.filter((n) => !compiled.has(n.id)).map((n) => n.id));
  });

  const opPalette = $derived(ops.map((o) => o.name));

  async function loadProfileBody(): Promise<void> {
    loadError = null;
    try {
      const body = await getProfile(name);
      sourcePath = body._source_path;
      // Server doesn't echo `source` on GET /profiles/{name}; reuse
      // the listing path to figure it out. Cheap (a single REST
      // round-trip alongside the body), reliable (the server tells
      // us, not a path heuristic).
      const cleaned: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(body)) {
        if (k === '_source_path') continue;
        if (v === null || v === undefined) continue;
        cleaned[k] = v;
      }
      const text = yamlStringify(cleaned);
      yamlText = text;
      originalYaml = text;
      yamlForLayout = text;
    } catch (e) {
      loadError = e instanceof ApiError ? e.detail : String(e);
    }
  }

  async function loadOps(): Promise<void> {
    try {
      ops = await api.get<OperationSummary[]>('/operations');
    } catch {
      // Non-fatal — composer just shows an empty palette.
    }
  }

  async function loadSourceMarker(): Promise<void> {
    try {
      const list = await api.get<Array<{ name: string; source: 'bundled' | 'user' }>>(
        '/profiles',
      );
      const me = list.find((p) => p.name === name);
      profileSource = me ? me.source : 'user';
    } catch {
      // Fall back to "user" so the delete button shows; the server
      // refuses bundled deletes anyway.
      profileSource = 'user';
    }
  }

  onMount(async () => {
    await Promise.all([loadOps(), loadProfileBody(), loadSourceMarker()]);
  });

  // Debounce the heavy `parsed` derivation. The editor still updates
  // `yamlText` every keystroke (CodeMirror needs that), but the
  // composer + per-node editor + validate fetch all derive from
  // `yamlForLayout` which we step forward 150 ms after the user stops
  // typing. dagre layout for a 20-node pipeline goes from per-
  // keystroke jank to a single repaint per pause.
  $effect(() => {
    const _v = yamlText;
    if (_v === yamlForLayout) return;
    const timer = setTimeout(() => {
      yamlForLayout = _v;
    }, 150);
    return () => clearTimeout(timer);
  });

  // Live validation — debounced 500 ms (plan §5 commit 47). Reads
  // the already-debounced `yamlForLayout` so the validate call fires
  // 500 + 150 ms after the last keystroke; the cancelled-flag pattern
  // matches commit 42's run-panel + commit 46's search input, so late
  // responses never write into dead state.
  $effect(() => {
    const _yaml = yamlForLayout;
    if (!_yaml.trim()) {
      validation = null;
      return;
    }
    let cancelled = false;
    const timer = setTimeout(async () => {
      validating = true;
      try {
        const result = await validateProfile(_yaml);
        if (!cancelled) validation = result;
      } catch (e) {
        if (!cancelled) {
          validation = {
            ok: false,
            compiled_nodes: [],
            error_class: 'NetworkError',
            message: e instanceof ApiError ? e.detail : String(e),
            line: null,
          };
        }
      } finally {
        if (!cancelled) validating = false;
      }
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  });

  function appendNodeFromPalette(opName: string): void {
    const existingIds = new Set(parsed.nodes.map((n) => n.id));
    const stem = opName.split('.').pop() ?? 'op';
    let id = stem;
    let i = 2;
    while (existingIds.has(id)) {
      id = `${stem}-${i++}`;
    }
    // Round-trip through Document so the rest of the file's comments
    // + key order are preserved. If the user typed something the
    // parser doesn't understand, we fall back to the plain-text
    // append — bad YAML in, bad YAML out is fine for v1.
    const doc = loadDocument(yamlText);
    addNode(doc, {
      id,
      op: opName,
      inputs: {},
      params: {},
      backend: null,
      depends_on: [],
    });
    yamlText = serializeDocument(doc);
    selectedNodeId = id;
  }

  function selectNode(id: string): void {
    selectedNodeId = id;
  }

  // Per-node editor on the right pane — applies edits through the
  // Document AST for round-trip preservation.
  function updateSelectedBackend(backend: string | null): void {
    if (!selectedNodeId) return;
    const doc = loadDocument(yamlText);
    mutateNode(doc, selectedNodeId, { backend: backend || null });
    yamlText = serializeDocument(doc);
  }

  function updateSelectedId(nextId: string): void {
    if (!selectedNodeId || !nextId || nextId === selectedNodeId) return;
    const doc = loadDocument(yamlText);
    if (mutateNode(doc, selectedNodeId, { id: nextId })) {
      yamlText = serializeDocument(doc);
      selectedNodeId = nextId;
    }
  }

  async function save(): Promise<void> {
    saving = true;
    saveError = null;
    try {
      const yamlName = parsed.name || name;
      // Guard: renaming via the YAML `name:` key creates a NEW file
      // at {yamlName}.yaml while leaving the original {name}.yaml in
      // place — a surprise rename-vs-fork. Refuse and point the user
      // at the explicit fork affordance (or, for user profiles, the
      // delete-then-save-as workflow).
      if (yamlName !== name) {
        saveError =
          `Save would create a new profile "${yamlName}" alongside the existing ` +
          `"${name}". To rename, fork to the new name from /ui/profiles, then delete ` +
          `this one.`;
        return;
      }
      if (parsed.kind === 'pipeline') {
        await saveProfile({
          profile_schema_version: '1.0',
          name: yamlName,
          kind: 'pipeline',
          description: parsed.description,
          inputs: parsed.inputs,
          graph: parsed.nodes,
          outputs: parsed.outputs,
        });
      } else {
        saveError = 'Prompt profiles can be edited via YAML but not saved through the workspace yet.';
        return;
      }
      originalYaml = yamlText;
    } catch (e) {
      saveError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      saving = false;
    }
  }

  async function deleteCurrent(): Promise<void> {
    if (!confirm(`Delete profile "${name}"? This cannot be undone.`)) return;
    deleting = true;
    try {
      await deleteProfile(name);
      await goto('/profiles');
    } catch (e) {
      saveError = e instanceof ApiError ? e.detail : String(e);
      deleting = false;
    }
  }

  async function runWithSources(sources: Record<string, string>): Promise<void> {
    showSourcesPicker = false;
    running = true;
    runError = null;
    try {
      const body = await api.post<{ job_id: string }>('/pipelines', {
        pipeline_yaml: yamlText,
        sources: Object.entries(sources).map(([s_name, artifact_id]) => ({
          name: s_name,
          artifact_id,
        })),
      });
      await goto(`/jobs/${body.job_id}`);
    } catch (e) {
      runError = e instanceof ApiError ? e.detail : String(e);
    } finally {
      running = false;
    }
  }

  const selectedNode = $derived(
    selectedNodeId ? parsed.nodes.find((n) => n.id === selectedNodeId) ?? null : null,
  );
</script>

<svelte:head>
  <title>media_engine · {name}</title>
</svelte:head>

<header class="mb-4 flex items-end justify-between gap-3">
  <div>
    <h1 class="text-xl font-semibold mb-1 font-mono" style="color: var(--text-primary);">
      {name}
    </h1>
    <p class="text-xs" style="color: var(--text-muted);">
      {isBundled ? '🔒 bundled · saving forks to your config dir' : '✎ user-editable'}
      {#if sourcePath}
        · <span class="font-mono">{sourcePath}</span>
      {/if}
    </p>
  </div>
  <div class="flex items-center gap-2">
    <button
      type="button"
      onclick={() => void save()}
      disabled={saving || !isPipeline}
      class="px-3 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
      style="background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border-light);"
    >
      {saving ? 'Saving…' : isDirty ? 'Save *' : 'Save'}
    </button>
    <button
      type="button"
      onclick={() => (showSourcesPicker = true)}
      disabled={running || !validation?.ok}
      class="px-3 py-1.5 rounded text-xs font-semibold disabled:opacity-50"
      style="background: var(--accent-green); color: var(--text-inverse);"
      title={validation?.ok ? 'Bind sources and submit POST /pipelines' : 'Fix validation errors before running'}
    >
      {running ? 'Submitting…' : 'Run'}
    </button>
    {#if !isBundled}
      <button
        type="button"
        onclick={() => void deleteCurrent()}
        disabled={deleting}
        class="px-3 py-1.5 rounded text-xs disabled:opacity-50"
        style="background: var(--bg-card); color: var(--accent-red); border: 1px solid rgba(220, 38, 38, 0.35);"
      >
        Delete
      </button>
    {/if}
  </div>
</header>

{#if loadError}
  <p
    class="mb-3 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{loadError}</p>
{/if}
{#if saveError}
  <p
    class="mb-3 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{saveError}</p>
{/if}
{#if runError}
  <p
    class="mb-3 text-xs p-2 rounded"
    style="color: var(--accent-red); background: var(--accent-red-soft); border: 1px solid rgba(220, 38, 38, 0.25);"
  >{runError}</p>
{/if}

<div class="grid grid-cols-12 gap-3 mb-3">
  <section class="col-span-7">
    {#if isPipeline}
      <ProfileComposer
        inputs={parsed.inputs}
        nodes={parsed.nodes}
        selectedNodeId={selectedNodeId}
        invalidNodeIds={invalidNodeIds}
        opPalette={opPalette}
        onSelectNode={selectNode}
        onAddOp={appendNodeFromPalette}
      />
    {:else if parsed.kind === 'prompt'}
      <div
        class="rounded p-4 text-sm"
        style="background: var(--bg-card); border: 1px solid var(--border-soft); color: var(--text-secondary);"
      >
        This is a prompt profile (markdown-with-frontmatter). The visual DAG composer is pipeline-only; edit the YAML side directly.
      </div>
    {:else}
      <div
        class="rounded p-4 text-sm italic"
        style="background: var(--bg-card); border: 1px solid var(--border-soft); color: var(--text-muted);"
      >
        Composer pane waits for valid YAML.
      </div>
    {/if}

    {#if selectedNode}
      <section
        class="mt-3 rounded p-3"
        style="background: var(--bg-card); border: 1px solid var(--border-soft);"
      >
        <div class="text-xs font-semibold uppercase mb-2" style="color: var(--text-muted);">
          Node {selectedNode.id}
        </div>
        <label class="block mb-2">
          <span class="block text-xs mb-1" style="color: var(--text-secondary);">id</span>
          <input
            type="text"
            value={selectedNode.id}
            onchange={(e) => updateSelectedId((e.currentTarget as HTMLInputElement).value.trim())}
            class="w-full px-2 py-1 rounded text-xs font-mono"
            style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
          />
        </label>
        <div class="text-xs font-mono mb-2" style="color: var(--text-muted);">
          op: {selectedNode.op}
        </div>
        <label class="block">
          <span class="block text-xs mb-1" style="color: var(--text-secondary);">backend</span>
          <input
            type="text"
            value={selectedNode.backend ?? ''}
            onchange={(e) => updateSelectedBackend((e.currentTarget as HTMLInputElement).value.trim() || null)}
            placeholder="(default)"
            class="w-full px-2 py-1 rounded text-xs font-mono"
            style="background: var(--bg-page); color: var(--text-primary); border: 1px solid var(--border-light);"
          />
        </label>
        <p class="mt-2 text-xs italic" style="color: var(--text-muted);">
          Per-node param editing (full schema form) is deferred to a future commit. For now,
          edit params directly in the YAML pane on the right — every save round-trips through
          the YAML AST so comments + key order are preserved.
        </p>
      </section>
    {/if}
  </section>

  <section class="col-span-5 flex flex-col gap-2">
    <YAMLEditor
      value={yamlText}
      onChange={(next) => (yamlText = next)}
      opNames={opPalette}
      errorLine={validation && !validation.ok ? validation.line : null}
      minHeight="60vh"
    />
  </section>
</div>

<section
  class="rounded p-3"
  style="background: var(--bg-card); border: 1px solid var(--border-soft);"
>
  <div class="text-xs font-semibold uppercase mb-2" style="color: var(--text-muted);">
    Validation
  </div>
  {#if validating}
    <p class="text-xs italic" style="color: var(--text-muted);">Compiling…</p>
  {:else if !validation}
    <p class="text-xs italic" style="color: var(--text-muted);">Edit the YAML above to see compile feedback.</p>
  {:else if validation.ok}
    <p class="text-xs" style="color: var(--accent-green);">
      ✓ Compiles cleanly — {validation.compiled_nodes.length} node{validation.compiled_nodes.length === 1 ? '' : 's'} ready.
    </p>
  {:else}
    <p class="text-xs font-mono" style="color: var(--accent-red);">
      ✗ {validation.error_class}{validation.line ? ` · line ${validation.line}` : ''}
    </p>
    <pre class="mt-1 text-xs whitespace-pre-wrap font-mono" style="color: var(--text-secondary);">{validation.message}</pre>
  {/if}
</section>

{#if showSourcesPicker}
  <SourcesPicker
    inputs={parsed.inputs}
    onCancel={() => (showSourcesPicker = false)}
    onConfirm={runWithSources}
  />
{/if}
