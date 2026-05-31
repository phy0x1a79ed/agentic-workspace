"""GPT-SoVITS sidecar for the native-TTS eval (port 7841).

Zero-shot voice cloning from a 5–10 s reference clip. Native TTS — no
RVC-style conversion step. Reference clip lives on disk; caller passes the
path + the transcribed reference text in the /synth body.

API
---
GET  /health  → {ready, sample_rate, vram_mb, gpt_ckpt, sovits_ckpt}
POST /synth   body: {text, ref_wav_path, ref_text, language?, top_k?, top_p?, temperature?}
              returns: audio/wav; headers:
                X-TTFA-Ms, X-Total-Ms, X-Audio-Sec, X-Sample-Rate, X-VRAM-Used-Mb

Run
---
mamba run -n awm-gptsovits python demo/tts_gptsovits_service.py --port 7841 \\
    --repo-root demo/voices/native-eval/_repos/GPT-SoVITS

The sidecar imports GPT_SoVITS.inference_webui's TTS_Config / pipeline
classes (the supported zero-shot path used by the official webui).
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-gptsovits")

REPO = Path(__file__).resolve().parents[1]
DEFAULT_GPTSOVITS_ROOT = REPO / "demo/voices/native-eval/_repos/GPT-SoVITS"

# Filled in main() and used by the lazy loader.
_GPTSOVITS_ROOT: Optional[Path] = None
_TTS_PIPE = None
_SAMPLE_RATE: Optional[int] = None


def _vram_mb() -> int:
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():
            return int(torch.cuda.memory_allocated() / (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    return 0


def _load_pipeline():
    """Lazy-load GPT-SoVITS using its v2 inference module.

    The library is not pip-installable; it must be cloned and added to
    sys.path. See demo/env-gptsovits.yml for setup.
    """
    global _TTS_PIPE, _SAMPLE_RATE
    if _TTS_PIPE is not None:
        return _TTS_PIPE
    assert _GPTSOVITS_ROOT is not None

    sys.path.insert(0, str(_GPTSOVITS_ROOT))
    sys.path.insert(0, str(_GPTSOVITS_ROOT / "GPT_SoVITS"))
    # The webui sets these env vars before import — replicate.
    os.environ.setdefault("is_share", "False")
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config  # noqa: PLC0415

    config_yaml = _GPTSOVITS_ROOT / "GPT_SoVITS/configs/tts_infer.yaml"
    log.info("loading GPT-SoVITS from %s", config_yaml)
    t = time.monotonic()
    cfg = TTS_Config(str(config_yaml))
    _TTS_PIPE = TTS(cfg)
    # Warmup pass to populate sample rate.
    _SAMPLE_RATE = int(getattr(cfg, "sampling_rate", 32000))
    log.info("GPT-SoVITS ready in %.1fs (sr=%d)", time.monotonic() - t, _SAMPLE_RATE)
    return _TTS_PIPE


# --- app ------------------------------------------------------------------

app = FastAPI()


@app.get("/health")
async def health():
    return {
        "ready": _TTS_PIPE is not None,
        "sample_rate": _SAMPLE_RATE,
        "vram_mb": _vram_mb(),
        "repo_root": str(_GPTSOVITS_ROOT) if _GPTSOVITS_ROOT else None,
    }


@app.get("/voices")
async def voices():
    return JSONResponse([{
        "label": "gpt-sovits-zero-shot",
        "kind": "native-zero-shot",
        "language": "en/ja/zh",
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
    language = payload.get("language", "en")
    if not text:
        raise HTTPException(400, "empty text")
    if not ref_path:
        raise HTTPException(400, "ref_wav_path required (zero-shot engine)")
    if not Path(ref_path).exists():
        raise HTTPException(404, f"ref_wav_path not found: {ref_path}")
    if not ref_text:
        raise HTTPException(400, "ref_text required (transcript of ref_wav_path)")

    pipe = _load_pipeline()

    request_args = {
        "text": text,
        "text_lang": language,
        "ref_audio_path": ref_path,
        "prompt_text": ref_text,
        "prompt_lang": language,
        "top_k": int(payload.get("top_k", 5)),
        "top_p": float(payload.get("top_p", 1.0)),
        "temperature": float(payload.get("temperature", 1.0)),
        "text_split_method": "cut5",
        "batch_size": 1,
        "speed_factor": 1.0,
        "return_fragment": False,
    }

    t0 = time.monotonic()
    audio_data = list(pipe.run(request_args))  # yields (sr, np.int16)
    t_total = time.monotonic() - t0
    if not audio_data:
        raise HTTPException(500, "no audio produced")
    sr, pcm = audio_data[0]
    sr = int(sr)
    duration = len(pcm) / float(sr)

    wav = _wav_bytes(pcm, sr)
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
    global _GPTSOVITS_ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7841)
    parser.add_argument("--repo-root", default=str(DEFAULT_GPTSOVITS_ROOT),
                        help="cloned GPT-SoVITS repo directory")
    parser.add_argument("--warm", action="store_true")
    args = parser.parse_args()
    _GPTSOVITS_ROOT = Path(args.repo_root).resolve()
    if not _GPTSOVITS_ROOT.exists():
        log.warning("GPT-SoVITS repo not found at %s — sidecar will fail on first /synth",
                    _GPTSOVITS_ROOT)
    if args.warm:
        _load_pipeline()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
