"""Bucket registry + factory.

``REGISTRY`` maps a bucket ``kind`` to its
:class:`~awm.social.buckets.base.Bucket` implementation. Adding a backend is one
new module + one line here — the storage-shaped parallel of
``connectors/__init__.REGISTRY``.
"""

from __future__ import annotations

from awm.social.buckets.base import Bucket, Bucket_Account, Entry
from awm.social.buckets.gdrive_bucket import GoogleDriveBucket
from awm.social.buckets.onedrive_bucket import OneDriveBucket
from awm.social.config import BucketConfig

REGISTRY: dict[str, type[Bucket]] = {
    "google_drive": GoogleDriveBucket,
    "onedrive": OneDriveBucket,
}


def build(cfg: BucketConfig) -> Bucket:
    """Construct the bucket backend for one configured ``[bucket.<name>]``.

    Routing is by ``kind``: ``google_drive`` → OAuth2 Drive client;
    ``onedrive`` → mira-daemon proxy. Config validation already rejects unknown
    kinds, so the ``ValueError`` is belt-and-braces.
    """
    account = Bucket_Account(
        name=cfg.name,
        kind=cfg.kind,
        root=cfg.root or "",
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        refresh_token=cfg.refresh_token,
        source=cfg.source,
        site=cfg.site,
        mira_url=cfg.mira_url,
        mira_token=cfg.mira_token,
        mira_verify_tls=cfg.mira_verify_tls,
    )
    cls = REGISTRY.get(cfg.kind)
    if cls is None:
        raise ValueError(f"no bucket backend for kind {cfg.kind!r}")
    return cls(account)


__all__ = [
    "REGISTRY", "build", "Bucket", "Bucket_Account", "Entry",
]
