#!/bin/bash
set -euo pipefail
cd /home/tony/agentic_workspace/projects/awm/voice
LOG=/tmp/setup-gptsovits.log
echo "[$(date -Iseconds)] gptsovits setup start" | tee -a "$LOG"
mamba env create -y -f demo/env-gptsovits.yml 2>&1 | tee -a "$LOG"
mamba run -n awm-gptsovits pip install --extra-index-url https://download.pytorch.org/whl/cu121 \
    torch==2.2.2 torchaudio==2.2.2 2>&1 | tee -a "$LOG"
mkdir -p demo/voices/native-eval/_repos
if [ ! -d demo/voices/native-eval/_repos/GPT-SoVITS ]; then
    git clone --depth 1 https://github.com/RVC-Boss/GPT-SoVITS \
        demo/voices/native-eval/_repos/GPT-SoVITS 2>&1 | tee -a "$LOG"
fi
mamba run -n awm-gptsovits pip install \
    -r demo/voices/native-eval/_repos/GPT-SoVITS/requirements.txt 2>&1 | tee -a "$LOG"
mamba run -n awm-gptsovits pip install fastapi 'uvicorn[standard]' \
    huggingface_hub 2>&1 | tee -a "$LOG"
mamba run -n awm-gptsovits python -c "
from huggingface_hub import snapshot_download
snapshot_download('lj1995/GPT-SoVITS',
                  local_dir='demo/voices/native-eval/_repos/GPT-SoVITS/GPT_SoVITS/pretrained_models')
" 2>&1 | tee -a "$LOG"
echo "[$(date -Iseconds)] gptsovits setup DONE" | tee -a "$LOG"
