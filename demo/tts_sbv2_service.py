"""Style-Bert-VITS2 sidecar for the native-TTS eval (port 7843).

Loads the GLaDOS Style-Bert-VITS2 checkpoint (WarriorMama777/GLaDOS_TTS)
from HF on first start. No voice conversion — this is pure native TTS for
characters with a dedicated pretrained checkpoint.

API
---
GET  /health  → {ready, model, sample_rate, vram_mb}
GET  /voices  → [{label, kind, language, has_reference}]
POST /synth   body: {text, voice_id?, style?, sdp_ratio?, length_scale?}
              returns: audio/wav blob; headers:
                X-TTFA-Ms, X-Total-Ms, X-Audio-Sec, X-Sample-Rate, X-VRAM-Used-Mb

Run
---
mamba run -n awm-sbv2 python demo/tts_sbv2_service.py --port 7843
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-sbv2")

REPO = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO / "demo/voices/native-eval/_models/sbv2"
GLADOS_REPO = "WarriorMama777/GLaDOS_TTS"

_MODEL = None
_MODEL_LABEL = "glados"
_SAMPLE_RATE: Optional[int] = None


def _vram_mb() -> int:
    try:
        import torch  # noqa: PLC0415
        if torch.cuda.is_available():
            return int(torch.cuda.memory_allocated() / (1024 * 1024))
    except Exception:  # noqa: BLE001
        pass
    return 0


def _fetch_model() -> Path:
    """Snapshot-download the GLaDOS SBV2 checkpoint into MODELS_DIR/glados/.

    The repo has both GPT-SoVITS and Style-Bert_VITS2 subtrees. Only the
    sbv2 subtree (~200 MB) is needed here.
    """
    target = MODELS_DIR / "glados"
    sbv2_root = target / "Models/Style-Bert_VITS2"
    if sbv2_root.exists() and any(sbv2_root.rglob("*.safetensors")):
        return target
    from huggingface_hub import snapshot_download  # noqa: PLC0415

    target.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s (sbv2 subtree only) → %s", GLADOS_REPO, target)
    snapshot_download(
        repo_id=GLADOS_REPO,
        local_dir=str(target),
        allow_patterns=["Models/Style-Bert_VITS2/**"],
    )
    return target


def _resolve_assets(root: Path) -> tuple[Path, Path, Path]:
    """Locate the .safetensors, config.json, style_vectors.npy inside root.

    The HF repo layout varies between authors — search recursively for each
    expected file.
    """
    # Prefer .safetensors over .ckpt/.pth (sbv2 native format).
    safetensors = list(root.rglob("*.safetensors"))
    if not safetensors:
        raise FileNotFoundError(f"no .safetensors under {root}")
    model_path = sorted(safetensors)[-1]  # highest-epoch usually sorts last
    parent = model_path.parent

    config = parent / "config.json"
    if not config.exists():
        cands = list(root.rglob("config.json"))
        if not cands:
            raise FileNotFoundError(f"no config.json under {root}")
        config = cands[0]

    style = parent / "style_vectors.npy"
    if not style.exists():
        cands = list(root.rglob("style_vectors.npy"))
        if not cands:
            raise FileNotFoundError(f"no style_vectors.npy under {root}")
        style = cands[0]

    return model_path, config, style


def _load_model():
    global _MODEL, _SAMPLE_RATE
    if _MODEL is not None:
        return _MODEL

    from style_bert_vits2.tts_model import TTSModel  # noqa: PLC0415
    from style_bert_vits2.nlp import bert_models  # noqa: PLC0415
    from style_bert_vits2.constants import Languages  # noqa: PLC0415

    # English BERT for EN inference (model is English-only per HF README).
    en_bert = "microsoft/deberta-v3-large"
    bert_models.load_model(Languages.EN, en_bert)
    bert_models.load_tokenizer(Languages.EN, en_bert)

    root = _fetch_model()
    model_path, config_path, style_path = _resolve_assets(root)
    log.info("loading sbv2 model=%s config=%s style=%s",
             model_path.name, config_path.name, style_path.name)

    t = time.monotonic()
    _MODEL = TTSModel(
        model_path=model_path,
        config_path=config_path,
        style_vec_path=style_path,
        device="cuda" if os.environ.get("CUDA_VISIBLE_DEVICES", "0") != "" else "cpu",
    )
    # Library defaults to JA g2p (pyopenjtalk) unless language=EN is passed.
    sr, _ = _MODEL.infer(text="ready.", language=Languages.EN)
    _SAMPLE_RATE = int(sr)
    log.info("sbv2 ready in %.1fs sr=%d", time.monotonic() - t, _SAMPLE_RATE)
    return _MODEL


# --- app ------------------------------------------------------------------

app = FastAPI()


@app.get("/health")
async def health():
    return {
        "ready": _MODEL is not None,
        "model": _MODEL_LABEL,
        "sample_rate": _SAMPLE_RATE,
        "vram_mb": _vram_mb(),
    }


@app.get("/voices")
async def voices():
    return JSONResponse([{
        "label": _MODEL_LABEL,
        "kind": "native",
        "language": "en",
        "has_reference": False,
    }])


def _wav_bytes(pcm: np.ndarray, sr: int) -> bytes:
    """Write mono int16 WAV. Accepts int16 (passed through) or float in [-1, 1]."""
    if pcm.dtype == np.int16:
        pcm_i16 = pcm
    else:
        pcm_i16 = (np.clip(pcm.astype(np.float32), -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm_i16.tobytes())
    return buf.getvalue()


@app.post("/synth")
async def synth(request: Request):
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty text")

    # SBV2 inference knobs. Defaults below are tuned for the GLaDOS checkpoint
    # — slower length + lower noise + Standard style give a more characteristic
    # mechanical-deliberate delivery than the library's Neutral/noise=0.6 defaults.
    sdp_ratio   = float(payload.get("sdp_ratio", 0.2))
    length      = float(payload.get("length_scale", 1.1))
    noise       = float(payload.get("noise", 0.4))
    noise_w     = float(payload.get("noise_w", 0.8))
    style       = payload.get("style", "Standard")
    style_weight = float(payload.get("style_weight", 1.0))

    model = _load_model()

    from style_bert_vits2.constants import Languages  # noqa: PLC0415
    t0 = time.monotonic()
    sr, audio = model.infer(
        text=text,
        language=Languages.EN,
        sdp_ratio=sdp_ratio,
        length=length,
        noise=noise,
        noise_w=noise_w,
        style=style,
        style_weight=style_weight,
    )
    t_total = time.monotonic() - t0

    # Library returns int16 numpy by default.
    if audio.dtype == np.int16:
        pcm = audio
    else:
        pcm = np.clip(audio, -1, 1)
    duration = len(pcm) / float(sr)

    wav = _wav_bytes(pcm, sr)
    log.info("synth: chars=%d sr=%d dur=%.2fs total=%.0fms",
             len(text), sr, duration, t_total * 1000)
    return Response(
        content=wav,
        media_type="audio/wav",
        headers={
            # Non-streaming engine: TTFA == total
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
    parser.add_argument("--port", type=int, default=7843)
    parser.add_argument("--warm", action="store_true",
                        help="load the model at startup instead of first request")
    args = parser.parse_args()
    if args.warm:
        _load_model()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
