/**
 * Format a duration in seconds as mm:ss (or h:mm:ss when ≥ 1 hour).
 *
 * Used by the audio range slider in the Run panel and anywhere else
 * we render a time offset. Pure function, no locale dependency, no
 * Intl bullshit — easy to test, predictable across browsers.
 *
 * Negative / non-finite / NaN inputs collapse to "00:00" so the UI
 * never renders garbage.
 */
export function formatDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '00:00';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number): string => n.toString().padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
