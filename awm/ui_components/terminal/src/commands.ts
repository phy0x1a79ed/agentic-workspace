/**
 * The command grid: labelled control-byte sequences the terminal chrome can
 * send into a live agent pane. Everything routes through the same `send()` as a
 * keystroke, so these are just pre-baked byte strings (escape codes / slash
 * commands + Enter). Data-driven so the page can extend or reorder them.
 */
export interface TermCommand {
  label: string;
  /** Raw bytes written to the pane (xterm/tmux keystroke stream). */
  bytes: string;
  hint?: string;
  danger?: boolean;
}

export const COMMAND_GRID: TermCommand[] = [
  { label: 'Esc', bytes: '\x1b', hint: 'cancel / interrupt' },
  { label: 'Ctrl-C', bytes: '\x03', hint: 'interrupt', danger: true },
  { label: 'Enter', bytes: '\r', hint: 'submit' },
  { label: '⇧Tab', bytes: '\x1b[Z', hint: 'cycle mode' },
  { label: '↑', bytes: '\x1b[A', hint: 'history up' },
  { label: '↓', bytes: '\x1b[B', hint: 'history down' },
  { label: '/compact', bytes: '/compact\r', hint: 'compact context' },
  { label: '/clear', bytes: '/clear\r', hint: 'clear context', danger: true },
];
