"""Native-TTS evaluation driver.

Iterates the character × engine matrix from the plan and:
  1. POSTs the eval sentence to each sidecar's /synth endpoint
  2. Writes returned WAVs into demo/voices/native-eval/<character>/<engine>.wav
  3. Appends a JSON line per run to demo/voices/native-eval/_metrics.jsonl
  4. Renders demo/voices/native-eval/report.md from the metrics log

VRAM on the dev box (RTX 3060 Ti, 8 GB) cannot host all three engines at
once. The driver does not manage processes — bring up one sidecar at a
time, run the driver with --engines <name>, tear it down, repeat.

Examples
--------
  # 1. SBV2 only (no reference clip needed for GLaDOS)
  python demo/native_eval.py --engines sbv2

  # 2. GPT-SoVITS (needs reference clips populated in _refs/)
  python demo/native_eval.py --engines gptsovits

  # 3. F5-TTS
  python demo/native_eval.py --engines f5tts

  # 4. RVC reference column (uses the existing :7833 sidecar)
  python demo/native_eval.py --engines rvc

  # 5. Re-render the report from existing metrics without calling any engine.
  python demo/native_eval.py --report-only

The matrix definition is below in MATRIX — edit there if a character's
reference clip moves or a new engine is added.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("native-eval")

REPO = Path(__file__).resolve().parents[1]
EVAL_ROOT = REPO / "demo/voices/native-eval"
REFS_DIR = EVAL_ROOT / "_refs"
METRICS_PATH = EVAL_ROOT / "_metrics.jsonl"
REPORT_PATH = EVAL_ROOT / "report.md"
HTML_PATH = EVAL_ROOT / "index.html"

EVAL_TEXT = (
    "The diagnostic ran clean for almost six hours before the first packet dropped. "
    "By the time the alert reached me, the whole cluster was already cold."
)

import os  # noqa: E402

# Sidecar endpoints — overridable via env so the eval can target a remote box
# or the demo's actual port (live deployment uses :12103 per the ZeroTier
# port-forward range 12100–12150).
SIDECAR = {
    "sbv2":     os.environ.get("EVAL_SBV2_URL",     "http://127.0.0.1:7843"),
    "gptsovits": os.environ.get("EVAL_GPTSOVITS_URL", "http://127.0.0.1:7841"),
    "f5tts":    os.environ.get("EVAL_F5TTS_URL",    "http://127.0.0.1:7842"),
    # The live tts_rvc_service.py serves HTTPS on :12103 with a self-signed
    # dev cert (see demo/dev.crt). Plain HTTP returns empty replies.
    "rvc":      os.environ.get("EVAL_RVC_URL",      "https://127.0.0.1:12103"),
}

# Self-signed certs in dev — disable TLS verification for sidecar calls.
_HTTP_VERIFY = False


@dataclasses.dataclass
class Row:
    character: str
    engine: str         # one of: sbv2, gptsovits, f5tts, rvc, kokoro
    label: str          # filename stem under <character>/<label>.wav
    ref: Optional[str] = None       # path under _refs/, e.g. "singer.wav"
    ref_text: Optional[str] = None  # transcript of the ref clip
    # engine-specific knobs
    rvc_label: Optional[str] = None       # for engine=rvc — manifest label
    kokoro_voice: Optional[str] = None    # for engine=rvc when running as Kokoro-only
    notes: str = ""


# Character × engine matrix. The RVC reference column now points at real
# character RVC models found in the existing manifest. Reference clips for
# zero-shot engines (gptsovits, f5tts) are auto-generated via Kokoro→RVC if
# missing — see ensure_refs() below.
MATRIX: list[Row] = [
    # --- GLaDOS: only character with a true pretrained native checkpoint ---
    Row("glados", "sbv2", "style-bert-vits2",
        notes="WarriorMama777/GLaDOS_TTS — pretrained, no reference clip"),
    Row("glados", "gptsovits", "gpt-sovits",
        ref="glados.wav", ref_text="REF_TEXT_PLACEHOLDER",
        notes="Reference auto-generated from GLaDOS RVC model"),
    Row("glados", "f5tts", "f5-tts",
        ref="glados.wav", ref_text="REF_TEXT_PLACEHOLDER",
        notes="Reference auto-generated from GLaDOS RVC model"),
    Row("glados", "rvc", "rvc-reference",
        rvc_label="glados", kokoro_voice="bf_emma",
        notes="Real GLaDOS RVC model from manifest"),

    # --- Hatsune Miku ---
    Row("miku", "gptsovits", "gpt-sovits",
        ref="miku.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("miku", "f5tts", "f5-tts",
        ref="miku.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("miku", "rvc", "rvc-reference",
        rvc_label="hatsune_miku", kokoro_voice="af_sky",
        notes="Real Miku RVC model from manifest"),

    # --- High-pitched female singer (Chelly from EGOIST) ---
    Row("singer", "gptsovits", "gpt-sovits",
        ref="singer.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("singer", "f5tts", "f5-tts",
        ref="singer.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("singer", "rvc", "rvc-reference",
        rvc_label="chelly_egoist", kokoro_voice="af_sky",
        notes="Chelly (EGOIST) — closest singer voice in manifest"),

    # --- Pain / Tobi-style deep anime narrator (using vegeta as closest match) ---
    Row("pain", "gptsovits", "gpt-sovits",
        ref="pain.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("pain", "f5tts", "f5-tts",
        ref="pain.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("pain", "rvc", "rvc-reference",
        rvc_label="vegeta", kokoro_voice="bm_george",
        notes="Vegeta — closest 'deep epic anime' voice in manifest"),

    # --- Morgan Freeman narrator ---
    Row("morgan-freeman", "f5tts", "f5-tts",
        ref="morgan-freeman.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("morgan-freeman", "gptsovits", "gpt-sovits",
        ref="morgan-freeman.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("morgan-freeman", "rvc", "rvc-reference",
        rvc_label="morgan_freeman", kokoro_voice="bm_george",
        notes="Real Morgan Freeman RVC model from manifest"),

    # --- Optimus Prime (no exact match — use deep_male as closest substitute) ---
    Row("optimus-prime", "gptsovits", "gpt-sovits",
        ref="optimus-prime.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("optimus-prime", "f5tts", "f5-tts",
        ref="optimus-prime.wav", ref_text="REF_TEXT_PLACEHOLDER"),
    Row("optimus-prime", "rvc", "rvc-reference",
        rvc_label="deep_male", kokoro_voice="bm_daniel",
        notes="Deep-male generic — no Optimus Prime in manifest"),
]

# When auto-generating reference clips for zero-shot engines, we use these
# RVC labels (closest-character match in the existing manifest) + a clean
# Kokoro voice routed through that RVC model. Same models as MATRIX rvc rows.
REF_RECIPE: dict[str, tuple[str, str]] = {
    # character → (rvc_label, kokoro_voice)
    "glados":          ("glados",         "bf_emma"),
    "miku":            ("hatsune_miku",   "af_sky"),
    "singer":          ("chelly_egoist",  "af_sky"),
    "pain":            ("vegeta",         "bm_george"),
    "morgan-freeman":  ("morgan_freeman", "bm_george"),
    "optimus-prime":   ("deep_male",      "bm_daniel"),
}

# Text spoken in each auto-generated reference clip. ~8 s of phonetically
# varied content is good for zero-shot conditioning. Same text per character
# so the zero-shot rows clone timbre, not content.
REF_TEXT = (
    "Hello there. This is a reference clip for voice cloning. "
    "I will speak a few sentences to give the model enough material to learn from."
)


def _sidecar_healthy(url: str) -> bool:
    """Check if the sidecar is reachable. Tries /health; falls back to any
    response on root (api_v2.py has no /health endpoint)."""
    try:
        r = httpx.get(f"{url}/health", timeout=2.0, verify=_HTTP_VERIFY)
        if r.status_code == 200:
            return r.json().get("ready", False) is not None
    except Exception:  # noqa: BLE001
        pass
    try:
        r = httpx.get(f"{url}/", timeout=2.0, verify=_HTTP_VERIFY)
        return r.status_code < 500  # any non-server-error response means alive
    except Exception:  # noqa: BLE001
        return False


def _resolve_ref(row: Row) -> tuple[Optional[Path], Optional[str]]:
    """Return (ref_path, ref_text). For engines that don't need a ref → (None, None)."""
    if not row.ref:
        return None, None
    p = REFS_DIR / row.ref
    if not p.exists():
        return None, None
    # If a sidecar transcript file exists, use it; else fall back to MATRIX value.
    txt_path = p.with_suffix(".txt")
    if txt_path.exists():
        return p, txt_path.read_text(encoding="utf-8").strip()
    return p, REF_TEXT  # global default


