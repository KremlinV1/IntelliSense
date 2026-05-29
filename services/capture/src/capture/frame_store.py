"""Blob store for JPEG frames.

POC: local filesystem under `frame_store_local_path`. Staging: MinIO. Prod: S3
with a WORM lifecycle policy. The interface is sync; capture calls it from a
thread pool so the event loop is never blocked on disk/network I/O.
"""
from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path


class FrameStore(ABC):
    @abstractmethod
    def put(self, frame_id: str, jpeg_bytes: bytes) -> str:
        """Persist bytes, return a URI the detection service can read from."""


class LocalFrameStore(FrameStore):
    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, frame_id: str, jpeg_bytes: bytes) -> str:
        # Shard by first 2 chars of frame_id so we don't end up with a single
        # directory containing millions of files.
        shard = self._root / frame_id[:2]
        shard.mkdir(parents=True, exist_ok=True)
        path = shard / f"{frame_id}.jpg"
        # atomic write: write-then-rename
        tmp = path.with_suffix(".jpg.tmp")
        with open(tmp, "wb") as f:
            f.write(jpeg_bytes)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        return f"file://{path}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_frame_store(backend: str, local_path: str) -> FrameStore:
    if backend == "local":
        return LocalFrameStore(local_path)
    # MinIO / S3 adapters land in Phase 8 with the WORM evidence vault work.
    raise NotImplementedError(f"frame store backend '{backend}' not implemented yet")
