/**
 * Injects the user's global Claude Code instructions
 * (`~/.claude/CLAUDE.md`, reached through the `global-instructions.md`
 * symlink beside this profile) into the DeepSeek Harness system prompt, as
 * the `user:global-instructions` section (order 10 — after the deployment
 * persona, before tool guidance).
 *
 * The file is read once, at `apply`: editing the source file only reaches
 * the model after the harness restarts, not on the next turn or session.
 * Registered as a `systemPrompt` section rather than a session message, so
 * the text renders as system-prompt text; model-visible ⟺ logged is already
 * satisfied by the existing `request/header` EpochHeader, so this plugin
 * adds no session event of its own.
 *
 * @module global-instructions
 */

import { readFileSync } from 'node:fs'

/**
 * @typedef {object} Config
 * @property {string} [path] - absolute path to the instructions file. A
 *   symlink resolves fine (`readFileSync` follows it, `stat` not `lstat`).
 * @property {number} [maxBytes] - byte cap on the loaded text; default
 *   65536. A longer file is truncated at load.
 * @property {boolean} [required] - when true, a missing `path` or an
 *   unreadable file throws at `apply` (fail loud) instead of being skipped.
 * @property {string} [prefix] - text rendered above the file body. This
 *   row's config carries the Claude Code -> DeepSeek Harness framing (the
 *   file was authored for a different harness, so the model needs the tool
 *   name mapping and permission to skip inapplicable directives).
 */

/** Default {@link Config.maxBytes} when the row omits it. */
const DEFAULT_MAX_BYTES = 65536

/** Cordis plugin name. */
export const name = 'global-instructions'

/** The prompt registry this row contributes to. */
export const inject = ['systemPrompt']

/**
 * Neutralize `{{`: the prompt registry's interpolation
 * (`@deepseek-ai/dsh-system-prompt`'s `interpolate()`) is strict — an
 * unrecognized `{{name}}` throws at every assembly, and there is no escape
 * syntax. A user-authored instructions file has no reason to use that
 * syntax, so a literal match is rewritten rather than left to fail the next
 * prompt assembly.
 * @param {string} text - the loaded file text.
 * @param {import('@deepseek-ai/cordis').Context} ctx - used to log the rewrite.
 * @returns {string} `text`, with any `{{` split to `{ {`.
 */
function guardInterpolation(text, ctx) {
  if (!text.includes('{{')) return text
  ctx.logger.warn('global-instructions: source text contains "{{"; rewriting to avoid prompt-variable interpolation')
  return text.replaceAll('{{', '{ {')
}

/**
 * Read `config.path` once and register it as the `user:global-instructions`
 * system-prompt section.
 * @param {import('@deepseek-ai/cordis').Context} ctx - injects `systemPrompt`.
 * @param {Config} config - see {@link Config}.
 */
export function apply(ctx, config) {
  const path = config?.path
  if (typeof path !== 'string' || path.length === 0) {
    if (config?.required === true) throw new Error('global-instructions: config.path is required')
    return
  }
  const maxBytes = config?.maxBytes ?? DEFAULT_MAX_BYTES
  let raw
  try {
    raw = readFileSync(path, 'utf8')
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error)
    if (config?.required === true) throw new Error(`global-instructions: could not read "${path}": ${reason}`)
    ctx.logger.warn(`global-instructions: could not read "${path}", skipping: ${reason}`)
    return
  }
  const rawBytes = Buffer.byteLength(raw, 'utf8')
  let text = raw
  if (rawBytes > maxBytes) {
    text = Buffer.from(raw, 'utf8').subarray(0, maxBytes).toString('utf8')
    ctx.logger.warn(`global-instructions: "${path}" is ${rawBytes} bytes, exceeding the ${maxBytes}-byte cap; truncated`)
  }
  text = guardInterpolation(text, ctx)
  const prefix = typeof config?.prefix === 'string' ? config.prefix : ''
  if (prefix.length > 0) text = `${prefix}\n\n${text}`
  ctx.effect(() => ctx.systemPrompt.section({
    name: 'user:global-instructions',
    order: 10,
    text,
  }), 'global-instructions.section()')
}
