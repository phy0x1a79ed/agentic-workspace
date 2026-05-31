#!/bin/bash
set -euo pipefail
cd /home/tony/agentic_workspace/projects/awm/voice
LOG=/tmp/setup-sbv2.log
echo "[$(date -Iseconds)] sbv2 setup start" | tee -a "$LOG"
mamba env create -y -f demo/env-sbv2.yml 2>&1 | tee -a "$LOG"
mamba run -n awm-sbv2 pip install --extra-index-url https://download.pytorch.org/whl/cu121 \
    torch==2.2.2 torchaudio==2.2.2 2>&1 | tee -a "$LOG"
mamba run -n awm-sbv2 pip install style-bert-vits2 fastapi 'uvicorn[standard]' \
    huggingface_hub 2>&1 | tee -a "$LOG"
echo "[$(date -Iseconds)] sbv2 setup DONE" | tee -a "$LOG"
