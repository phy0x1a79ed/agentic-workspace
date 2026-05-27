"""Standalone RVC inference HTTP service.

Lives in the `awm-rvc` mamba env (Python 3.10 + CUDA torch + rvc-python).
The main demo (`awm` env, Python 3.14) cannot import these deps directly,
so it talks to this process over loopback HTTP.

POST /infer
    body: raw mono int16 PCM at SOURCE_SR (default 22050)
    query: pitch (semitone shift, default 0)
    response body: raw mono int16 PCM at 48000 Hz
    response header: X-Sample-Rate: 48000

GET /health → {"ready": true, "model": "..."}

Run:
    mamba run -n awm-rvc python demo/rvc_service.py \
        --model demo/voices/rvc/Chelly_Egoist.pth \
        --index demo/voices/rvc/Chelly_Egoist.index \
        --port 7831
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rvc-service")

SOURCE_SR = int(os.environ.get("RVC_SOURCE_SR", "22050"))
OUTPUT_SR = 48000


class RVCWrapper:
    def __init__(self, model_path: Path, index_path: Path, models_dir: Path, device: str):
        self.model_path = Path(model_path)
        self.index_path = Path(index_path)
        self.models_dir = Path(models_dir)
        self.device = device
        self._rvc = None

    def load(self):
        from rvc_python.infer import RVCInference

        log.info("loading RVC on %s (model=%s)", self.device, self.model_path.name)
        t0 = time.monotonic()
        self._rvc = RVCInference(models_dir=str(self.models_dir), device=self.device)
        self._rvc.load_model(
            model_path_or_name=str(self.model_path),
            index_path=str(self.index_path),
            version="v2",
        )
        # rmvpe runs on GPU (much faster than the default `harvest`/pyworld).
        # index_rate=0 disables FAISS lookup — small accent tradeoff for ~30% speedup.
        f0_method = os.environ.get("RVC_F0_METHOD", "rmvpe")
        index_rate = float(os.environ.get("RVC_INDEX_RATE", "0.5"))
        self._rvc.set_params(f0method=f0_method, index_rate=index_rate)
        log.info("RVC ready in %.1fs (f0=%s, index_rate=%.2f)",
                 time.monotonic() - t0, f0_method, index_rate)

    def infer_pcm(self, pcm: bytes, source_sr: int, pitch: int = 0) -> bytes:
        """Run PCM through RVC via file-based temp path. Returns int16 PCM at OUTPUT_SR."""
        if self._rvc is None:
            raise RuntimeError("model not loaded")
        with tempfile.TemporaryDirectory(dir="/dev/shm" if Path("/dev/shm").exists() else None) as td:
            in_path = Path(td) / "in.wav"
            out_path = Path(td) / "out.wav"
            with wave.open(str(in_path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(source_sr)
                w.writeframes(pcm)
            self._rvc.set_params(f0up_key=pitch)
            self._rvc.infer_file(str(in_path), str(out_path))
            with wave.open(str(out_path), "rb") as r:
                if r.getframerate() != OUTPUT_SR:
                    log.warning("unexpected output sr %d", r.getframerate())
                frames = r.readframes(r.getnframes())
                return frames

    def infer_pcm_direct(self, pcm: bytes, source_sr: int, pitch: int = 0) -> tuple[bytes, int]:
        """Run PCM through RVC in-memory — no WAV file I/O.

        Returns (int16 PCM bytes at model's native output SR, output sample rate).

        Reseeds torch's global RNG before each call. Some op inside rvc-python's
        pipeline reads from torch.default_generator (~22% nrms run-to-run jitter
        without this), and the jitter is large enough that the streaming
        sliding-window stitcher cannot crossfade across it.
        """
        if self._rvc is None:
            raise RuntimeError("model not loaded")
        import torch  # noqa: PLC0415
        torch.manual_seed(0)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(0)

        # Use the model's index file (if any) from the model info dict.
        model_info = self._rvc.models.get(self._rvc.current_model or "", {})
        file_index = model_info.get("index", "")

        audio_int16 = np.frombuffer(pcm, dtype=np.int16)

        self._rvc.set_params(f0up_key=pitch)

        result = self._rvc.vc.vc_single(
            sid=0,
            input_audio_path=(source_sr, audio_int16),
            f0_up_key=self._rvc.f0up_key,
            f0_method=self._rvc.f0method,
            file_index=file_index,
            file_index2="",
            index_rate=self._rvc.index_rate,
            filter_radius=self._rvc.filter_radius,
            resample_sr=self._rvc.resample_sr,
            rms_mix_rate=self._rvc.rms_mix_rate,
            protect=self._rvc.protect,
            f0_file="",
        )

        # vc_single returns a numpy int16 array on success, or
        # (error_string, (None, None)) on failure.
        if isinstance(result, tuple):
            err_msg = result[0] if result[0] else "unknown rvc error"
            raise RuntimeError(f"vc_single failed: {err_msg}")

        out_sr = int(self._rvc.vc.tgt_sr)
        return result.tobytes(), out_sr


app = FastAPI()
_rvc: RVCWrapper | None = None


@app.get("/health")
async def health():
    return {
        "ready": _rvc is not None and _rvc._rvc is not None,
        "model": _rvc.model_path.name if _rvc else None,
        "device": _rvc.device if _rvc else None,
        "output_sample_rate": OUTPUT_SR,
    }


@app.post("/infer")
async def infer(request: Request):
    if _rvc is None:
        raise HTTPException(503, "not loaded")
    body = await request.body()
    if not body:
        return Response(content=b"", media_type="application/octet-stream")
    try:
        source_sr = int(request.query_params.get("sr", SOURCE_SR))
        pitch = int(request.query_params.get("pitch", "0"))
    except ValueError:
        raise HTTPException(400, "bad sr/pitch")
    t0 = time.monotonic()
    out = _rvc.infer_pcm(body, source_sr, pitch)
    log.info(
        "infer: %d→%d bytes in %dms (pitch=%+d)",
        len(body), len(out), int((time.monotonic() - t0) * 1000), pitch,
    )
    return Response(
        content=out,
        media_type="application/octet-stream",
        headers={"X-Sample-Rate": str(OUTPUT_SR)},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--index", default="")
    parser.add_argument("--models-dir", default="demo/voices/rvc")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7831)
    args = parser.parse_args()

    global _rvc
    _rvc = RVCWrapper(
        model_path=Path(args.model),
        index_path=Path(args.index),
        models_dir=Path(args.models_dir),
        device=args.device,
    )
    _rvc.load()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
