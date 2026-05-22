import { describe, expect, it } from 'vitest';
import { kindAccent, layoutLineage } from '$lib/components/dag/lineage-layout';
import type { LineageNode } from '$lib/api/lineage';

// Tiny fixture: an Analysis derived from a Transcript derived from an
// Audio derived from a Video. Mirrors the analysis-full pipeline shape.
const sampleTree: LineageNode = {
  artifact_id: 'analysis-root',
  kind: 'analysis',
  op: 'intelligence.analyze',
  backend: 'gemini',
  inputs: [
    {
      artifact_id: 'transcript-1',
      kind: 'transcript',
      op: 'audio.transcribe',
      backend: 'mlx-whisper',
      inputs: [
        {
          artifact_id: 'audio-1',
          kind: 'audio',
          op: 'video.extract_audio',
          inputs: [
            {
              artifact_id: 'video-1',
              kind: 'video',
              op: 'acquire.url',
              backend: 'yt-dlp',
            },
          ],
        },
      ],
    },
  ],
};

describe('lineage layoutLineage', () => {
  it('emits one node per unique artifact id', () => {
    const { nodes } = layoutLineage(sampleTree);
    expect(nodes.map((n) => n.id).sort()).toEqual([
      'analysis-root',
      'audio-1',
      'transcript-1',
      'video-1',
    ]);
  });

  it('emits one edge per parent → input pairing', () => {
    const { edges } = layoutLineage(sampleTree);
    // Edges flow from upstream (input) to downstream (parent).
    expect(edges.map((e) => `${e.source}->${e.target}`).sort()).toEqual([
      'audio-1->transcript-1',
      'transcript-1->analysis-root',
      'video-1->audio-1',
    ]);
  });

  it('flags the root with is_root=true', () => {
    const { nodes } = layoutLineage(sampleTree);
    const root = nodes.find((n) => n.id === 'analysis-root');
    expect(root?.data.is_root).toBe(true);
    for (const n of nodes) {
      if (n.id !== 'analysis-root') {
        expect(n.data.is_root).toBe(false);
      }
    }
  });

  it('dedupes fan-in: a shared upstream id appears once', () => {
    // Two transcripts both pointing at the same audio.
    const tree: LineageNode = {
      artifact_id: 'analysis-fanin',
      kind: 'analysis',
      inputs: [
        {
          artifact_id: 'transcript-a',
          kind: 'transcript',
          inputs: [{ artifact_id: 'audio-shared', kind: 'audio' }],
        },
        {
          artifact_id: 'transcript-b',
          kind: 'transcript',
          inputs: [{ artifact_id: 'audio-shared', kind: 'audio' }],
        },
      ],
    };
    const { nodes, edges } = layoutLineage(tree);
    expect(nodes.filter((n) => n.id === 'audio-shared')).toHaveLength(1);
    // Two upstream edges from the shared audio, one to each transcript.
    expect(
      edges.filter((e) => e.source === 'audio-shared').map((e) => e.target).sort(),
    ).toEqual(['transcript-a', 'transcript-b']);
  });

  it('propagates truncated_reason through the layout', () => {
    const tree: LineageNode = {
      artifact_id: 'analysis-truncated',
      kind: 'analysis',
      inputs: [
        {
          artifact_id: 'transcript-deep',
          kind: 'transcript',
          truncated_reason: 'max_depth',
        },
      ],
    };
    const { nodes } = layoutLineage(tree);
    const truncated = nodes.find((n) => n.id === 'transcript-deep');
    expect(truncated?.data.truncated_reason).toBe('max_depth');
  });

  it('positions every node with concrete numeric coordinates', () => {
    const { nodes } = layoutLineage(sampleTree);
    for (const n of nodes) {
      expect(Number.isFinite(n.position.x)).toBe(true);
      expect(Number.isFinite(n.position.y)).toBe(true);
    }
  });
});

describe('lineage kindAccent', () => {
  it('groups media kinds under green', () => {
    for (const kind of ['video', 'audio', 'image', 'frameset']) {
      expect(kindAccent(kind).label).toContain('green');
    }
  });

  it('groups analysis kinds under amber', () => {
    for (const kind of ['analysis', 'session_analysis', 'embedding']) {
      expect(kindAccent(kind).label).toContain('amber');
    }
  });

  it('falls back to neutral text-secondary for everything else', () => {
    for (const kind of ['transcript', 'diarization', 'markdown', 'webpage', 'unknown']) {
      expect(kindAccent(kind).label).toContain('text-secondary');
    }
  });
});
