"""Combined Kokoro TTS + RVC voice conversion sidecar.

Holds both model stacks in one process so PCM never leaves the heap between
Kokoro and RVC. Streaming splits the text into phrase-sized chunks (sentence
boundaries, plus mid-sentence at commas/semicolons/colons for long sentences),
synthesizes each chunk with a single RVC call, and emits one frame per chunk.

Why per-chunk (not overlap-and-crop): rvc-python's `vc_single` adds ~3 s of
internal padding around each call, and HuBERT/rmvpe are context-sensitive
against that padding. The same input region under different window lengths
produces different output, which no outer crossfade can hide. Splitting at
prosodic boundaries lands seams on low-energy frames where any mismatch is
masked, and Kokoro→RVC is pipelined so the next chunk's Kokoro runs while
the current chunk's RVC is still on the GPU.

POST /stream
    body: {text, tts_voice, rvc_label?, pitch?, speed?, lang?}
    response: `[u32 LE n_bytes][u32 LE head_overlap_samples]
               [u32 LE tail_overlap_samples][int16 PCM bytes]` frames
    headers: Content-Type=application/octet-stream, X-Sample-Rate=<rvc_sr>,
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
import librosa
import onnxruntime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import uvicorn

# Import RVCWrapper from the existing sidecar to avoid code duplication.
sys.path.insert(0, str(Path(__file__).parent))
from rvc_service import RVCWrapper  # noqa: E402

from kokoro_onnx import Kokoro  # noqa: E402

# Monkey-patch load_audio everywhere it's imported so in-memory (orig_sr, audio)
# tuples work. The stock load_audio calls .strip() before the try/except, so
# tuples crash.
from rvc_python.lib import audio as _rvc_audio  # noqa: E402
from rvc_python.modules.vc import modules as _rvc_vc_modules  # noqa: E402
_orig_load_audio = _rvc_audio.load_audio
def _patched_load_audio(file, sr):
    if isinstance(file, tuple):
        audio = file[1] / 32768.0
        if len(audio.shape) == 2:
            audio = np.mean(audio, -1)
        return librosa.resample(audio, orig_sr=file[0], target_sr=16000)
    return _orig_load_audio(file, sr)
_rvc_audio.load_audio = _patched_load_audio
_rvc_vc_modules.load_audio = _patched_load_audio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-rvc")

REPO = Path(__file__).resolve().parents[1]
RVC_LIBRARY = REPO / "demo/voices/rvc"
KOKORO_SR = 24_000

# Phrase splitting. The first chunk drives TTFA, so we cap it tight; later
# chunks can be longer to reduce the number of seams in the body of the
# utterance.
FIRST_CHUNK_MAX_CHARS = 40
BODY_CHUNK_MAX_CHARS  = 120
# 1ms linear ramp at each chunk's edges to suppress sample-boundary clicks
# when adjacent frames are butt-joined in the browser.
EDGE_FADE_MS = 1
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

        log.info("loading Kokoro from %s (device=%s)", kokoro_model, self.device)
        t = time.monotonic()
        if self.device.startswith("cuda"):
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        session = onnxruntime.InferenceSession(str(kokoro_model), providers=providers)
        self.kokoro = Kokoro.from_session(session, str(kokoro_voices))
        log.info("kokoro ready in %.1fs (%d voices), onnx provider=%s",
                 time.monotonic() - t, len(self.kokoro.get_voices()), providers[0])

        raw = json.loads(manifest_path.read_text())
        # Manifest entries mix absolute paths and repo-relative paths like
        # "demo/voices/rvc/library/<label>/model.pth". Resolve the relative
        # ones against REPO so the sidecar works regardless of CWD.
        for m in raw:
            for k in ("pth", "index"):
                v = m.get(k)
                if v and not Path(v).is_absolute():
                    m[k] = str((REPO / v).resolve())
        self.manifest = {m["label"]: m for m in raw}
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

        # Evict LRU if at cap. Free the underlying CUDA tensors so VRAM is
        # actually released (Python ref-drop alone doesn't return the blocks
        # to the allocator until empty_cache runs).
        while len(self.rvc_cache) >= self.lru_cap:
            evict_lbl, evicted = self.rvc_cache.popitem(last=False)
            log.info("evicting RVC: %s", evict_lbl)
            del evicted
            try:
                import torch  # noqa: PLC0415
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
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


def _frame(pcm_bytes: bytes, head_overlap: int = 0, tail_overlap: int = 0) -> bytes:
    """12-byte header + payload: u32 n_bytes, u32 head_overlap, u32 tail_overlap.

    head_overlap is the number of output samples at the START of this frame
    that should be linearly faded in (matched by the PREVIOUS frame's
    fade-out tail). tail_overlap is the symmetric fade-out at the END,
    matched by the NEXT frame's fade-in. Both 0 marks an isolated frame.
    """
    return struct.pack("<III", len(pcm_bytes), head_overlap, tail_overlap) + pcm_bytes


# Sentence splitter: keep the terminator with the sentence, drop empties.
_SENTENCE_RE = re.compile(r"[^.!?]+(?:[.!?]+|$)", re.DOTALL)
# Sub-sentence splitter: comma/semicolon/colon/em-dash, terminator kept on
# the left side so each piece still reads naturally.
_SUBPHRASE_RE = re.compile(r"[^,;:—]+(?:[,;:—]+|$)", re.DOTALL)


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_RE.findall(text)]
    parts = [p for p in parts if p]
    return parts or [text]


def _split_chunks(text: str) -> list[str]:
    """Sentence-first, then comma-split long sentences.

    The first chunk is bounded by FIRST_CHUNK_MAX_CHARS so TTFA is low; the
    rest are bounded by BODY_CHUNK_MAX_CHARS so we keep the number of seams
    small. Sub-phrase splits land on commas/semicolons/colons/em-dashes,
    where RVC seam mismatches are masked by the natural prosodic pause.
    """
    chunks: list[str] = []
    for sent in _split_sentences(text):
        max_chars = FIRST_CHUNK_MAX_CHARS if not chunks else BODY_CHUNK_MAX_CHARS
        if len(sent) <= max_chars:
            chunks.append(sent)
            continue
        # Greedy pack sub-phrases up to max_chars; first one stays tight.
        buf = ""
        for part in (p.strip() for p in _SUBPHRASE_RE.findall(sent)):
            if not part:
                continue
            cap = FIRST_CHUNK_MAX_CHARS if not chunks else BODY_CHUNK_MAX_CHARS
            if buf and len(buf) + 1 + len(part) > cap:
                chunks.append(buf)
                buf = part
            else:
                buf = f"{buf} {part}" if buf else part
        if buf:
            chunks.append(buf)
    return chunks or [text]


def _apply_edge_fade(pcm_bytes: bytes, n_samples: int) -> bytes:
    """Linear fade-in on first n samples and fade-out on last n samples.

    Pops at the sample-level discontinuity between butt-joined RVC outputs
    are eliminated by ramping each chunk's edges to/from zero — 1ms is
    inaudible as an envelope but enough to kill the click.
    """
    arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    n = min(n_samples, len(arr) // 2)
    if n <= 0:
        return pcm_bytes
    arr = arr.copy()
    ramp = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
    arr[:n]  = (arr[:n].astype(np.float32) * ramp).astype(np.int16)
    arr[-n:] = (arr[-n:].astype(np.float32) * ramp[::-1]).astype(np.int16)
    return arr.tobytes()


@app.post("/stream")
async def stream(request: Request):
    assert HOLDER is not None
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    tts_voice = payload.get("tts_voice") or "bf_emma"
    rvc_label = payload.get("rvc_label") or None
    pitch = int(payload.get("pitch", 0))
    speed = float(payload.get("speed", 1.0))
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

    running_sr = out_sr  # outer scope; updated by producer

    async def producer():
        """Per-chunk pipeline: Kokoro chunk N+1 runs while RVC chunk N runs."""
        nonlocal running_sr
        loop = asyncio.get_running_loop()
        # Bounded queue: Kokoro may race up to a few chunks ahead of RVC so
        # the GPU stays busy, but not unboundedly (~10 MB per chunk).
        chunk_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=4)

        t_start = time.monotonic()
        chunks = _split_chunks(text)
        log.info("stream start: voice=%s rvc=%s pitch=%d speed=%.2f "
                 "len(text)=%d n_chunks=%d first=%r",
                 tts_voice, rvc_label, pitch, speed, len(text),
                 len(chunks), chunks[0][:60])

        async def kokoro_task():
            try:
                for chunk_text in chunks:
                    parts: list[np.ndarray] = []
                    async for chunk_f32, _sr in HOLDER.kokoro.create_stream(
                        chunk_text, voice=tts_voice, lang=lang, speed=speed
                    ):
                        parts.append((np.clip(chunk_f32, -1, 1) * 32767).astype(np.int16))
                    if parts:
                        await chunk_q.put(np.concatenate(parts).tobytes())
            except Exception:  # noqa: BLE001
                log.exception("kokoro task failed")
                raise
            finally:
                await chunk_q.put(None)

        kt = asyncio.create_task(kokoro_task())
        chunk_idx = 0
        first_audio_ms = None
        edge_fade_samples = 0  # set once after first RVC call (depends on rvc_sr)

        try:
            while True:
                pcm_bytes = await chunk_q.get()
                if pcm_bytes is None:
                    break

                if rvc_wrap is not None:
                    out_bytes, rvc_sr = await loop.run_in_executor(
                        None, rvc_wrap.infer_pcm_direct,
                        pcm_bytes, KOKORO_SR, pitch,
                    )
                    running_sr = rvc_sr
                else:
                    out_bytes = pcm_bytes
                    running_sr = KOKORO_SR

                if edge_fade_samples == 0:
                    edge_fade_samples = max(1, running_sr * EDGE_FADE_MS // 1000)
                out_bytes = _apply_edge_fade(out_bytes, edge_fade_samples)

                if first_audio_ms is None:
                    first_audio_ms = int((time.monotonic() - t_start) * 1000)
                    log.info("  TTFA: %dms (chunk_in=%dms → out=%dms @ %dHz)",
                             first_audio_ms,
                             (len(pcm_bytes) // 2) * 1000 // KOKORO_SR,
                             (len(out_bytes) // 2) * 1000 // max(running_sr, 1),
                             running_sr)

                chunk_idx += 1
                yield _frame(out_bytes, 0, 0)
        finally:
            kt.cancel()
            with __import__("contextlib").suppress(Exception):
                await kt
            total_ms = int((time.monotonic() - t_start) * 1000)
            log.info("stream done: chunks=%d ttfa=%sms total=%dms sr=%s",
                     chunk_idx, first_audio_ms, total_ms, running_sr)

    headers = {
        "X-Sample-Rate": str(running_sr),
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
    parser.add_argument("--lru-cap", type=int, default=1)
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

    # Warm up: realistic inferences to compile CUDA kernels for the SHAPES
    # the streaming path actually uses (CORE+TRAIL and LEAD+CORE+TRAIL), so
    # the first user request doesn't pay JIT cost. Warm Kokoro too — first
    # ONNX session.run() is otherwise ~1s of cold start.
    if HOLDER.manifest:
        first_label = next(iter(HOLDER.manifest))
        log.info("warming up Kokoro + RVC (%s)", first_label)
        try:
            wrap = HOLDER.get_rvc(first_label)
            import asyncio  # noqa: PLC0415
            async def _warm_kokoro():
                gen = HOLDER.kokoro.create_stream(
                    "Hello, this is a warm up.", voice="bf_emma", lang="en-gb"
                )
                pcm = b""
                async for chunk_f32, _sr in gen:
                    pcm_i16 = (np.clip(chunk_f32, -1, 1) * 32767).astype(np.int16)
                    pcm += pcm_i16.tobytes()
                return pcm
            kt0 = time.monotonic()
            warmup_pcm = asyncio.run(_warm_kokoro())
            log.info("  kokoro warm: %d samples in %dms",
                     len(warmup_pcm) // 2, int((time.monotonic() - kt0) * 1000))
            # Warm RVC at two realistic shapes: a short first-chunk
            # ("Hello world,") and a longer body chunk. Helps JIT both
            # tensor shape paths the producer will hit.
            for label, nbytes in [("first ~1s", KOKORO_SR * 1 * 2),
                                  ("body ~3s",  KOKORO_SR * 3 * 2)]:
                slab = warmup_pcm[:nbytes] if len(warmup_pcm) >= nbytes else warmup_pcm
                t0 = time.monotonic()
                out, out_sr = wrap.infer_pcm_direct(slab, KOKORO_SR, 0)
                log.info("  rvc warm %-10s: in=%dB out=%dB @ %dHz in %dms",
                         label, len(slab), len(out), out_sr,
                         int((time.monotonic() - t0) * 1000))
            log.info("warm-up done")
        except Exception as exc:  # noqa: BLE001
            log.warning("warm-up failed (non-fatal): %s", exc)

    uvicorn.run(
        app, host=args.host, port=args.port, log_level="warning",
        ssl_keyfile=args.ssl_keyfile if Path(args.ssl_keyfile).exists() else None,
        ssl_certfile=args.ssl_certfile if Path(args.ssl_certfile).exists() else None,
    )


if __name__ == "__main__":
    main()
