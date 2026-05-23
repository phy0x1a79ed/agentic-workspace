"""Combined Kokoro TTS + RVC voice conversion sidecar (port 7833).

Holds both model stacks in one process so PCM never leaves the heap between
Kokoro and RVC. Pipelined: kokoro streams chunks → RVC converts each chunk
→ HTTP flushes each converted chunk to the browser. RVC models are loaded
lazily and kept in an LRU cache (3 entries — keep VRAM headroom for the
sidecar at 7831 if it's also running).

POST /stream
    body: {text, tts_voice, rvc_label?, pitch?, lang?}
    response: length-prefixed PCM frames `[u32 LE n_bytes][int16 PCM bytes]…`
    headers: Content-Type=application/octet-stream, X-Sample-Rate=24000|48000,
             Access-Control-Allow-Origin=*

GET /voices  → {tts: [...english voice ids...], rvc: [{label, repo, has_index, version}, ...]}
GET /health  → readiness + currently-loaded RVC labels
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import struct
import sys
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import uvicorn

# Import RVCWrapper from the existing sidecar to avoid code duplication.
sys.path.insert(0, str(Path(__file__).parent))
from rvc_service import RVCWrapper  # noqa: E402

from kokoro_onnx import Kokoro  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-rvc")

REPO = Path(__file__).resolve().parents[1]
RVC_LIBRARY = REPO / "demo/voices/rvc"
KOKORO_SR = 24_000
# Note: actual RVC output SR varies per model (32k / 40k / 48k) — pulled from
# rvc.vc.tgt_sr after load and propagated to the browser via X-Sample-Rate.

# English Kokoro voices (filtered out non-English at the boundary).
ENGLISH_PREFIXES = ("af_", "am_", "bf_", "bm_")


# --- Model holders --------------------------------------------------------

class ModelHolder:
    """Single process state: Kokoro instance + LRU of RVCWrappers."""

    def __init__(
        self,
        kokoro_model: Path,
        kokoro_voices: Path,
        manifest_path: Path,
        device: str = "cuda:0",
        lru_cap: int = 3,
    ):
        self.device = device
        self.lru_cap = lru_cap

        log.info("loading Kokoro from %s", kokoro_model)
        t = time.monotonic()
        self.kokoro = Kokoro(str(kokoro_model), str(kokoro_voices))
        log.info("kokoro ready in %.1fs (%d voices)",
                 time.monotonic() - t, len(self.kokoro.get_voices()))

        self.manifest = {m["label"]: m for m in json.loads(manifest_path.read_text())}
        log.info("rvc manifest: %d voices", len(self.manifest))

        self.rvc_cache: OrderedDict[str, RVCWrapper] = OrderedDict()
        # Track the version that worked per-label (v2 vs v1 fallback).
        self.version_hint: dict[str, str] = {}

    # English voice list for the picker.
    def english_voices(self) -> list[str]:
        return sorted(
            v for v in self.kokoro.get_voices()
            if v.startswith(ENGLISH_PREFIXES)
        )

    def rvc_list(self) -> list[dict]:
        return [
            {
                "label": lbl,
                "repo": m.get("repo", ""),
                "has_index": bool(m.get("index")),
                "version": self.version_hint.get(lbl),
                "loaded": lbl in self.rvc_cache,
            }
            for lbl, m in self.manifest.items()
        ]

    def get_rvc(self, label: str) -> RVCWrapper:
        """Load on demand, cache LRU. Raises KeyError if label unknown."""
        if label not in self.manifest:
            raise KeyError(label)
        if label in self.rvc_cache:
            self.rvc_cache.move_to_end(label)
            return self.rvc_cache[label]
        m = self.manifest[label]
        pth = Path(m["pth"])
        idx = m.get("index") or ""
        log.info("loading RVC: %s", label)
        wrap = RVCWrapper(
            model_path=pth, index_path=Path(idx) if idx else Path(""),
            models_dir=RVC_LIBRARY, device=self.device,
        )
        # RVCWrapper.__init__ wraps index_path in Path() — but Path("") becomes
        # "." which rvc-python rejects. Force back to a raw empty string so
        # load_model gets the empty-index sentinel it expects.
        if not idx:
            wrap.index_path = ""  # type: ignore[assignment]
        # rvc_service.RVCWrapper.load() loads via load_model(version='v2').
        # On state_dict mismatch try v1.
        try:
            wrap.load()
            self.version_hint[label] = "v2"
        except Exception as exc:  # noqa: BLE001
            if "state_dict" not in str(exc):
                raise
            log.warning("v2 state_dict mismatch for %s, retrying v1", label)
            # Reload with version override (rvc_service.RVCWrapper hardcodes v2;
            # bypass by calling RVCInference directly).
            from rvc_python.infer import RVCInference
            wrap._rvc = RVCInference(models_dir=str(RVC_LIBRARY), device=self.device)
            wrap._rvc.load_model(
                model_path_or_name=str(pth),
                index_path=idx or "",
                version="v1",
            )
            wrap._rvc.set_params(
                f0method=os.environ.get("RVC_F0_METHOD", "rmvpe"),
                index_rate=float(os.environ.get("RVC_INDEX_RATE", "0.5")),
                f0up_key=0,
            )
            self.version_hint[label] = "v1"

        # Evict LRU if at cap.
        if len(self.rvc_cache) >= self.lru_cap:
            evict_lbl, _ = self.rvc_cache.popitem(last=False)
            log.info("evicting RVC: %s", evict_lbl)
        self.rvc_cache[label] = wrap
        return wrap


HOLDER: Optional[ModelHolder] = None


# --- HTTP app -------------------------------------------------------------

app = FastAPI()


@app.middleware("http")
async def cors(request: Request, call_next):
    if request.method == "OPTIONS":
        from fastapi.responses import Response
        r = Response(status_code=204)
    else:
        r = await call_next(request)
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    # CORS hides custom response headers from JS unless explicitly exposed.
    # Without this, fetch().headers.get("X-Sample-Rate") returns null → JS
    # falls back to its default SR → audio plays at the wrong speed.
    r.headers["Access-Control-Expose-Headers"] = "X-Sample-Rate"
    return r


@app.get("/health")
async def health():
    assert HOLDER is not None
    return {
        "ready": True,
        "device": HOLDER.device,
        "kokoro_voices": len(HOLDER.kokoro.get_voices()),
        "rvc_voices": len(HOLDER.manifest),
        "rvc_loaded": list(HOLDER.rvc_cache.keys()),
    }


@app.get("/voices")
async def voices():
    assert HOLDER is not None
    return {
        "tts": HOLDER.english_voices(),
        "rvc": HOLDER.rvc_list(),
    }


def _frame(pcm_bytes: bytes) -> bytes:
    """Length-prefixed frame: 4-byte u32 LE + payload."""
    return struct.pack("<I", len(pcm_bytes)) + pcm_bytes


# Sentence splitter: keep the terminator with the sentence, drop empties.
_SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+|$)", re.DOTALL)


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_RE.findall(text)]
    parts = [p for p in parts if p]
    return parts or [text]


@app.post("/stream")
async def stream(request: Request):
    assert HOLDER is not None
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    tts_voice = payload.get("tts_voice") or "bf_emma"
    rvc_label = payload.get("rvc_label") or None
    pitch = int(payload.get("pitch", 0))
    lang = payload.get("lang") or ("en-us" if tts_voice.startswith("a") else "en-gb")

    if not text:
        raise HTTPException(400, "empty text")
    if rvc_label and rvc_label not in HOLDER.manifest:
        raise HTTPException(404, f"unknown rvc_label {rvc_label}")

    # Pre-load RVC so we don't pay the load cost mid-stream.
    rvc_wrap: Optional[RVCWrapper] = None
    if rvc_label:
        loop = asyncio.get_running_loop()
        rvc_wrap = await loop.run_in_executor(None, HOLDER.get_rvc, rvc_label)

    if rvc_wrap is not None:
        # Each RVC model has its own native output SR (32k/40k/48k) — read it
        # off the loaded model and tell the browser to play at that rate.
        out_sr = int(rvc_wrap._rvc.vc.tgt_sr)
    else:
        out_sr = KOKORO_SR

    async def producer():
        """Pipelined generator: Kokoro → RVC → frames."""
        loop = asyncio.get_running_loop()
        # Queue between Kokoro task and RVC task (bounded so kokoro doesn't
        # race too far ahead — RVC is the slower stage).
        tts_q: asyncio.Queue[Optional[np.ndarray]] = asyncio.Queue(maxsize=4)

        t_start = time.monotonic()
        log.info("stream start: voice=%s rvc=%s pitch=%d len(text)=%d",
                 tts_voice, rvc_label, pitch, len(text))

        # Pre-split into sentences so each one becomes its own pipeline chunk.
        # Kokoro's create_stream alone won't sub-chunk under 510 phonemes, so
        # for short utterances we'd otherwise wait for the whole thing.
        sentences = _split_sentences(text)

        async def kokoro_task():
            try:
                for sent in sentences:
                    async for chunk_f32, sr in HOLDER.kokoro.create_stream(
                        sent, voice=tts_voice, lang=lang
                    ):
                        pcm_i16 = (np.clip(chunk_f32, -1, 1) * 32767).astype(np.int16)
                        await tts_q.put(pcm_i16)
            except Exception:  # noqa: BLE001
                log.exception("kokoro task failed")
                raise
            finally:
                await tts_q.put(None)

        kt = asyncio.create_task(kokoro_task())

        chunk_idx = 0
        first_audio_ms = None
        try:
            while True:
                chunk = await tts_q.get()
                if chunk is None:
                    break
                if rvc_wrap is not None:
                    # Run RVC in thread executor (sync/blocking, GPU-bound).
                    out_pcm_bytes = await loop.run_in_executor(
                        None,
                        rvc_wrap.infer_pcm,
                        chunk.tobytes(), KOKORO_SR, pitch,
                    )
                else:
                    out_pcm_bytes = chunk.tobytes()
                if first_audio_ms is None:
                    first_audio_ms = int((time.monotonic() - t_start) * 1000)
                    log.info("  TTFA: %dms", first_audio_ms)
                chunk_idx += 1
                yield _frame(out_pcm_bytes)
        finally:
            kt.cancel()
            with __import__("contextlib").suppress(Exception):
                await kt
            total_ms = int((time.monotonic() - t_start) * 1000)
            log.info("stream done: chunks=%d ttfa=%sms total=%dms",
                     chunk_idx, first_audio_ms, total_ms)

    headers = {
        "X-Sample-Rate": str(out_sr),
        "Cache-Control": "no-store",
    }
    return StreamingResponse(
        producer(), media_type="application/octet-stream", headers=headers,
    )


# --- main -----------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kokoro-model",
                        default=str(REPO / "demo/voices/kokoro/kokoro-v1.0.onnx"))
    parser.add_argument("--kokoro-voices",
                        default=str(REPO / "demo/voices/kokoro/voices-v1.0.bin"))
    parser.add_argument("--manifest",
                        default=str(REPO / "demo/voices/rvc/manifest.json"))
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--lru-cap", type=int, default=3)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=12103)
    parser.add_argument("--ssl-keyfile", default=str(REPO / "demo/dev.key"))
    parser.add_argument("--ssl-certfile", default=str(REPO / "demo/dev.crt"))
    args = parser.parse_args()

    global HOLDER
    HOLDER = ModelHolder(
        kokoro_model=Path(args.kokoro_model),
        kokoro_voices=Path(args.kokoro_voices),
        manifest_path=Path(args.manifest),
        device=args.device,
        lru_cap=args.lru_cap,
    )
    uvicorn.run(
        app, host=args.host, port=args.port, log_level="warning",
        ssl_keyfile=args.ssl_keyfile if Path(args.ssl_keyfile).exists() else None,
        ssl_certfile=args.ssl_certfile if Path(args.ssl_certfile).exists() else None,
    )


if __name__ == "__main__":
    main()
