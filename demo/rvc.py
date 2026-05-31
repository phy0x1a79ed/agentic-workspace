"""Client for the standalone RVC inference service (demo/rvc_service.py).

The main demo cannot import rvc-python directly (Python version mismatch
+ heavy CUDA torch). Instead it POSTs PCM to a loopback HTTP service
running in the `awm-rvc` env.

Usage:
    from rvc import get_rvc_client
    client = get_rvc_client()
    if client.available:
        out_pcm = client.convert(pcm_bytes, source_sr=22050)
        # out_pcm is int16 PCM @ client.sample_rate (48000)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("voice-demo.rvc")

RVC_URL = os.environ.get("RVC_URL", "http://127.0.0.1:7831")
RVC_TIMEOUT = float(os.environ.get("RVC_TIMEOUT", "30"))


class RVCClient:
    def __init__(self, url: str = RVC_URL):
        self.url = url.rstrip("/")
        self._sample_rate: Optional[int] = None
        self._available: Optional[bool] = None
        self._client = httpx.Client(timeout=RVC_TIMEOUT)

    @property
    def sample_rate(self) -> int:
        if self._sample_rate is None:
            self.check()
        return self._sample_rate or 48000

    @property
    def available(self) -> bool:
        if self._available is None:
            self.check()
        return bool(self._available)

    def check(self) -> bool:
        try:
            r = self._client.get(f"{self.url}/health", timeout=2.0)
            r.raise_for_status()
            data = r.json()
            self._available = bool(data.get("ready"))
            self._sample_rate = int(data.get("output_sample_rate", 48000))
            log.info(
                "RVC service ready=%s model=%s sr=%d",
                self._available, data.get("model"), self._sample_rate,
            )
        except Exception as exc:  # noqa: BLE001
            log.info("RVC service not reachable at %s: %s", self.url, exc)
            self._available = False
            self._sample_rate = 48000
        return bool(self._available)

    def convert(self, pcm: bytes, source_sr: int, pitch: int = 0) -> bytes:
        """POST raw int16 mono PCM, return int16 mono PCM at sample_rate."""
        if not pcm:
            return b""
        r = self._client.post(
            f"{self.url}/infer",
            content=pcm,
            params={"sr": source_sr, "pitch": pitch},
            headers={"Content-Type": "application/octet-stream"},
        )
        r.raise_for_status()
        sr_hdr = r.headers.get("X-Sample-Rate")
        if sr_hdr:
            self._sample_rate = int(sr_hdr)
        return r.content


_singleton: Optional[RVCClient] = None


def get_rvc_client() -> RVCClient:
    global _singleton
    if _singleton is None:
        _singleton = RVCClient()
    return _singleton
