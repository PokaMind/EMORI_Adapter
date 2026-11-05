"""Conversation watchdog: detects a turn that opened but never submitted and
drives recovery (re-prompt, then graceful end). Distinct from IdleTicker, which
only keeps the robot's BODY alive -- this keeps the CONVERSATION alive.

Pure state machine over an injected monotonic ``now`` (the caller passes
``time.monotonic()``), so it is testable without real time or threads. It does
NOT touch the robot. The :class:`~emori_adapter.hostapp.driver.WatchdogTicker`
thread polls :meth:`ConversationWatchdog.due` and dispatches the side effects.

Thread-safety: this object is written by FastAPI threadpool handlers
(``on_armed`` / ``on_user_speaking`` / ``on_submitted`` / ``on_ai_utterance`` /
``on_activity``) AND read+written by the WatchdogTicker daemon (``due``). It
therefore owns a private :class:`threading.Lock` taken for the FULL
read-modify-write of every public method. The driver must NEVER hold
``driver._lock`` while calling into the watchdog, and the ticker must release
the watchdog lock (done implicitly when ``due`` returns) BEFORE it acquires
``driver._lock`` to finalize -- the two locks are never held at once (no
AB/BA deadlock).

Legal transitions (the clock lives ONLY in OPEN):
    any                      --on_session_start--> IDLE     (new learner: first-turn grace)
    IDLE/SUBMITTED/TIMED_OUT --on_armed--> ARMED          (no clock; timer-inert)
    ARMED/IDLE               --on_user_speaking--> OPEN    (starts _opened_at)
    OPEN                     --on_activity--> OPEN         (refreshes the quiet clock)
    OPEN                     --on_turn_rejected--> OPEN     (bounded second chance)
    OPEN                     --on_submitted--> SUBMITTED    (clears clock)
    OPEN                     --due() ceiling--> TIMED_OUT   (one-shot force_end)
    any                      --on_ai_utterance--> IDLE      (turn advanced normally)

End policy (presence-aware, so a session never ends on a learner who is there):
    * silent stall   -- ``quiet >= ceiling`` (first turn: ``first_ceiling_s``) ends the
                        turn ONLY while nobody is seen (``due(present=False)``);
    * unseen ceiling -- ``open >= abs_ceiling_s`` ends a turn that sound keeps alive
                        while nobody is seen (the noisy empty room);
    * present silence-- ``open >= present_silence_s`` (never before the turn's silent
                        ceiling) with ``quiet >= present_quiet_s`` ends a turn on
                        somebody who is seen but no longer answering (0 = off);
    * present cap    -- ``open >= present_ceiling_s`` is the last end while somebody
                        is seen, for sound that never stops (0 = presence never extends
                        anything: legacy).
Every class default reproduces the earlier, presence-blind behaviour exactly; the
driver wires the production policy from the environment.
"""

from __future__ import annotations

import enum
import threading
from typing import Optional


class TurnState(enum.Enum):
    IDLE = "idle"
    ARMED = "armed"
    OPEN = "open"
    SUBMITTED = "submitted"
    TIMED_OUT = "timed_out"


