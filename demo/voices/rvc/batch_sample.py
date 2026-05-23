"""Run a fixed set of source WAVs through a batch of RVC voice models.

Designed to run in the `awm-rvc` env (Python 3.10 + CUDA torch + rvc-python).
Loads RVCInference once, swaps voice models per outer iter with load_model(),
then runs inference against every source WAV for that voice (avoids reloading
voice models 4× when iterating over sources).

Inputs:
    /tmp/voice_manifest.json — list of {label, pth, index?}
    demo/voices/rvc/samples/<src_*>.wav — Kokoro-rendered source clips

Outputs:
    demo/voices/rvc/samples/<voice_label>__<source_label>.wav
    demo/voices/rvc/samples/_index.json
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("batch")

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLES_DIR = REPO_ROOT / "demo/voices/rvc/samples"
MANIFEST = Path("/tmp/voice_manifest.json")

# Source clips to convert (label, file, description). Files must exist in SAMPLES_DIR.
SOURCES = [
    ("src_male_low",    "src_male_low.wav",    "Kokoro bm_george (deep male)"),
    ("src_male_mid",    "src_male_mid.wav",    "Kokoro am_adam (mid male)"),
    ("src_female_mid",  "src_female_mid.wav",  "Kokoro bf_emma (mid female)"),
    ("src_female_high", "src_female_high.wav", "Kokoro af_sky (high female)"),
]

F0_METHOD = os.environ.get("RVC_F0_METHOD", "rmvpe")
INDEX_RATE = float(os.environ.get("RVC_INDEX_RATE", "0.5"))


def main():
    from rvc_python.infer import RVCInference

    manifest = json.loads(MANIFEST.read_text())
    src_paths = [(lab, SAMPLES_DIR / fn, desc) for lab, fn, desc in SOURCES]
    for lab, p, _ in src_paths:
        if not p.exists():
            log.error("missing source: %s (%s)", lab, p)
            return 1

    log.info("voices: %d, sources: %d  (%d total inferences)",
             len(manifest), len(src_paths), len(manifest) * len(src_paths))
    log.info("f0=%s index_rate=%.2f", F0_METHOD, INDEX_RATE)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    log.info("initializing RVCInference on cuda:0...")
    t0 = time.monotonic()
    rvc = RVCInference(models_dir=str(REPO_ROOT / "demo/voices/rvc"), device="cuda:0")
    log.info("init done in %.1fs", time.monotonic() - t0)

    report = []
    for vi, m in enumerate(manifest, 1):
        label = m["label"]
        pth = m["pth"]
        idx = m.get("index") or ""
        entry = {
            "label": label,
            "source_name": m.get("source_name", ""),
            "repo": m.get("repo", ""),
            "has_index": bool(idx),
            "status": "pending",
            "outputs": {},  # source_label -> {out, infer_ms, out_bytes}
        }

        # Try v2 first, fall back to v1 on state_dict mismatch. Detect on load.
        loaded_version = None
        last_exc: Exception | None = None
        for version in ("v2", "v1"):
            try:
                t_load = time.monotonic()
                rvc.load_model(
                    model_path_or_name=pth, index_path=idx, version=version,
                )
                rvc.set_params(f0method=F0_METHOD, index_rate=INDEX_RATE, f0up_key=0)
                entry["load_ms"] = int((time.monotonic() - t_load) * 1000)
                entry["version"] = version
                loaded_version = version
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if "state_dict" in str(exc):
                    log.warning("[%d/%d] %s — %s state_dict mismatch", vi, len(manifest), label, version)
                    continue
                break

        if loaded_version is None:
            entry["status"] = f"fail_load: {last_exc}"
            entry["traceback"] = traceback.format_exc(limit=3)
            log.error("[%d/%d] %s LOAD FAILED: %s", vi, len(manifest), label, last_exc)
            report.append(entry)
            continue

        # Now run inference against every source.
        all_ok = True
        for src_label, src_path, _ in src_paths:
            out_path = SAMPLES_DIR / f"{label}__{src_label}.wav"
            try:
                t_inf = time.monotonic()
                rvc.infer_file(str(src_path), str(out_path))
                infer_ms = int((time.monotonic() - t_inf) * 1000)
                entry["outputs"][src_label] = {
                    "out": str(out_path.relative_to(REPO_ROOT)),
                    "infer_ms": infer_ms,
                    "out_bytes": out_path.stat().st_size,
                }
            except Exception as exc:  # noqa: BLE001
                all_ok = False
                entry["outputs"][src_label] = {"error": str(exc)[:200]}
                log.error("[%d/%d] %s × %s INFER FAILED: %s",
                          vi, len(manifest), label, src_label, exc)

        entry["status"] = "ok" if all_ok else "partial"
        total_infer = sum(o.get("infer_ms", 0) for o in entry["outputs"].values())
        log.info("[%d/%d] %s — %s load=%dms infer=%dms (×%d sources) %s",
                 vi, len(manifest), label, loaded_version,
                 entry["load_ms"], total_infer, len(entry["outputs"]), entry["status"])
        report.append(entry)

    out_index = SAMPLES_DIR / "_index.json"
    out_index.write_text(json.dumps({
        "sources": [{"label": l, "file": f, "desc": d} for l, f, d in SOURCES],
        "voices": report,
    }, indent=2))
    ok = sum(1 for r in report if r["status"] == "ok")
    log.info("done: %d/%d ok, index=%s", ok, len(report), out_index)
    return 0 if ok == len(report) else 1


if __name__ == "__main__":
    sys.exit(main())
