/**
 * Clean-NASA design tokens — TS mirror of app.css.
 *
 * Use these when a component needs the raw value (e.g. setting an inline
 * style for a chart color). For ordinary styling, prefer Tailwind utility
 * classes generated from the @theme block (`bg-bg-page`, `text-text-primary`,
 * etc.) so the design-system entry point stays canonical.
 */

export const tokens = {
  bg: {
    page: '#F5F4EE',
    card: '#F5F5F0',
    alt: '#EEEDE8',
    deep: '#E6E5DE',
    header: '#2A3328',
  },
  border: {
    light: '#D1D5DB',
    warm: '#C9C5B8',
    soft: '#DAD8CE',
  },
  accent: {
    green: '#059669',
    greenSoft: 'rgba(5, 150, 105, 0.08)',
    greenLine: 'rgba(5, 150, 105, 0.22)',
    amber: '#D97706',
    amberSoft: 'rgba(217, 119, 6, 0.08)',
    amberLine: 'rgba(217, 119, 6, 0.25)',
    red: '#DC2626',
    redSoft: 'rgba(220, 38, 38, 0.08)',
  },
  text: {
    primary: '#2D2D2D',
    secondary: '#555D66',
    muted: '#7B8490',
    inverse: '#F5F5F0',
  },
  font: {
    sans:
      "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', Roboto, system-ui, sans-serif",
    mono: "'Monaco', 'Menlo', 'Consolas', 'JetBrains Mono', monospace",
  },
} as const;

export type Tokens = typeof tokens;
