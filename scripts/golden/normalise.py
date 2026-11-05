"""Mask what legitimately differs between two runs, and nothing else.

A session id, a login token or a clock reading changes on every run, so a byte comparison
needs them gone. Each is replaced by a placeholder, and identities get a number in order of
first appearance: two runs that use the same session in the same places mask to the same
text, and a run that puts the wrong session somewhere does not.

Masking happens twice. When a run is recorded, so a login token never reaches a file, and
again when it is compared, so the rule in force is the current one. It is idempotent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
# hostapp _mint_session_id: <student>-<date>-<time>-<hex>, then a longer tail on a badge.
_SESSION = re.compile(r"[A-Za-z0-9_-]*?-?\d{8}-\d{6}-[0-9a-f]{16}(?:[A-Za-z0-9_-]*[0-9a-f]{32})?")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_ISO_TIME = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_LONG_HEX = re.compile(r"\b[0-9a-f]{32,}\b")
# A query value that signs someone in. Masked whole, because its value is often itself a URL.
_SIGNED_QUERY = re.compile(r"([?&](?:amp;)?(?:sso|token|jwt)=)[^&\"'\s<>]+")

# A field holding a clock reading or something measured from one, whatever its value.
_CLOCK_KEY = re.compile(
    r"(?:^|_)(?:t|ts|time|timestamp|wall|at|elapsed|uptime|latency|duration_s|first_seen|last_seen"
    r"|age_s|since|after_s|started|ended|exp|iat)$")

TIME_MASK = "<time>"


class Normaliser:
    """Masks for one document or stream. Identity numbers count within this instance."""

    def __init__(self, drop_fields: Iterable[str] = ()) -> None:
        self._drop = frozenset(drop_fields)
        self._ids: dict[str, str] = {}

    def _identity(self, kind: str, value: str) -> str:
        known = self._ids.get(value)
        if known is None:
            known = f"<{kind}:{sum(1 for v in self._ids.values() if v.startswith('<' + kind)) + 1}>"
            self._ids[value] = known
        return known

    def text(self, value: str) -> str:
        out = _SIGNED_QUERY.sub(lambda m: m.group(1) + "<signed>", value)
        out = _JWT.sub("<jwt>", out)
        out = _SESSION.sub(lambda m: self._identity("sid", m.group(0)), out)
        out = _UUID.sub(lambda m: self._identity("uuid", m.group(0)), out)
        out = _ISO_TIME.sub(TIME_MASK, out)
        return _LONG_HEX.sub(lambda m: self._identity("hex", m.group(0)), out)

    def value(self, data: Any, *, key: str = "") -> Any:
        """A JSON value, masked. ``key`` is the field it sits under."""

        if isinstance(data, dict):
            return {k: self.value(v, key=str(k)) for k, v in data.items()
                    if str(k) not in self._drop}
        if isinstance(data, (list, tuple)):
            return [self.value(v, key=key) for v in data]
        if _CLOCK_KEY.search(key) and not isinstance(data, bool) and data not in (None, "", 0):
            return TIME_MASK
        if isinstance(data, str):
            return self.text(data)
        return data


def mask_html(html: str, normaliser: Optional[Normaliser] = None) -> str:
    return (normaliser or Normaliser()).text(html)


def mask_document(text: str) -> str:
    """A file, line by line. A line of JSON is masked by field, any other line as text."""

    normaliser = Normaliser()
    lines = []
    for line in text.splitlines():
        try:
            lines.append(json.dumps(normaliser.value(json.loads(line)), sort_keys=True,
                                    ensure_ascii=False))
        except ValueError:
            lines.append(normaliser.text(line))
    return "\n".join(lines)
