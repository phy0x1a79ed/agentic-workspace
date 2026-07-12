"""Duo Mobile device protocol client.

A faithful port of the reverse-engineered reference implementation
``falsidge/ruo`` (AGPL-3.0) by way of the ``virtual-auth`` project: the RSA
object is named ``key`` (Ruo called the full keypair ``pubkey``, which is
misleading), credentials and the private key are persisted to explicit paths,
and transaction details are parsed into a small struct for nicer status output.

FCM push delivery is dropped here (Duo's Firebase project enforces Play
Integrity / App Check, so a headless token never registers) — the awm 2fa
service drives the device by *polling* during a burst window instead.

Protocol summary (all device requests after activation are signed):

  activate : POST https://{host}/push/v2/activation/{code}
  fetch    : GET  /push/v2/device/transactions    -> response.transactions[]
  answer   : POST /push/v2/device/transactions/{urgid}  (answer=approve|deny)

Signature: SHA-512 over "date\\nMETHOD\\nhost.lower()\\npath\\nurlencode(params)",
PKCS#1 v1.5 with the device private key, sent as
``Authorization: Basic base64(pkey + ":" + base64(sig))`` plus an ``x-duo-date``
header carrying the same RFC-2822 date used in the signed string.
"""

from __future__ import annotations

import base64
import datetime
import email.utils
import json
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

# Device parameters presented at activation. Lifted from Ruo / revalo/duo-bypass
# / FreshSupaSulley DuOSU — values that reassure Duo we are a real Android tablet.
ACTIVATION_DEVICE_PARAMS: dict[str, str] = {
    "customer_protocol": "1",
    "pkpush": "rsa-sha512",
    "jailbroken": "false",
    "architecture": "arm64",
    "region": "US",
    "app_id": "com.duosecurity.duomobile",
    "full_disk_encryption": "true",
    "passcode_status": "true",
    "platform": "Android",
    # Some Duo orgs enforce a minimum Duo Mobile version at activation and reject
    # older ones with code 40301 ("...is deprecated..."). 4.90.0 clears the gate
    # observed on the b33f80e4 org; bump if a newer org rejects it. NOTE: probe
    # the version gate with a deliberately INVALID code — a high-enough version
    # with a *valid* code will activate and consume it.
    "app_version": "4.90.0",
    "app_build_number": "490000",
    "version": "13",
    "manufacturer": "Google",
    "language": "en",
    "model": "Pixel 7",
    "security_patch_level": "2023-09-01",
}

DEFAULT_TIMEOUT = 30


@dataclass
class Transaction:
    """A pending Duo login transaction, parsed defensively from the API."""

    urgid: str
    raw: dict[str, Any] = field(default_factory=dict)

    def _attrs(self) -> dict[str, Any]:
        """Normalize ``attributes`` to a dict.

        Duo returns this under varying shapes across versions: a plain dict, a
        list of ``[label, value]`` pairs, or a list of ``{"name"/"value"}`` (or
        single-key) dicts. Anything unrecognized collapses to ``{}`` so callers
        never crash on a logging/notification path.
        """
        attrs = self.raw.get("attributes")
        if isinstance(attrs, dict):
            return attrs
        if isinstance(attrs, list):
            out: dict[str, Any] = {}
            for item in attrs:
                if isinstance(item, dict):
                    if "name" in item and "value" in item:
                        out[str(item["name"])] = item["value"]
                    else:
                        out.update(item)
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    out[str(item[0])] = item[1]
            return out
        return {}

    @property
    def app(self) -> str:
        # Duo exposes the integration/app name under varying keys across versions.
        for k in ("integration_name", "service", "caller_name"):
            if self.raw.get(k):
                return str(self.raw[k])
        attrs = self._attrs()
        return str(attrs.get("Application") or attrs.get("integration_name") or "unknown app")

    @property
    def details(self) -> dict[str, str]:
        """Best-effort human-readable fields (location, IP, browser, time)."""
        out: dict[str, str] = {}
        attrs = self._attrs()
        merged = {**self.raw, **attrs}
        mapping = {
            "Location": ("location", "Location"),
            "IP address": ("ip_address", "IP address", "ip"),
            "Browser": ("browser", "Browser"),
            "Time": ("time", "Time", "created"),
            "User": ("display_username", "username", "User"),
        }
        for label, keys in mapping.items():
            for k in keys:
                if merged.get(k):
                    out[label] = str(merged[k])
                    break
        return out


