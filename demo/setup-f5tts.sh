#!/bin/bash
set -euo pipefail
cd /home/tony/agentic_workspace/projects/awm/voice
LOG=/tmp/setup-f5tts.log
echo "[$(date -Iseconds)] f5tts setup start" | tee -a "$LOG"
mamba env create -y -f demo/env-f5tts.yml 2>&1 | tee -a "$LOG"
mamba run -n awm-f5tts pip install --extra-index-url https://download.pytorch.org/whl/cu121 \
    torch==2.2.2 torchaudio==2.2.2 2>&1 | tee -a "$LOG"
mamba run -n awm-f5tts pip install f5-tts fastapi 'uvicorn[standard]' \
    huggingface_hub 2>&1 | tee -a "$LOG"
echo "[$(date -Iseconds)] f5tts setup DONE" | tee -a "$LOG"
