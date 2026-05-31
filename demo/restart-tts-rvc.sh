#!/usr/bin/env bash
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
RVC_ENV="/home/tony/lib/miniforge3/envs/rvc"
LD_LIBRARY_PATH="$RVC_ENV/lib/python3.10/site-packages/nvidia/cublas/lib:$RVC_ENV/lib/python3.10/site-packages/nvidia/cuda_runtime/lib:$RVC_ENV/lib/python3.10/site-packages/nvidia/cudnn/lib:$RVC_ENV/lib"
export LD_LIBRARY_PATH
pkill -f "tts_rvc_service.py" 2>/dev/null || true
sleep 1
PYTHONUNBUFFERED=1 nohup "$RVC_ENV/bin/python" -u "$DIR/tts_rvc_service.py" \
    --port 12123 \
    --kokoro-model "$DIR/voices/kokoro/kokoro-v1.0.onnx" \
    --kokoro-voices "$DIR/voices/kokoro/voices-v1.0.bin" \
    --manifest "$DIR/voices/rvc/manifest.json" \
    --ssl-keyfile "$DIR/dev.key" \
    --ssl-certfile "$DIR/dev.crt" \
    --device cuda:0 --lru-cap 3 \
    > /tmp/tts_rvc.log 2>&1 &
echo "PID=$!"
