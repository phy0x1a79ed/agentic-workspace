/**
 * TTS playback stub.
 *
 * The kyutai pocket-tts backend port (mirroring vagrant-shell/awm/voice/tts.py
 * into web-ui's awm/voice/ + a /voice/tts WS endpoint) is deferred. This
 * module exists so the Transcript replay button has somewhere to call —
 * when the real service lands, swap the implementation here and every
 * caller picks it up automatically.
 */

export const ttsEnabled = true;

export function replay(text: string): void {
  // eslint-disable-next-line no-console
  console.log('[tts mock] replay:', text);
}
