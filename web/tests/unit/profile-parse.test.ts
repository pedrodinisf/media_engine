import { describe, expect, it } from 'vitest';
import {
  addNode,
  loadDocument,
  mutateNode,
  parseProfileText,
  removeNode,
  serializeDocument,
} from '$lib/profile/parse';

const PIPELINE_WITH_COMMENTS = `# Top-of-file comment — must survive round-trips.
name: my-pipeline
kind: pipeline
description: ""

# Sources picked at run time.
inputs:
  - { name: source, kind: video }

# Graph nodes — order is preserved.
graph:
  - id: audio
    op: video.extract_audio
    inputs: { in: source }

  - id: transcript
    op: audio.transcribe
    inputs: { audio: audio }
    params:
      language: en

outputs:
  - transcript
`;

describe('parseProfileText', () => {
  it('extracts kind, inputs, graph, and outputs', () => {
    const parsed = parseProfileText(PIPELINE_WITH_COMMENTS);
    expect(parsed.kind).toBe('pipeline');
    expect(parsed.name).toBe('my-pipeline');
    expect(parsed.inputs).toEqual([{ name: 'source', kind: 'video' }]);
    expect(parsed.nodes).toHaveLength(2);
    expect(parsed.nodes[0]?.id).toBe('audio');
    expect(parsed.nodes[1]?.params).toEqual({ language: 'en' });
    expect(parsed.outputs).toEqual(['transcript']);
    expect(parsed.isPipeline).toBe(true);
  });

  it('returns EMPTY_GRAPH for unparseable input', () => {
    const parsed = parseProfileText('not: valid: [yaml');
    expect(parsed.kind).toBe('unknown');
    expect(parsed.nodes).toEqual([]);
  });

  it('returns EMPTY_GRAPH for empty input', () => {
    expect(parseProfileText('').kind).toBe('unknown');
    expect(parseProfileText('   ').kind).toBe('unknown');
  });
});

describe('Document round-trip', () => {
  it('preserves the top-of-file comment after mutating a node', () => {
    const doc = loadDocument(PIPELINE_WITH_COMMENTS);
    mutateNode(doc, 'transcript', { backend: 'mlx-whisper' });
    const out = serializeDocument(doc);
    expect(out).toContain('# Top-of-file comment');
    expect(out).toContain('# Sources picked at run time.');
    expect(out).toContain('# Graph nodes — order is preserved.');
    expect(out).toContain('backend: mlx-whisper');
  });

  it('renames a node id without disturbing others', () => {
    const doc = loadDocument(PIPELINE_WITH_COMMENTS);
    const ok = mutateNode(doc, 'audio', { id: 'audio_renamed' });
    expect(ok).toBe(true);
    const out = serializeDocument(doc);
    const reparsed = parseProfileText(out);
    expect(reparsed.nodes.map((n) => n.id)).toEqual([
      'audio_renamed',
      'transcript',
    ]);
  });

  it('returns false when mutating a non-existent node', () => {
    const doc = loadDocument(PIPELINE_WITH_COMMENTS);
    expect(mutateNode(doc, 'no-such-id', { backend: 'x' })).toBe(false);
  });

  it('appends a new node via addNode', () => {
    const doc = loadDocument(PIPELINE_WITH_COMMENTS);
    addNode(doc, {
      id: 'summary',
      op: 'intelligence.summarize',
      inputs: { tx: 'transcript' },
      params: {},
      backend: null,
      depends_on: [],
    });
    const out = serializeDocument(doc);
    const reparsed = parseProfileText(out);
    expect(reparsed.nodes.map((n) => n.id)).toEqual([
      'audio',
      'transcript',
      'summary',
    ]);
    expect(reparsed.nodes[2]?.op).toBe('intelligence.summarize');
    // Comments still present.
    expect(out).toContain('# Graph nodes — order is preserved.');
  });

  it('removes a node by id', () => {
    const doc = loadDocument(PIPELINE_WITH_COMMENTS);
    expect(removeNode(doc, 'audio')).toBe(true);
    const out = serializeDocument(doc);
    const reparsed = parseProfileText(out);
    expect(reparsed.nodes.map((n) => n.id)).toEqual(['transcript']);
  });

  it('serialises a freshly-loaded document near-identically to the input', () => {
    // Identity round-trip (no mutations) should re-emit the same
    // YAML — comments, blank lines, ordering. The `yaml` lib's
    // Document model is engineered for this.
    const doc = loadDocument(PIPELINE_WITH_COMMENTS);
    const out = serializeDocument(doc);
    // Allow optional trailing-newline differences; compare by
    // stripped content.
    expect(out.trim()).toEqual(PIPELINE_WITH_COMMENTS.trim());
  });
});
