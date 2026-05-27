/**
 * TTS playback — POST /voice/tts/speak → audio/wav, play via Audio element.
 *
 * The endpoint synthesizes via the currently loaded engine (`POST
 * /voice/engines/tts/load` first). 409 here means no engine is loaded; the
 * UI surface controls that selection from the Voice control panel.
 *
 * `replay()` is the legacy entrypoint used by ChatHistory's per-post speaker
 * button. `speak()` is the explicit one-shot used by VoiceConversation /
 * Voice control panel "Output → Speak". Both share the same fetch/play path
 * but `replay()` swallows errors quietly (the UI button has no error slot),
 * while `speak()` rethrows so callers can show a banner.
 */

export const ttsEnabled = true;

let _audio: HTMLAudioElement | null = null;

async function fetchAudio(text: string): Promise<Blob> {
  const r = await fetch('/voice/tts/speak', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) {
    let detail = '';
    try { detail = await r.text(); } catch { /* ignore */ }
    throw new Error(`tts ${r.status}${detail ? ': ' + detail.slice(0, 200) : ''}`);
  }
  return r.blob();
}

function playBlob(blob: Blob): Promise<void> {
  return new Promise((resolve, reject) => {
    if (_audio) {
      try { _audio.pause(); } catch { /* ignore */ }
      _audio = null;
    }
    const url = URL.createObjectURL(blob);
    const el = new Audio(url);
    _audio = el;
    el.onended = () => { URL.revokeObjectURL(url); resolve(); };
    el.onerror = () => { URL.revokeObjectURL(url); reject(new Error('audio playback failed')); };
    el.play().catch(reject);
  });
}

export async function speak(text: string): Promise<void> {
  const t = (text || '').trim();
  if (!t) return;
  const blob = await fetchAudio(t);
  await playBlob(blob);
}

export function replay(text: string): void {
  speak(text).catch((err) => {
    // eslint-disable-next-line no-console
    console.warn('[tts replay]', err);
  });
}

export function stopPlayback(): void {
  if (_audio) {
    try { _audio.pause(); } catch { /* ignore */ }
    _audio = null;
  }
}