class DuoClient:
    def __init__(self, host: str | None = None, akey: str | None = None,
                 pkey: str | None = None, key: RSA.RsaKey | None = None):
        self.host = host
        self.akey = akey
        self.pkey = pkey
        self.key = key  # RSA keypair (private). Generated lazily for activation.
        self.code: str | None = None
        self.info: dict[str, Any] = {}

    # ---- persistence -------------------------------------------------------

    @classmethod
    def load(cls, creds_path: Path, key_path: Path) -> "DuoClient":
        with open(key_path, "rb") as f:
            key = RSA.import_key(f.read())
        with open(creds_path, "r") as f:
            info = json.load(f)
        if "response" in info:
            info = info["response"]
        c = cls(host=info.get("host"), akey=info["akey"], pkey=info["pkey"], key=key)
        c.info = info
        return c

    def save(self, creds_path: Path, key_path: Path) -> None:
        creds_path.parent.mkdir(parents=True, exist_ok=True)
        with open(key_path, "wb") as f:
            f.write(self.key.export_key("PEM"))
        key_path.chmod(0o600)
        info = dict(self.info)
        info.setdefault("host", self.host)
        with open(creds_path, "w") as f:
            json.dump(info, f, indent=2)
        creds_path.chmod(0o600)

    # ---- activation --------------------------------------------------------

    def read_code(self, code: str) -> None:
        """Parse a ``CODE-BASE64HOST`` activation string (the QR / email value)."""
        code, host = (x.strip("<>") for x in code.split("-", 1))
        missing = len(host) % 4
        if missing:
            host += "=" * (4 - missing)
        self.code = code
        self.host = base64.b64decode(host.encode("ascii")).decode("ascii")

    def activate(self) -> dict[str, Any]:
        if not self.code:
            raise ValueError("no activation code; call read_code() first")
        if self.key is None:
            self.key = RSA.generate(2048)
        params = dict(ACTIVATION_DEVICE_PARAMS)
        params["pubkey"] = self.key.publickey().export_key("PEM").decode("ascii")
        r = requests.post(
            f"https://{self.host}/push/v2/activation/{self.code}",
            params=params, timeout=DEFAULT_TIMEOUT,
        )
        r.raise_for_status()
        body = r.json()
        resp = body.get("response", body)
        self.info = resp
        self.akey = resp["akey"]
        self.pkey = resp["pkey"]
        if not self.info.get("host"):
            self.info["host"] = self.host
        return resp

    # ---- signed device requests -------------------------------------------

    def _now(self) -> str:
        return email.utils.format_datetime(datetime.datetime.now(datetime.timezone.utc))

    def _auth_header(self, method: str, path: str, date: str, data: dict[str, str]) -> str:
        message = (
            date + "\n" + method + "\n" + self.host.lower() + "\n" + path + "\n"
            + urllib.parse.urlencode(data)
        ).encode("ascii")
        sig = pkcs1_15.new(self.key).sign(SHA512.new(message))
        token = base64.b64encode(
            (self.pkey + ":" + base64.b64encode(sig).decode("ascii")).encode("ascii")
        ).decode("ascii")
        return "Basic " + token

    def _request(self, method: str, path: str, data: dict[str, str],
                 headers_extra: dict[str, str] | None = None,
                 timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
        date = self._now()
        headers = {
            "Authorization": self._auth_header(method, path, date, data),
            "x-duo-date": date,
            "host": self.host,
        }
        if headers_extra:
            headers.update(headers_extra)
        url = f"https://{self.host}{path}"
        if method == "GET":
            r = requests.get(url, params=data, headers=headers, timeout=timeout)
        else:
            r = requests.post(url, data=data, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def get_transactions(self, timeout: int = DEFAULT_TIMEOUT) -> list[Transaction]:
        body = self._request(
            "GET", "/push/v2/device/transactions",
            {"akey": self.akey, "fips_status": "1", "hsm_status": "true", "pkpush": "rsa-sha512"},
            timeout=timeout,
        )
        txs = (body.get("response") or {}).get("transactions") or []
        return [Transaction(urgid=t["urgid"], raw=t) for t in txs]

    def reply_transaction(self, urgid: str, answer: str) -> dict[str, Any]:
        if answer not in ("approve", "deny"):
            raise ValueError(f"answer must be 'approve' or 'deny', got {answer!r}")
        return self._request(
            "POST", "/push/v2/device/transactions/" + urgid,
            {"akey": self.akey, "answer": answer, "fips_status": "1",
             "hsm_status": "true", "pkpush": "rsa-sha512"},
            headers_extra={"txId": urgid},
        )
