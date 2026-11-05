"""What the adapter is allowed to put on the DDS wire, and what it must not.

``/ws/relay`` broadcasts the verbatim stamped envelope: the roster student id, the
minted session id, and the sentence the robot is about to say. The publisher copies
those straight into ``emori_msgs/Context`` and ``emori_msgs/SpeechCue``, and on the
robot the only thing between that wire and the rest of the network is
``ROS_LOCALHOST_ONLY=1`` -- a networking setting, not a data-protection control. Any
node that joins the domain (ours, a partner's, a laptop plugged into the robot for an
afternoon) reads every field of every message.

So this module is the gate. Identifying fields are pseudonymised by default and the
robot's own speech text is withheld by default; both are knobs, so a research capture
that genuinely needs the text turns it on *deliberately* rather than by nobody having
thought about it.

What the gate never touches is the part the behaviour lane actually consumes --
``cue_category``, ``est_duration``, ``lang``, ``ended``, ``seq``. Withholding those
would protect no one; it would just stop the head moving while the robot speaks.

Pure stdlib on purpose: this is the half that has to be testable on a machine with no
ROS installed, which is every machine except the container.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass

# Spelled out rather than "anything that is not 0". A privacy control must not be
# switched off by a typo in a compose file, so only these words move a knob and
# anything else (including "flase" and "") falls back to the shipped default.
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

_EPHEMERAL_SALT = ""


def ephemeral_salt() -> str:
    """One random salt for the lifetime of this PROCESS, minted on first use.

    A per-process salt is the safe default and a real limitation at the same time:
    pseudonyms are stable inside a run (so a consumer can group a session's rows) and
    meaningless across a restart (so nobody can join two days of captures). Setting
    ``EMORI_ROS_ID_SALT`` is how you opt into linkability, which is a decision with a
    data-protection answer, not a default.
    """

    global _EPHEMERAL_SALT
    if not _EPHEMERAL_SALT:
        _EPHEMERAL_SALT = secrets.token_hex(16)
    return _EPHEMERAL_SALT


def _flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Read one boolean knob, falling back to ``default`` on anything unrecognised."""

    raw = str(env.get(name, "")).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def pseudonym(value: str, salt: str) -> str:
    """A stable, non-reversible stand-in for one identifier. Empty in, empty out.

    The empty case is deliberate: "the hostapp stamped no id" is information, and
    hashing ``""`` would turn it into a plausible-looking pseudonym that a consumer
    would happily group rows under, inventing a person who was never there.

    Truncated to 16 hex characters (64 bits) because the population this has to stay
    unique across is one learner at a time on one robot, and the result travels as a
    topic-sized string that a human has to be able to read back off a log line.
    """

    if not value:
        return ""
    digest = hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()
    return "p-" + digest[:16]


@dataclass(frozen=True)
class Privacy:
    """The effective privacy posture of one publisher process.

    Frozen because it is read once at node start and must not drift mid-session: a
    capture in which half the cycles are pseudonymised and half are not is worse than
    either choice made consistently.
    """

    deidentify: bool = True
    speech_text: bool = False
    salt: str = ""
    # True when the salt was minted rather than configured -- the node says so at
    # start, because "the pseudonyms are not linkable across restarts" is the kind of
    # thing that has to be in the journal before anyone plans an analysis around them.
    ephemeral: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Privacy:
        """Build the posture from the container environment (defaults: on, off).

        Defaults are the protective ones. A deployment that wants the raw roster id or
        the robot's speech on DDS has to say so in the compose file, where a reviewer
        can see it.
        """

        source = os.environ if env is None else env
        salt = str(source.get("EMORI_ROS_ID_SALT", "") or "").strip()
        return cls(
            deidentify=_flag(source, "EMORI_ROS_DEIDENTIFY", True),
            speech_text=_flag(source, "EMORI_ROS_SPEECH_TEXT", False),
            salt=salt or ephemeral_salt(),
            ephemeral=not salt,
        )

    def apply_ids(self, person_id: str, session_id: str) -> tuple[str, str]:
        """The pair to write into ``Context``/``SpeechCue``'s identity fields."""

        if not self.deidentify:
            return person_id, session_id
        return pseudonym(person_id, self.salt), pseudonym(session_id, self.salt)

    def apply_speech(self, text: str, cue_body: str) -> tuple[str, str]:
        """The sentence and its cue body, or two empty strings when text is withheld.

        ``cue_body`` travels with the text because it is free-form direction written
        about that same sentence; keeping it while dropping the sentence would leak the
        half nobody remembered to look at.
        """

        if self.speech_text:
            return text, cue_body
        return "", ""

    def describe(self) -> str:
        """One log line: what a reader of the journal needs to interpret a capture."""

        return (f"deidentify={int(self.deidentify)} speech_text={int(self.speech_text)} "
                f"salt={'ephemeral' if self.ephemeral else 'configured'}")
