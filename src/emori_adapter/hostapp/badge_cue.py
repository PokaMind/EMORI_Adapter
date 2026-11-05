"""The optional spoken "badge not recognized" cue: one locally reviewed WAV, pinned by digest.

A deployment that has decided to reveal roster membership provisions the file and its
SHA-256. This is never a synthesis or substitution path: an invalid or mismatched file
raises and the caller falls back to no cue at all.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import stat
import struct
from pathlib import Path

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WAV_SAMPLE_RATE = 24000
_WAV_CHANNELS = 1
_WAV_BITS_PER_SAMPLE = 16


class BadgeCueError(ValueError):
    """The cue file is missing a digest, malformed, or not the reviewed one."""


def _validate_wav(raw: bytes) -> None:
    """Require a complete 24 kHz mono PCM16 RIFF/WAV container.

    Parsing the complete RIFF chunk table detects malformed headers, truncated chunks,
    duplicate ``fmt``/``data`` chunks and stale declared lengths before a page can try to
    play the file.
    """

    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise BadgeCueError("invalid_wav_asset")
    if struct.unpack_from("<I", raw, 4)[0] != len(raw) - 8:
        raise BadgeCueError("invalid_wav_asset")

    offset = 12
    fmt_chunk: bytes | None = None
    data_chunk: bytes | None = None
    while offset < len(raw):
        if offset + 8 > len(raw):
            raise BadgeCueError("invalid_wav_asset")
        chunk_id = raw[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", raw, offset + 4)[0]
        offset += 8
        end = offset + chunk_size
        if end > len(raw):
            raise BadgeCueError("invalid_wav_asset")
        chunk = raw[offset:end]
        if chunk_id == b"fmt ":
            if fmt_chunk is not None:
                raise BadgeCueError("invalid_wav_asset")
            fmt_chunk = chunk
        elif chunk_id == b"data":
            if data_chunk is not None:
                raise BadgeCueError("invalid_wav_asset")
            data_chunk = chunk
        offset = end + (chunk_size & 1)
        if offset > len(raw):
            raise BadgeCueError("invalid_wav_asset")

    if offset != len(raw) or fmt_chunk is None or data_chunk is None or len(fmt_chunk) < 16:
        raise BadgeCueError("invalid_wav_asset")
    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = (
        struct.unpack_from("<HHIIHH", fmt_chunk)
    )
    if (
        audio_format != 1
        or channels != _WAV_CHANNELS
        or sample_rate != _WAV_SAMPLE_RATE
        or bits_per_sample != _WAV_BITS_PER_SAMPLE
        or block_align != 2
        or byte_rate != _WAV_SAMPLE_RATE * block_align
        or not data_chunk
        or len(data_chunk) % block_align != 0
    ):
        raise BadgeCueError("invalid_wav_asset")


def load_badge_cue_wav(path: Path, expected_sha256: str) -> bytes:
    """Read the reviewed cue: a regular file, a complete WAV, the pinned digest."""

    if not _SHA256_RE.match(expected_sha256 or ""):
        raise BadgeCueError("asset_hash_invalid")
    path = Path(path)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise BadgeCueError("asset_symlink_forbidden")
    if not stat.S_ISREG(mode):
        raise BadgeCueError("asset_type_invalid")
    raw = path.read_bytes()
    _validate_wav(raw)
    if not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
        raise BadgeCueError("asset_hash_mismatch")
    return raw