class ConversationWatchdog:
    """One-turn-at-a-time watchdog. Fed events; reports 'reprompt' / 'force_end'.

    ``reprompt_after_s`` should be strictly less than ``open_ceiling_s``; if it is
    not, the ceiling dominates and force_end fires WITHOUT a reprompt (fail-safe).
    """

    def __init__(
        self,
        *,
        open_ceiling_s: float = 15.0,
        reprompt_after_s: float = 10.0,
        abs_ceiling_s: float = 40.0,
        reject_extensions: int = 3,
        first_ceiling_s: Optional[float] = None,
        first_reprompt_s: Optional[float] = None,
        max_reprompts: int = 1,
        max_reprompts_per_turn: int = 0,
        reprompt_every_s: float = 15.0,
        present_ceiling_s: float = 0.0,
        present_silence_s: float = 0.0,
        present_quiet_s: float = 10.0,
        self_noise_s: float = 0.0,
        armed_ceiling_s: float = 0.0,
    ) -> None:
        self.open_ceiling_s = open_ceiling_s
        self.reprompt_after_s = reprompt_after_s
        # The FIRST user turn of a session (no turn submitted yet) gets its own,
        # longer windows: a learner's first sound can come 18-22 s after the turn
        # opens (they are reading the scenario back to themselves), and a 15 s stall
        # ceiling would end the session before they spoke. None = same as later.
        self.first_ceiling_s = open_ceiling_s if first_ceiling_s is None else first_ceiling_s
        self.first_reprompt_s = (reprompt_after_s if first_reprompt_s is None
                                 else first_reprompt_s)
        # Re-prompts per QUIET STRETCH (activity starts a new stretch), the k-th one at
        # reprompt_at + (k-1) * reprompt_every_s; and a cap per OPEN window (0 = none).
        self.max_reprompts = max(0, int(max_reprompts))
        self.max_reprompts_per_turn = max(0, int(max_reprompts_per_turn))
        self.reprompt_every_s = max(0.1, float(reprompt_every_s))
        # How many REJECTED attempts may each extend one open turn.
        self.reject_extensions = max(0, int(reject_extensions))
        # A HARD ceiling from turn-open, immune to on_activity refreshes, so
        # continuous background speech cannot hold a turn open forever. It applies
        # to turns where NOBODY IS SEEN; 0 disables.
        self.abs_ceiling_s = abs_ceiling_s
        # The last end while a person is seen (camera body / robot face). 0 = presence
        # extends nothing (the earlier, presence-blind behaviour).
        self.present_ceiling_s = present_ceiling_s
        # Being seen is not answering. A person who has stopped talking is ended once the
        # turn has run this long and nothing has been heard for present_quiet_s, so someone
        # still talking, whose attempts keep being rejected, is left to the cap. 0 = off.
        self.present_silence_s = max(0.0, float(present_silence_s))
        self.present_quiet_s = max(0.0, float(present_quiet_s))
        # ARMED is not timer-inert, which would leave one path unbounded: an endpointer
        # that hangs or fails to load never posts the turn's open, so a learner standing
        # in front of a stuck page would never be ended (the walk-away is held off by their
        # camera cycles). The worst endpointer load measured on the robot was 34.5 s.
        # 0 keeps ARMED inert (legacy).
        self.armed_ceiling_s = max(0.0, float(armed_ceiling_s))
        self._armed_at: Optional[float] = None
        # After a re-prompt the robot MOVES (and may speak) next to an open mic: sound in
        # this window is ours, not the learner's, and must not refresh the quiet clock.
        self.self_noise_s = max(0.0, float(self_noise_s))
        self.state = TurnState.IDLE
        self._opened_at: Optional[float] = None
        self._last_activity_at: Optional[float] = None
        self._stretch_reprompts = 0
        self._turn_reprompts = 0
        self._rejects = 0
        self._activity = 0
        self._blank_until: Optional[float] = None
        self._submitted = 0
        self.last_end_kind = ""
        # Private lock: guards the full read-modify-write of every public method so
        # the ticker (due) and the handlers never corrupt state/_opened_at/latches.
        # NEVER reuse driver._lock here, and never hold both at once.
        self._lock = threading.Lock()

    # Kept for callers/tests that read the old one-shot latch.
    @property
    def _reprompted(self) -> bool:
        return self._stretch_reprompts > 0

    @property
    def bounded(self) -> bool:
        """Every OPEN window ends on its own (the walk-away may stand down in OPEN)."""
        return self.abs_ceiling_s > 0

    def _reset_turn_clock(self) -> None:
        """Clear everything that describes ONE open window. Caller holds self._lock."""
        self._opened_at = None
        self._last_activity_at = None
        self._stretch_reprompts = 0
        self._turn_reprompts = 0
        self._rejects = 0
        self._activity = 0
        self._blank_until = None

    def on_session_start(self, *, now: float) -> None:
        """A new learner: back to IDLE, and the next turn is a FIRST turn again."""
        with self._lock:
            self.state = TurnState.IDLE
            self._reset_turn_clock()
            self._submitted = 0
            self.last_end_kind = ""

    def on_armed(self, *, now: float) -> None:
        """AI finished speaking; the turn is about to open. Only ``armed_ceiling_s``
        (a stuck endpointer) can end an ARMED turn."""
        with self._lock:
            self.state = TurnState.ARMED
            self._reset_turn_clock()
            self._armed_at = now

    def on_user_speaking(self, *, now: float) -> None:
        """The user has the floor: the OPEN window (and its clock) begins now."""
        with self._lock:
            if self.state in (TurnState.ARMED, TurnState.IDLE):
                self.state = TurnState.OPEN
                self._reset_turn_clock()
                self._opened_at = now

    def on_submitted(self, *, now: float) -> None:
        """Turn submitted: close the OPEN window and clear the clock + latch."""
        with self._lock:
            self.state = TurnState.SUBMITTED
            self._reset_turn_clock()
            self._submitted += 1

    def on_ai_utterance(self, *, now: float) -> None:
        """AI is speaking => the previous turn advanced normally; disarm to IDLE."""
        with self._lock:
            self.state = TurnState.IDLE
            self._reset_turn_clock()

    def _blanked(self, now: float) -> bool:
        return self._blank_until is not None and now < self._blank_until

    def on_activity(self, *, now: float) -> None:
        """Speech activity while the turn is OPEN: refresh the quiet clock and start a
        new quiet stretch. A continuously speaking user is never force-ended by the
        silent-stall rule. Ignored outside OPEN, and ignored for the clock while
        the robot's own re-prompt is moving/speaking next to the mic."""
        with self._lock:
            if self.state is not TurnState.OPEN:
                return
            self._activity += 1
            if self._blanked(now):
                return
            self._last_activity_at = now
            self._stretch_reprompts = 0

    def on_turn_rejected(self, *, now: float) -> None:
        """A turn was heard and thrown away: give the speaker another window.

        Refreshes the quiet clock and starts a new quiet stretch, exactly like real
        activity -- but only ``reject_extensions`` times per open window, never inside
        the self-noise blank, and never past the hard ceilings. Ignored outside OPEN.
        """
        with self._lock:
            if self.state is not TurnState.OPEN:
                return
            if self._blanked(now) or self._rejects >= self.reject_extensions:
                return
            self._rejects += 1
            self._last_activity_at = now
            self._stretch_reprompts = 0

    def _time_out(self, kind: str) -> str:
        self.state = TurnState.TIMED_OUT      # terminal: subsequent due() -> None
        self.last_end_kind = kind
        return "force_end"

    def due(self, *, now: float, present: bool = False) -> Optional[str]:
        """Return 'reprompt' when the open turn has been quiet long enough, then
        'force_end' when a ceiling is reached. None otherwise.

        ``present``: somebody is SEEN in front of the robot right now (the caller
        decides from camera evidence). While present, only ``present_ceiling_s`` can
        end the turn. The whole check-and-mutate runs inside one lock acquisition, so
        each action fires at most once per due step even under concurrent events."""
        with self._lock:
            if (self.state is TurnState.ARMED and self.armed_ceiling_s > 0
                    and self._armed_at is not None
                    and now - self._armed_at >= self.armed_ceiling_s):
                return self._time_out("armed_ceiling")
            if self.state is not TurnState.OPEN or self._opened_at is None:
                return None
            first = self._submitted == 0
            ceiling = self.first_ceiling_s if first else self.open_ceiling_s
            reprompt_at = self.first_reprompt_s if first else self.reprompt_after_s
            open_for = now - self._opened_at
            base = self._opened_at if self._last_activity_at is None \
                else max(self._opened_at, self._last_activity_at)
            quiet = now - base
            seen = bool(present) and self.present_ceiling_s > 0
            cap = self.present_ceiling_s if seen else self.abs_ceiling_s
            if cap > 0 and open_for >= cap:
                return self._time_out("present_cap" if seen else "abs_ceiling")
            if (seen and self.present_silence_s > 0
                    and open_for >= max(self.present_silence_s, ceiling)
                    and quiet >= self.present_quiet_s):
                return self._time_out("present_silent")
            # Silent stall FIRST (before the re-prompt) so a misconfigured
            # reprompt_after_s >= open_ceiling_s fails safe instead of spamming.
            if not seen and quiet >= ceiling:
                return self._time_out("silent")
            if (self._stretch_reprompts < self.max_reprompts
                    and (self.max_reprompts_per_turn <= 0
                         or self._turn_reprompts < self.max_reprompts_per_turn)
                    and quiet >= reprompt_at + self._stretch_reprompts * self.reprompt_every_s):
                self._stretch_reprompts += 1
                self._turn_reprompts += 1
                if self.self_noise_s > 0:
                    self._blank_until = now + self.self_noise_s
                return "reprompt"
            return None

    def snapshot(self, *, now: float) -> dict:
        """Numbers only (no speech content) for the journal line and /api/status."""
        with self._lock:
            opened = self._opened_at
            base = None if opened is None else (
                opened if self._last_activity_at is None
                else max(opened, self._last_activity_at))
            return {
                "state": self.state.value,
                "first_turn": self._submitted == 0,
                "open_s": None if opened is None else round(now - opened, 1),
                "quiet_s": None if base is None else round(now - base, 1),
                "reprompts": self._turn_reprompts,
                "rejects": self._rejects,
                "activity": self._activity,
                "turns_submitted": self._submitted,
                "last_end_kind": self.last_end_kind,
            }
