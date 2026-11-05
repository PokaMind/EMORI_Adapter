"""What happened to each spoken sentence's motion plan, as a stream.

A sentence's plan has three moments, and each is one record:

========  ==========================  =======================================================
Record    Put by                      Holds
========  ==========================  =======================================================
planned   ``LiveDriver.on_utterance`` plan id, sentence seq, the family performed, the family
                                      Pokamind forwarded, how the family was chosen, the slug,
                                      the sentence's event time, and why nothing was planned
started   ``RobotPlayer._play_batch`` plan id and the time of the plan's first command
ended     ``RobotPlayer``             plan id, outcome, and why the plan was skipped
========  ==========================  =======================================================

**What the motion path pays.** With no reader the driver and the player hold ``None`` here,
and each of the three moments is a ``None`` check. With readers each moment is one
:meth:`PlanEventStream.put`: a tuple the caller built and, per reader, one length test and one
``deque.append``. There is no lock here, no wait, no formatting and no I/O. A reader that is
behind or gone changes that cost by nothing: its queue is full, the record is not kept and the
loss is counted.

**The join.** A sentence's family is only known once the scheduler has submitted its plan, so
the player's first record can reach a queue just before the driver's ``planned``. Everything
that reads the stream therefore goes through :class:`PlanJoin`, off the motion path, which holds
a player's record until its plan is named and turns records into whole :class:`PlanStep` values.

**The readers.** :class:`PlanEventReader` drains in the order the records were put, and is what
a graph node ticks on. :class:`SessionPlanCounts` keeps a session's records until the session
ends, and is what reaches the metrics store.

Records hold ids, times, families, slugs and outcomes, never the words of a sentence or a cue
direction.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace

PLANNED = "planned"
STARTED = "started"
ENDED = "ended"

#: The plan performed: everything up to its last moving intent went out. What can be left then
#: is the settle, which the next sentence or the end of the turn replaces.
FINISHED = "finished"
#: Something newer took the head first: the next sentence, the end of the turn, an escalation,
#: the end of the session, or a player that was stopped.
PREEMPTED = "preempted"
#: The voice stopped first, so the rest of the plan was dropped and the head settled.
VOICE_END = "voice_end"
#: The plan never performed. ``skip_reason`` says why.
SKIPPED = "skipped"
#: Counted at the end of a session only: the plan was still running when the session ended.
UNFINISHED = "unfinished"
#: Counted at the end of a session only: records its queue could not hold, not sentences.
LOST = "lost"

#: The voice never came, so nothing but the settle was sent.
NO_VOICE = "no_voice"
#: ``EMORI_CUE_MOTION=0``: the sentence got the fixed talking gesture and no plan.
CUE_MOTION_OFF = "cue_motion_off"
#: The robot refused the recording that was this plan's performance.
REFUSED = "refused"

#: In a session's counts, in place of a family: the sentence's plan moved nothing, so naming a
#: family would say the robot performed one.
NOT_PERFORMED = "-"

#: ``first_apply_delay_s`` of a plan that has sent nothing.
NOT_STARTED = -1.0

#: A reader that drains on a timer: a sentence is three records, and these are seconds apart.
READER_CAPACITY = 256
#: A session's records until its end, about 2700 sentences.
SESSION_CAPACITY = 8192

_MAX_OPEN = 64
_MAX_HELD = 64


def batch_outcome(n: int, issued: int, *, no_voice: bool, voice_cut: bool,
                  preempted: bool, refused: bool = False) -> tuple[str, str]:
    """``(outcome, skip_reason)`` of a batch of ``n`` intents, on the player's own facts.

    ``issued`` is the highest intent index sent, -1 for none. ``no_voice``: the voice hold ran
    out. ``voice_cut``: the voice stopped with intents still to go. ``preempted``: a recorded
    move was stopped under a newer sentence, which no index can show because the whole
    performance is one intent. ``refused``: the robot said no to that recording, and what went
    out afterwards was only the settle.
    """

    if no_voice:
        return SKIPPED, NO_VOICE
    if refused:
        return SKIPPED, REFUSED
    if preempted:
        return PREEMPTED, ""
    if voice_cut:
        return VOICE_END, ""
    if issued >= max(0, n - 2):
        return FINISHED, ""
    return PREEMPTED, ""


# --- the motion path's side ------------------------------------------------------------


class PlanEventQueue:
    """One reader's bounded queue. Any thread may put; one thread drains."""

    __slots__ = ("capacity", "dropped", "_buffer")

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.dropped = 0
        self._buffer: deque = deque()

    def put(self, event: tuple) -> None:
        buffer = self._buffer
        if len(buffer) >= self.capacity:
            self.dropped += 1
            return
        buffer.append(event)

    def drain(self) -> list[tuple]:
        buffer = self._buffer
        events: list[tuple] = []
        while True:
            try:
                events.append(buffer.popleft())
            except IndexError:
                return events


