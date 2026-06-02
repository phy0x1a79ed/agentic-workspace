#!/usr/bin/env bash
# Restart the Style-Bert-VITS2 sidecar (port 7843).
#
# Loads the WarriorMama777/GLaDOS_TTS checkpoint on first call to /synth
# (~200 MB, downloaded via huggingface_hub.snapshot_download into
# demo/voices/native-eval/_models/sbv2/glados/). Subsequent restarts
# reuse the local copy.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
SBV2_ENV="/home/tony/lib/miniforge3/envs/awm-sbv2"
LD_LIBRARY_PATH="$SBV2_ENV/lib/python3.10/site-packages/nvidia/cublas/lib:$SBV2_ENV/lib/python3.10/site-packages/nvidia/cuda_runtime/lib:$SBV2_ENV/lib/python3.10/site-packages/nvidia/cudnn/lib:$SBV2_ENV/lib"
export LD_LIBRARY_PATH
pkill -f "tts_sbv2_service.py" 2>/dev/null || true
sleep 1
PYTHONUNBUFFERED=1 nohup "$SBV2_ENV/bin/python" -u "$DIR/tts_sbv2_service.py" \
    --port 7843 \
    > /tmp/tts_sbv2.log 2>&1 &
echo "PID=$!"
