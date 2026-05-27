"""F5-TTS sidecar for the native-TTS eval (port 7842).

Zero-shot voice cloning via the F5-TTS diffusion model (SWivid/F5-TTS).
Reference clip lives on disk; caller passes the path + reference transcript.

API
---
GET  /health  → {ready, sample_rate, vram_mb, model}
POST /synth   body: {text, ref_wav_path, ref_text, model?, nfe_step?, cfg_strength?}
              returns: audio/wav; headers:
                X-TTFA-Ms, X-Total-Ms, X-Audio-Sec, X-Sample-Rate, X-VRAM-Used-Mb

Run
---
mamba run -n awm-f5tts python demo/tts_f5tts_service.py --port 7842
"""
from __future__ import annotations

import argparse
import io
import logging
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-f5tts")

_API = None
_MODEL_LABEL = "F5TTS_v1_Base"  # f5-tts >=1.1 renamed the constructor arg
_SAMPLE_RATE: Optional[int] = None


def _vram_mb() -> int:
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():
            return int(torch.cuda.memory_allocated() / (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    return 0


def _load_api(model_label: str = _MODEL_LABEL):
    global _API, _SAMPLE_RATE, _MODEL_LABEL
    if _API is not None:
        return _API

    from f5_tts.api import F5TTS  # noqa: PLC0415

    log.info("loading F5-TTS model=%s", model_label)
    t = time.monotonic()
    _API = F5TTS(model=model_label, device="cuda")
    _MODEL_LABEL = model_label
    # F5-TTS outputs at 24kHz by convention.
    _SAMPLE_RATE = 24_000
    log.info("F5-TTS ready in %.1fs (sr=%d)", time.monotonic() - t, _SAMPLE_RATE)
    return _API


# --- app ------------------------------------------------------------------

app = FastAPI()


@app.get("/health")
async def health():
    return {
        "ready": _API is not None,
        "sample_rate": _SAMPLE_RATE,
        "model": _MODEL_LABEL,
        "vram_mb": _vram_mb(),
    }


@app.get("/voices")
async def voices():
    return JSONResponse([{
        "label": "f5-tts-zero-shot",
        "kind": "native-zero-shot",
        "language": "en/zh",
        "needs_reference": True,
    }])


def _wav_bytes(pcm: np.ndarray, sr: int) -> bytes:
    if pcm.dtype != np.int16:
        pcm = (np.clip(pcm, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


@app.post("/synth")
async def synth(request: Request):
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    ref_path = payload.get("ref_wav_path")
    ref_text = (payload.get("ref_text") or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    if not ref_path or not Path(ref_path).exists():
        raise HTTPException(404, f"ref_wav_path not found: {ref_path}")

    api = _load_api(payload.get("model", _MODEL_LABEL))

    t0 = time.monotonic()
    # F5TTS.infer returns (wav_np_f32, sample_rate, spectrogram).
    seed = payload.get("seed")
    seed = int(seed) if seed is not None and int(seed) >= 0 else None
    wav_f32, sr, _ = api.infer(
        ref_file=ref_path,
        ref_text=ref_text,
        gen_text=text,
        nfe_step=int(payload.get("nfe_step", 32)),
        cfg_strength=float(payload.get("cfg_strength", 2.0)),
        remove_silence=bool(payload.get("remove_silence", False)),
        seed=seed,
    )
    t_total = time.monotonic() - t0

    sr = int(sr)
    duration = float(len(wav_f32)) / sr
    wav = _wav_bytes(wav_f32, sr)
    log.info("synth: chars=%d sr=%d dur=%.2fs total=%.0fms",
             len(text), sr, duration, t_total * 1000)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            "X-TTFA-Ms": str(int(t_total * 1000)),
            "X-Total-Ms": str(int(t_total * 1000)),
            "X-Audio-Sec": f"{duration:.3f}",
            "X-Sample-Rate": str(sr),
            "X-VRAM-Used-Mb": str(_vram_mb()),
            "Cache-Control": "no-store",
        },
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7842)
    parser.add_argument("--model", default=_MODEL_LABEL)
    parser.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    if args.warm:
        _load_api(args.model)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
