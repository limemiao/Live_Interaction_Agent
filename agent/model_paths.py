"""Resolve project model dependencies from the Hugging Face cache."""

from __future__ import annotations

from functools import lru_cache

from huggingface_hub import snapshot_download


ASR_LARGE_V3 = "Systran/faster-whisper-large-v3"
EMBED_BGE_SMALL_ZH = "BAAI/bge-small-zh-v1.5"
SPEAKER_ECAPA = "speechbrain/spkrec-ecapa-voxceleb"


@lru_cache(maxsize=None)
def cached_snapshot(repo_id: str) -> str:
    """Return a complete local snapshot without making a network request."""
    return snapshot_download(repo_id=repo_id, local_files_only=True)