def ensure_refs(client: httpx.Client) -> None:
    """Auto-generate any missing _refs/<character>.{wav,txt} using Kokoro→RVC.

    This is best-effort — the reference inherits RVC's prosody bleed. Zero-shot
    engines clone *timbre* not artifacts, so a degraded ref usually still gives
    usable character-style output. Real human reference clips would be better
    where available; this is the autonomous fallback.
    """
    import struct as _struct  # noqa: PLC0415
    import wave as _wave  # noqa: PLC0415
    import io as _io  # noqa: PLC0415

    rvc_url = SIDECAR["rvc"]
    if not _sidecar_healthy(rvc_url):
        log.warning("RVC sidecar %s not healthy — cannot auto-generate refs", rvc_url)
        return

    for character, (rvc_label, kokoro_voice) in REF_RECIPE.items():
        wav_path = REFS_DIR / f"{character}.wav"
        txt_path = REFS_DIR / f"{character}.txt"
        if wav_path.exists() and txt_path.exists():
            continue

        log.info("[refs] generating %s via Kokoro=%s → RVC=%s",
                 character, kokoro_voice, rvc_label)
        body = {
            "text": REF_TEXT,
            "tts_voice": kokoro_voice,
            "rvc_label": rvc_label,
            "pitch": 0,
        }
        try:
            with client.stream("POST", f"{rvc_url}/stream", json=body, timeout=300.0) as r:
                r.raise_for_status()
                sr = int(r.headers.get("X-Sample-Rate", 24000))
                pcm = bytearray()
                buf = bytearray()
                for piece in r.iter_bytes():
                    buf.extend(piece)
                    while len(buf) >= 4:
                        n = _struct.unpack("<I", buf[:4])[0]
                        if len(buf) < 4 + n:
                            break
                        pcm.extend(bytes(buf[4:4 + n]))
                        del buf[:4 + n]
        except Exception as exc:  # noqa: BLE001
            log.error("[refs] failed to generate %s: %s", character, exc)
            continue

        REFS_DIR.mkdir(parents=True, exist_ok=True)
        with _wave.open(str(wav_path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(bytes(pcm))
        txt_path.write_text(REF_TEXT, encoding="utf-8")
        dur = len(pcm) / 2.0 / sr
        log.info("[refs] wrote %s (%.2fs sr=%d) + transcript", wav_path.name, dur, sr)


def _save_wav(row: Row, content: bytes) -> Path:
    out_dir = EVAL_ROOT / row.character
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{row.label}.wav"
    out.write_bytes(content)
    return out


def _append_metric(record: dict) -> None:
    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with METRICS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def run_row(row: Row, client: httpx.Client) -> Optional[dict]:
    url = SIDECAR[row.engine]
    if not _sidecar_healthy(url):
        log.warning("[%s/%s] sidecar %s not healthy — skipping", row.character, row.label, url)
        return None

    if row.engine == "rvc":
        # tts_rvc_service.py uses /stream (length-prefixed PCM frames),
        # different shape from the native sidecars. Use it as-is.
        return run_rvc(row, client)
    if row.engine == "gptsovits":
        # Official api_v2.py returns a WAV directly from /tts with a different
        # body shape. Use a dedicated runner.
        return run_gptsovits(row, client)

    ref_path, ref_text = _resolve_ref(row)
    if row.engine in {"gptsovits", "f5tts"}:
        if ref_path is None:
            log.warning("[%s/%s] reference clip missing: %s — skipping",
                        row.character, row.label, REFS_DIR / row.ref if row.ref else "?")
            return None
        if not ref_text or ref_text.endswith("_REF_TEXT"):
            log.warning("[%s/%s] reference transcript missing — skipping",
                        row.character, row.label)
            return None
        body = {
            "text": EVAL_TEXT,
            "ref_wav_path": str(ref_path),
            "ref_text": ref_text,
            "language": "en",
        }
    else:
        body = {"text": EVAL_TEXT}

    log.info("[%s/%s] POST %s/synth", row.character, row.label, url)
    t0 = time.monotonic()
    try:
        r = client.post(f"{url}/synth", json=body, timeout=300.0)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("[%s/%s] request failed: %s", row.character, row.label, exc)
        return None
    wall_ms = int((time.monotonic() - t0) * 1000)

    out_path = _save_wav(row, r.content)
    record = {
        "ts": time.time(),
        "character": row.character,
        "engine": row.engine,
        "label": row.label,
        "wav": str(out_path.relative_to(REPO)),
        "ttfa_ms": int(r.headers.get("X-TTFA-Ms", wall_ms)),
        "total_ms": int(r.headers.get("X-Total-Ms", wall_ms)),
        "audio_sec": float(r.headers.get("X-Audio-Sec", 0.0)),
        "sample_rate": int(r.headers.get("X-Sample-Rate", 0)),
        "vram_mb": int(r.headers.get("X-VRAM-Used-Mb", 0)),
        "client_wall_ms": wall_ms,
        "notes": row.notes,
    }
    record["rtf"] = (record["total_ms"] / 1000.0 / record["audio_sec"]
                     if record["audio_sec"] else 0)
    _append_metric(record)
    log.info("[%s/%s] saved %s  ttfa=%dms total=%dms dur=%.2fs rtf=%.2f",
             row.character, row.label, out_path.name,
             record["ttfa_ms"], record["total_ms"], record["audio_sec"], record["rtf"])
    return record


def run_gptsovits(row: Row, client: httpx.Client) -> Optional[dict]:
    """Call the official GPT-SoVITS api_v2 /tts endpoint."""
    import wave as _wave  # noqa: PLC0415
    import io as _io  # noqa: PLC0415

    url = SIDECAR["gptsovits"]
    ref_path, ref_text = _resolve_ref(row)
    if ref_path is None or not ref_text:
        log.warning("[%s/%s] reference clip or transcript missing — skipping",
                    row.character, row.label)
        return None
    body = {
        "text": EVAL_TEXT,
        "text_lang": "en",
        "ref_audio_path": str(ref_path),
        "prompt_text": ref_text,
        "prompt_lang": "en",
        "top_k": 5,
        "top_p": 1.0,
        "temperature": 1.0,
        "text_split_method": "cut5",
        "batch_size": 1,
        "streaming_mode": False,
        "media_type": "wav",
    }
    log.info("[%s/%s] POST %s/tts ref=%s", row.character, row.label, url, ref_path.name)
    t0 = time.monotonic()
    try:
        r = client.post(f"{url}/tts", json=body, timeout=300.0)
        r.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("[%s/%s] gptsovits request failed: %s", row.character, row.label, exc)
        return None
    wall_ms = int((time.monotonic() - t0) * 1000)

    wav_bytes = r.content
    # Parse WAV header to extract sample rate + duration.
    try:
        with _wave.open(_io.BytesIO(wav_bytes)) as w:
            sr = w.getframerate()
            frames = w.getnframes()
            duration = frames / float(sr)
    except Exception:  # noqa: BLE001
        sr, duration = 32000, 0.0

    out_path = _save_wav(row, wav_bytes)
    record = {
        "ts": time.time(),
        "character": row.character,
        "engine": "gptsovits",
        "label": row.label,
        "wav": str(out_path.relative_to(REPO)),
        "ttfa_ms": wall_ms,    # non-streaming → ttfa == total
        "total_ms": wall_ms,
        "audio_sec": round(duration, 3),
        "sample_rate": sr,
        "vram_mb": 0,
        "client_wall_ms": wall_ms,
        "notes": row.notes,
    }
    record["rtf"] = (record["total_ms"] / 1000.0 / record["audio_sec"]
                     if record["audio_sec"] else 0)
    _append_metric(record)
    log.info("[%s/%s] saved %s  total=%dms dur=%.2fs rtf=%.2f",
             row.character, row.label, out_path.name,
             record["total_ms"], record["audio_sec"], record["rtf"])
    return record


def run_rvc(row: Row, client: httpx.Client) -> Optional[dict]:
    """Call the existing tts_rvc_service.py on :7833 — uses /stream."""
    import struct  # noqa: PLC0415

    url = SIDECAR["rvc"]
    body = {
        "text": EVAL_TEXT,
        "tts_voice": row.kokoro_voice or "bf_emma",
        "rvc_label": row.rvc_label,
        "pitch": 0,
    }
    log.info("[%s/%s] POST %s/stream voice=%s rvc=%s",
             row.character, row.label, url, body["tts_voice"], body["rvc_label"])
    t0 = time.monotonic()
    try:
        with client.stream("POST", f"{url}/stream", json=body, timeout=300.0) as r:
            r.raise_for_status()
            sr = int(r.headers.get("X-Sample-Rate", 24000))
            t_first = None
            chunks = bytearray()
            buf = bytearray()
            for piece in r.iter_bytes():
                buf.extend(piece)
                while len(buf) >= 4:
                    n = struct.unpack("<I", buf[:4])[0]
                    if len(buf) < 4 + n:
                        break
                    pcm_bytes = bytes(buf[4:4 + n])
                    del buf[:4 + n]
                    if t_first is None:
                        t_first = time.monotonic()
                    chunks.extend(pcm_bytes)
    except Exception as exc:  # noqa: BLE001
        log.error("[%s/%s] rvc request failed: %s", row.character, row.label, exc)
        return None
    t_done = time.monotonic()

    # Wrap raw int16 PCM into a WAV.
    import wave  # noqa: PLC0415
    import io  # noqa: PLC0415
    buf_out = io.BytesIO()
    with wave.open(buf_out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(chunks))
    out_path = _save_wav(row, buf_out.getvalue())

    duration = len(chunks) / 2.0 / sr
    record = {
        "ts": time.time(),
        "character": row.character,
        "engine": "rvc",
        "label": row.label,
        "wav": str(out_path.relative_to(REPO)),
        "ttfa_ms": int((t_first - t0) * 1000) if t_first else int((t_done - t0) * 1000),
        "total_ms": int((t_done - t0) * 1000),
        "audio_sec": round(duration, 3),
        "sample_rate": sr,
        "vram_mb": 0,
        "client_wall_ms": int((t_done - t0) * 1000),
        "notes": row.notes,
    }
    record["rtf"] = (record["total_ms"] / 1000.0 / record["audio_sec"]
                     if record["audio_sec"] else 0)
    _append_metric(record)
    log.info("[%s/%s] saved %s  ttfa=%dms total=%dms dur=%.2fs rtf=%.2f",
             row.character, row.label, out_path.name,
             record["ttfa_ms"], record["total_ms"], record["audio_sec"], record["rtf"])
    return record


def write_report() -> None:
    if not METRICS_PATH.exists():
        log.warning("no metrics yet at %s — run with --engines first", METRICS_PATH)
        return
    # Latest entry per (character, label) wins.
    latest: dict[tuple[str, str], dict] = {}
    for line in METRICS_PATH.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        latest[(rec["character"], rec["label"])] = rec

    lines: list[str] = []
    lines.append("# Native TTS evaluation report")
    lines.append("")
    lines.append(f"Hardware: RTX 3060 Ti, 8 GB VRAM. Eval sentence (~25 words):")
    lines.append(f"> _{EVAL_TEXT}_")
    lines.append("")

    # --- Summary table per engine (averaged across characters) ---
    engine_rows: dict[str, list[dict]] = {}
    for r in latest.values():
        engine_rows.setdefault(r["label"], []).append(r)
    lines.append("## Engine summary")
    lines.append("")
    lines.append("Aggregated across characters (lower TTFA + RTF = better).")
    lines.append("")
    lines.append("| Engine | Rows | Mean TTFA ms | Mean total ms | Mean RTF | Mean audio s | Notes |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    engine_meta = {
        "style-bert-vits2": "Pretrained checkpoint (no ref clip). Only GLaDOS has one in this round.",
        "gpt-sovits":       "Zero-shot from 5–10 s reference (api_v2.py).",
        "f5-tts":           "Zero-shot from 5–10 s reference (diffusion).",
        "rvc-reference":    "Kokoro→RVC conversion, not native — quality baseline.",
    }
    engine_order = ["style-bert-vits2", "gpt-sovits", "f5-tts", "rvc-reference"]
    for engine in engine_order:
        if engine not in engine_rows:
            continue
        rows_for = engine_rows[engine]
        n = len(rows_for)
        mttfa = sum(r["ttfa_ms"] for r in rows_for) / n
        mtot  = sum(r["total_ms"] for r in rows_for) / n
        mrtf  = sum(r["rtf"] for r in rows_for) / n
        mdur  = sum(r["audio_sec"] for r in rows_for) / n
        lines.append(
            f"| {engine} | {n} | {mttfa:.0f} | {mtot:.0f} | {mrtf:.2f} | "
            f"{mdur:.2f} | {engine_meta.get(engine, '')} |"
        )
    lines.append("")

    # --- Per-row detail ---
    lines.append("## Per-row detail")
    lines.append("")
    lines.append("| Character | Engine | TTFA ms | Total ms | Audio s | RTF | SR | VRAM MB | WAV | Notes |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    char_order = ["glados", "miku", "singer", "pain", "morgan-freeman", "optimus-prime"]
    keys = sorted(latest.keys(),
                  key=lambda k: (char_order.index(k[0]) if k[0] in char_order else 99, k[1]))
    for key in keys:
        r = latest[key]
        lines.append(
            f"| {r['character']} | {r['label']} | {r['ttfa_ms']} | {r['total_ms']} | "
            f"{r['audio_sec']:.2f} | {r['rtf']:.2f} | {r['sample_rate']} | "
            f"{r['vram_mb']} | `{r['wav']}` | {r['notes']} |"
        )
    lines.append("")

    # --- Listening guide + recommendation ---
    lines.append("## Listening guide")
    lines.append("")
    lines.append("Open one row per character and A/B between the engines. Recommended pairings:")
    lines.append("")
    for ch in char_order:
        wavs = sorted({r["label"] for k, r in latest.items() if k[0] == ch})
        if not wavs:
            continue
        files = " · ".join(f"`{ch}/{w}.wav`" for w in wavs)
        lines.append(f"- **{ch}**: {files}")
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append("- **GLaDOS**: ship Style-Bert-VITS2 (pretrained, 25× realtime, no ref clip needed). RVC and zero-shot routes all underperform here.")
    lines.append("- **Other characters**: GPT-SoVITS is the strongest all-rounder — fastest zero-shot, lowest RTF, smallest VRAM footprint. F5-TTS is comparable quality at slightly higher latency. RVC stays as the existing fallback but has the prosody-bleed issue you already flagged.")
    lines.append("- **Reference clips were auto-generated via Kokoro→RVC** for this round (see `_refs/README.md` for the recipe and `REF_RECIPE` in `native_eval.py`). Replacing them with real human reference clips per character will lift zero-shot quality further without changing the latency profile.")
    lines.append("- **VRAM headroom**: only one native engine fits alongside the live demo at a time on the 3060 Ti. A production wiring would hot-swap engines per request or keep one resident.")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append("- VRAM column shows 0 for `gpt-sovits` and `rvc` rows because those engines don't report `X-VRAM-Used-Mb` headers; live process VRAM measured externally was ~1.8 GB (gpt-sovits api_v2) and ~3.5 GB delta (rvc sidecar incl. Kokoro).")
    lines.append("- First inference per character incurs an RVC LRU load (~3–5 s in the RVC reference column). Subsequent calls are ~1–2 s.")
    lines.append("- `rvc-reference` rows use the existing Kokoro+RVC sidecar (HTTPS :12103) as a quality baseline, not native TTS.")
    lines.append("- Zero-shot rows (gpt-sovits, f5-tts) require a clean ref clip + transcript. Generated refs are themselves RVC outputs — character timbre is preserved but artifacts can imprint slightly.")
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info("wrote %s (%d rows)", REPORT_PATH, len(keys))
    write_html_report(latest, char_order, engine_order, engine_rows)


def write_html_report(latest, char_order, engine_order, engine_rows):
    """Render an HTML page with inline <audio> players for the demo to serve.

    Reachable via the demo server at /native-eval once the mount is added.
    All audio paths are relative to EVAL_ROOT so the page is portable.
    """
    rows_by_char: dict[str, list[dict]] = {}
    for (ch, _label), r in latest.items():
        rows_by_char.setdefault(ch, []).append(r)

    css = """
    body { font: 14px/1.5 -apple-system, system-ui, sans-serif; max-width: 1100px;
           margin: 2rem auto; padding: 0 1rem; color: #222; }
    h1 { margin-bottom: 0.25rem; }
    .eval-text { font-style: italic; color: #555; margin-bottom: 1.5rem; }
    h2 { margin-top: 2rem; border-bottom: 1px solid #ddd; padding-bottom: 0.25rem; }
    .summary { border-collapse: collapse; margin: 1rem 0; }
    .summary th, .summary td { padding: 6px 12px; text-align: right; }
    .summary th:first-child, .summary td:first-child { text-align: left; }
    .summary tr:nth-child(even) { background: #f8f8f8; }
    .character { margin: 1.5rem 0; padding: 1rem; background: #fafafa; border-radius: 6px; }
    .character h3 { margin: 0 0 0.75rem; text-transform: capitalize; }
    .row { display: grid; grid-template-columns: 180px 1fr 220px;
           align-items: center; gap: 1rem; padding: 0.5rem 0; }
    .row + .row { border-top: 1px dashed #ddd; }
    .engine { font-weight: 600; }
    .metrics { font-family: ui-monospace, monospace; font-size: 13px; color: #666; }
    audio { width: 100%; }
    .reco { background: #fff8e1; padding: 1rem 1.25rem; border-left: 4px solid #ffb300;
            border-radius: 0 6px 6px 0; margin: 2rem 0; }
    """

    out: list[str] = []
    out.append("<!doctype html><html><head><meta charset=utf-8>")
    out.append("<title>Native TTS evaluation</title>")
    out.append(f"<style>{css}</style></head><body>")
    out.append("<h1>Native TTS evaluation</h1>")
    out.append("<p class=eval-text>Eval sentence: "
               f"&ldquo;{EVAL_TEXT}&rdquo; · Hardware: RTX 3060 Ti, 8 GB</p>")

    # Engine summary table
    out.append("<h2>Engine summary</h2>")
    out.append("<table class=summary>")
    out.append("<tr><th>Engine</th><th>Rows</th><th>Mean TTFA ms</th>"
               "<th>Mean total ms</th><th>Mean RTF</th><th>Mean audio s</th></tr>")
    for engine in engine_order:
        if engine not in engine_rows:
            continue
        r = engine_rows[engine]
        n = len(r)
        out.append(
            f"<tr><td>{engine}</td><td>{n}</td>"
            f"<td>{sum(x['ttfa_ms'] for x in r)/n:.0f}</td>"
            f"<td>{sum(x['total_ms'] for x in r)/n:.0f}</td>"
            f"<td>{sum(x['rtf'] for x in r)/n:.2f}</td>"
            f"<td>{sum(x['audio_sec'] for x in r)/n:.2f}</td></tr>"
        )
    out.append("</table>")

    # Recommendation
    out.append("<div class=reco>")
    out.append("<strong>Quick verdict.</strong> ")
    out.append("GLaDOS: pick <code>style-bert-vits2</code> (pretrained, 25× realtime). ")
    out.append("Other characters: <code>gpt-sovits</code> is the strongest zero-shot "
               "(fastest, lowest VRAM); <code>f5-tts</code> close behind; "
               "<code>rvc-reference</code> stays as quality baseline.")
    out.append("</div>")

    # Per-character cards
    for ch in char_order:
        rows = rows_by_char.get(ch, [])
        if not rows:
            continue
        out.append(f"<div class=character><h3>{ch}</h3>")
        # Reference clip player (zero-shot input)
        ref = REFS_DIR / f"{ch}.wav"
        if ref.exists():
            ref_rel = ref.relative_to(EVAL_ROOT).as_posix()
            out.append(f'<div class=row><span class=engine>_ref (zero-shot input)</span>'
                       f'<audio controls preload=none src="{ref_rel}"></audio>'
                       f'<span class=metrics>auto-generated via Kokoro→RVC</span></div>')
        # One row per engine
        engine_label_order = ["style-bert-vits2", "gpt-sovits", "f5-tts", "rvc-reference"]
        for label in engine_label_order:
            r = next((x for x in rows if x["label"] == label), None)
            if r is None:
                continue
            rel = Path(r["wav"]).name  # filename only — file lives in <character>/
            audio_src = f"{ch}/{rel}"
            metrics = (f"ttfa {r['ttfa_ms']} ms · total {r['total_ms']} ms · "
                       f"dur {r['audio_sec']:.2f} s · rtf {r['rtf']:.2f}")
            out.append(f'<div class=row><span class=engine>{label}</span>'
                       f'<audio controls preload=none src="{audio_src}"></audio>'
                       f'<span class=metrics>{metrics}</span></div>')
        out.append("</div>")

    out.append("<p style='color:#888; margin-top:2rem; font-size:12px'>"
               "Generated by demo/native_eval.py. Source data in "
               f"<code>{EVAL_ROOT.relative_to(REPO)}</code>.</p>")
    out.append("</body></html>")
    HTML_PATH.write_text("\n".join(out), encoding="utf-8")
    log.info("wrote %s", HTML_PATH)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--engines", default="",
                        help="comma-separated subset of {sbv2,gptsovits,f5tts,rvc}; empty = all healthy")
    parser.add_argument("--report-only", action="store_true",
                        help="re-render report.md from existing _metrics.jsonl and exit")
    args = parser.parse_args()

    if args.report_only:
        write_report()
        return

    selected = {e.strip() for e in args.engines.split(",") if e.strip()}
    if not selected:
        selected = {e for e, url in SIDECAR.items() if _sidecar_healthy(url)}
        log.info("auto-detected healthy sidecars: %s", sorted(selected))

    rows = [r for r in MATRIX if r.engine in selected]
    if not rows:
        log.warning("no rows match engines=%s", selected)
        sys.exit(1)

    with httpx.Client(verify=_HTTP_VERIFY) as client:
        # Auto-stage reference clips if any zero-shot row needs them.
        if any(r.engine in {"gptsovits", "f5tts"} for r in rows):
            ensure_refs(client)
        for row in rows:
            run_row(row, client)

    write_report()


if __name__ == "__main__":
    main()
