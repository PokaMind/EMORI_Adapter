"""Session-source interface.

A :class:`SessionSource` yields :class:`AnalysisCycle` objects at the session's
natural cadence (~2 s). Implementations: mock generator, CSV replay, and the real
Pokamind live client. The source owns pacing (it may sleep between cycles); the
pipeline just consumes.
"""

from __future__ import annotations

import abc
from collections.abc import Iterator

from emori_adapter.models import AnalysisCycle


class SessionSource(abc.ABC):
    """Abstract producer of per-cycle Pokamind analysis."""

    @abc.abstractmethod
    def cycles(self) -> Iterator[AnalysisCycle]:
        """Yield analysis cycles until the session ends."""
        raise NotImplementedError

    def close(self) -> None:  # optional cleanup hook
        pass
