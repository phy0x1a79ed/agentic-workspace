/** Status → CSS color variable. Mirrors roma-ui's statusColor() pattern. */
export const STATUS_COLORS: Record<string, string> = {
  // Agent / room state
  idle:        'var(--text3)',
  pending:     'var(--text3)',
  ready:       'var(--text3)',
  active:      'var(--atomizer)',
  running:     'var(--atomizer)',
  planning:    '#a855f7',
  recording:   'var(--recording)',
  verifying:   'var(--warn)',
  completed:   'var(--ok)',
  done:        'var(--ok)',
  failed:      'var(--danger)',
  cancelled:   'var(--danger)',
  closed:      'var(--text3)',
  archived:    'var(--text3)'
};

export function statusColor(s: string | undefined | null): string {
  return STATUS_COLORS[(s || '').toLowerCase()] ?? 'var(--text2)';
}