class PlanEventStream:
    """The one thing the motion path holds. See the module docstring for what it costs."""

    __slots__ = ("_queues",)

    def __init__(self) -> None:
        self._queues: tuple[PlanEventQueue, ...] = ()

    def put(self, event: tuple) -> None:
        for queue in self._queues:
            queue.put(event)

    def subscribe(self, capacity: int) -> PlanEventQueue:
        """A new reader's queue. Replaces the tuple whole, so a put never sees it half made."""

        queue = PlanEventQueue(capacity)
        self._queues = (*self._queues, queue)
        return queue


# --- the readers' side -----------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """One moment of one plan, with everything known about the plan by then.

    ``family`` is the family performed, empty when nothing was planned or the sentence went on
    the talking flow. ``first_apply_delay_s`` runs from the sentence's event to the plan's first
    command, so it includes the wait for the voice.
    """

    plan_id: int
    seq: int
    family: str = ""
    family_authored: str = ""
    family_source: str = ""
    slug: str = ""
    started: bool = False
    ended: bool = False
    outcome: str = ""
    skip_reason: str = ""
    first_apply_delay_s: float = NOT_STARTED


class PlanJoin:
    """Records in, whole steps out. One thread at a time; never on the motion path."""

    def __init__(self) -> None:
        self._open: dict[int, tuple[PlanStep, float]] = {}
        self._held: dict[int, list[tuple]] = {}
        #: Records given up on: no ``planned`` came for them, because the queue lost it or
        #: because a session's reader took stock before they arrived.
        self.unmatched = 0

    def feed(self, event: tuple) -> list[PlanStep]:
        kind, plan = event[0], event[1]
        if kind == PLANNED:
            return self._planned(event)
        record = self._open.get(plan)
        if record is None:
            self._hold(plan, event)
            return []
        step, at = record
        if kind == STARTED:
            step = replace(step, started=True, first_apply_delay_s=max(0.0, event[2] - at))
            self._open[plan] = (step, at)
            return [step]
        del self._open[plan]
        return [replace(step, ended=True, outcome=event[2], skip_reason=event[3])]

    def close_open(self) -> list[PlanStep]:
        """The plans not ended yet, which this join stops waiting for."""

        steps = [step for step, _at in self._open.values()]
        self._open.clear()
        return steps

    def _planned(self, event: tuple) -> list[PlanStep]:
        _kind, plan, seq, family, authored, source, slug, at, skip_reason = event
        step = PlanStep(plan_id=plan, seq=seq, family=family, family_authored=authored,
                        family_source=source, slug=slug)
        if skip_reason:
            return [replace(step, ended=True, outcome=SKIPPED, skip_reason=skip_reason)]
        self._open[plan] = (step, at)
        if len(self._open) > _MAX_OPEN:
            # Its ended record was lost, so nothing else will ever close it.
            del self._open[next(iter(self._open))]
        steps = [step]
        for held in self._held.pop(plan, ()):
            steps.extend(self.feed(held))
        return steps

    def _hold(self, plan: int, event: tuple) -> None:
        self._held.setdefault(plan, []).append(event)
        if len(self._held) > _MAX_HELD:
            self.unmatched += len(self._held.pop(next(iter(self._held))))


class PlanEventReader:
    """A queue on the stream and a join behind it."""

    def __init__(self, stream: PlanEventStream, capacity: int = READER_CAPACITY) -> None:
        self._queue = stream.subscribe(capacity)
        self._join = PlanJoin()

    @property
    def capacity(self) -> int:
        return self._queue.capacity

    @property
    def dropped(self) -> int:
        """Records the queue could not hold."""

        return self._queue.dropped

    @property
    def unmatched(self) -> int:
        """Records given up on, because the ``planned`` of their plan never came."""

        return self._join.unmatched

    def drain(self) -> list[PlanStep]:
        steps: list[PlanStep] = []
        for event in self._queue.drain():
            steps.extend(self._join.feed(event))
        return steps

    def close_open(self) -> list[PlanStep]:
        return self._join.close_open()


class SessionPlanCounts:
    """How a session's sentences went, by ``(family, outcome)``, in memory until it ends."""

    def __init__(self, stream: PlanEventStream, capacity: int = SESSION_CAPACITY) -> None:
        self._reader = PlanEventReader(stream, capacity)
        self._lost_seen = 0

    def take(self) -> dict[tuple[str, str], int]:
        """Everything since the last call, and start again. A session calls it at each end."""

        counts: dict[tuple[str, str], int] = {}
        for step in self._reader.drain():
            if step.ended:
                _count(counts, _performed(step), step.outcome)
        for step in self._reader.close_open():
            _count(counts, _performed(step), UNFINISHED)
        lost = self._reader.dropped - self._lost_seen
        if lost > 0:
            counts[("", LOST)] = lost
            self._lost_seen += lost
        return counts


def _performed(step: PlanStep) -> str:
    return step.family if step.started and step.outcome != SKIPPED else NOT_PERFORMED


def _count(counts: dict[tuple[str, str], int], family: str, outcome: str) -> None:
    counts[(family, outcome)] = counts.get((family, outcome), 0) + 1
