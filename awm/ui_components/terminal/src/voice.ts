/**
 * Device-native speech-to-text via the browser's Web Speech API — the phone's
 * own dictation, NOT the awm server-side `stt` pipeline. One-shot: start
 * listening, stream interim text to a callback, resolve the final transcript.
 *
 * Availability is spotty (Chrome/Android/desktop yes; Firefox no), so callers
 * must feature-detect with `voiceAvailable()` and hide the button when false.
 */

type SpeechRecognitionCtor = new () => any;

function ctor(): SpeechRecognitionCtor | null {
  if (typeof window === 'undefined') return null;
  const w = window as any;
  return w.SpeechRecognition || w.webkitSpeechRecognition || null;
}

export function voiceAvailable(): boolean {
  return ctor() !== null;
}

export interface Dictation {
  /** Stop listening now (keeps whatever was recognised). */
  stop(): void;
  /** Abort and discard. */
  abort(): void;
}

export interface DictationHandlers {
  /** Live partial transcript as the user speaks. */
  onInterim?: (text: string) => void;
  /** Final recognised text (once), before `onEnd`. */
  onFinal?: (text: string) => void;
  /** Recognition ended (stopped / errored / silence). */
  onEnd?: (error?: string) => void;
}

/** Begin dictation. Returns null if the API is unavailable. */
export function startDictation(handlers: DictationHandlers): Dictation | null {
  const C = ctor();
  if (!C) return null;
  const rec = new C();
  rec.lang = navigator.language || 'en-US';
  rec.interimResults = true;
  rec.continuous = false;
  let finalText = '';

  rec.onresult = (ev: any) => {
    let interim = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) finalText += r[0].transcript;
      else interim += r[0].transcript;
    }
    if (interim) handlers.onInterim?.(interim);
  };
  rec.onerror = (ev: any) => handlers.onEnd?.(ev?.error || 'speech error');
  rec.onend = () => {
    if (finalText) handlers.onFinal?.(finalText.trim());
    handlers.onEnd?.();
  };
  try {
    rec.start();
  } catch {
    return null;
  }
  return {
    stop: () => { try { rec.stop(); } catch { /* ignore */ } },
    abort: () => { try { rec.abort(); } catch { /* ignore */ } },
  };
}
