"""Persisted superadmin settings (audio levels, mic-profile multipliers).

Same atomic-JSON pattern as ``hostapp/lang_state.py``: a small state file
outranks env defaults, written via tempfile + ``os.replace`` so a power cut
mid-write can never corrupt it, and a missing/corrupt file silently means
"no overrides" — the admin surface must degrade, never crash, because it is
the recovery tool.

Two consumers:

* the ``emori-admin`` service (its own process) reads AND writes via
  :class:`AdminState`;
* the hostapp's ``mic_profile.py`` (a different process) reads the
  mic-multiplier overrides via :func:`read_state` on each profile switch —
  profile switches happen a handful of times per session, so a fresh read
  per call is cheap and needs no cross-process invalidation protocol.

This module is stdlib-only on purpose: the hostapp imports it and must not
gain new dependencies from doing so.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any

ENV_STATE_FILE = "EMORI_ADMIN_STATE_FILE"
DEFAULT_STATE_FILE = "artifacts/admin_state.json"

# Field name -> (validator, human range description). Unknown fields are
# rejected by update() and ignored by read_state() so a newer writer cannot
# poison an older reader.
_MAX_MULT = 4.0  # mirrors mic_profile._MAX_VOL — wpctl gain multiplier ceiling


def _valid_percent(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100


def _valid_mult(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0.0 <= float(value) <= _MAX_MULT
    )


_FIELDS = {
    # Desired hardware levels (daemon /api/volume scale, 0-100). Re-applied
    # at emori-admin startup so a reboot can never leave the speaker at 0.
    "speaker_volume": _valid_percent,
    "mic_volume": _valid_percent,
    # Operator overrides for the hostapp's wpctl mic-profile multipliers
    # (session/idle). None/absent = keep the env-var/built-in defaults.
    "mic_session_mult": _valid_mult,
    "mic_idle_mult": _valid_mult,
}


def _state_path(path: str | None = None) -> str:
    return path or os.environ.get(ENV_STATE_FILE, "").strip() or DEFAULT_STATE_FILE


def read_state(path: str | None = None) -> dict[str, float]:
    """Best-effort read of the persisted settings.

    Returns only known, valid fields; ``{}`` when the file is missing,
    unreadable, or corrupt. Never raises — callers treat the result as
    optional overrides on top of their own defaults.
    """
    target = _state_path(path)
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, valid in _FIELDS.items():
        value = raw.get(key)
        if value is not None and valid(value):
            out[key] = float(value)
    return out


class AdminState:
    """Read/write handle used by the emori-admin service process."""

    def __init__(self, path: str | None = None) -> None:
        self._path = _state_path(path)
        self._lock = threading.RLock()

    @property
    def path(self) -> str:
        return self._path

    def get(self) -> dict[str, float]:
        """Current persisted settings (known valid fields only)."""
        with self._lock:
            return read_state(self._path)

    def update(self, **fields: Any) -> dict[str, float]:
        """Merge ``fields`` into the state file atomically and return the result.

        Unknown field names raise ``ValueError`` (a typo must not silently
        persist nothing); out-of-range values raise ``ValueError`` too.
        Passing ``None`` for a field removes the override.
        """
        for key, value in fields.items():
            if key not in _FIELDS:
                raise ValueError(f"unknown admin state field: {key!r}")
            if value is not None and not _FIELDS[key](value):
                raise ValueError(f"invalid value for {key}: {value!r}")
        with self._lock:
            state = read_state(self._path)
            for key, value in fields.items():
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = float(value)
            self._write(state)
            return dict(state)

    def _write(self, state: dict[str, float]) -> None:
        """Atomic write (tempfile in the same directory + os.replace).

        Best-effort on the temp-file cleanup path but the write itself is
        allowed to raise OSError — the admin API turns that into a 500 the
        operator can see, rather than silently dropping their setting.
        """
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".admin_state_", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
