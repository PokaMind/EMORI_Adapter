"""The synthetic sentences a scripted motion-sync run speaks, and what each one tests.

Every line is made up: plain workplace-training talk with no feeling of its own, so that
when a sentence has a family it is because the case gave it one. The words deliberately
avoid everything the planner reads from a sentence's own text, which is what lets a case
state the family it expects. ``tests/test_motion_sync_run.py`` runs the list through the
real cue scheduler to keep that true.

A case is about one thing: when its event arrives relative to its audio, the gap in front
of it, its length, its family, or what else is going on around it. ``expected_exception``
names the allowed class (E1 to E7) a case is entitled to, or ``None`` when every gate
applies. Nothing here depends on a robot, a network or a sound card.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

LANGS = ("en", "ca")
FAMILIES = ("tension", "levity", "energy", "intimacy", "softening", "release")
EVENT_OFFSETS_MS = (-6400, -3500, -1000, -150, 0, 40, 80, 150)
GAPS_MS = (0, 30, 100, 300)
COMMA_PAUSE_S = 0.7
BREATH_PAUSE_S = 0.4
# How far apart a burst's events land. Three events, so 50 ms from the first to the last.
BURST_STEP_MS = 25.0
# The gap in front of a sentence that opens its own turn.
TURN_GAP_MS = 1000.0
# A sentence whose voice never comes still needs room: the settle plays out in this.
NO_AUDIO_SLOT_S = 3.0
# Where inside a long sentence its analysis cycles land, from the sentence's first sample.
ENGAGEMENT_AT_S = (2.5, 6.0, 9.0)
# A covering line runs this long under the sentence that follows it, as the page's own does
# until the session's first audio hushes it.
COVER_OVERLAP_MS = 300.0

# A part break inside a spoken line: the renderer puts the case's pause there.
PART_BREAK = "|"

_NORMAL = {
    "en": (
        "The new rota goes up on the board every Monday morning.",
        "Please check the stock list before the first delivery arrives.",
        "Each team lead signs the handover sheet at the end of the shift.",
        "The training room is on the second floor, next to the lifts.",
        "We keep spare gloves in the cupboard beside the main door.",
        "Your badge opens the side entrance from seven in the morning.",
        "The printer on this floor needs a new cartridge this week.",
        "Write the room number at the top of every form.",
        "The morning briefing lasts ten minutes and starts at eight.",
        "Trolleys go back to the store room after the last round.",
        "The new schedule lists every task by department.",
        "Lunch breaks are taken in two groups from noon.",
        "The supervisor reviews the checklist at the start of each week.",
        "Visitors sign the book at reception and wear a paper tag.",
        "The linen order goes out on Tuesday and comes back on Thursday.",
        "Keep the corridor clear so the cleaning cart can get through.",
        "The next course starts in the blue room after the break.",
        "Name labels are printed at the front desk on request.",
        "Each ward has its own folder on the shared drive.",
        "The fire doors on this level are checked every month.",
        "New starters shadow a colleague during their first week.",
        "The meeting notes go out by mail the following morning.",
        "Parking permits are renewed at the office in January.",
        "Staff lockers are at the end of the ground floor corridor.",
    ),
    "ca": (
        "La nova graella de torns es penja al tauler cada dilluns al matí.",
        "Si us plau, revisa la llista d'estoc abans que arribi la primera entrega.",
        "Cada cap d'equip signa el full de relleu al final del torn.",
        "La sala de formació és al segon pis, al costat dels ascensors.",
        "Guardem guants de recanvi a l'armari del costat de la porta principal.",
        "La teva targeta obre l'entrada lateral a partir de les set del matí.",
        "La impressora d'aquest pis necessita un cartutx nou aquesta setmana.",
        "Escriu el número d'habitació a la part de dalt de cada formulari.",
        "La reunió del matí dura deu minuts i comença a les vuit.",
        "Els carros tornen al magatzem després de l'última ronda.",
        "El nou horari recull cada tasca per departament.",
        "Les pauses per dinar es fan en dos grups a partir del migdia.",
        "La supervisora revisa la llista de comprovació al principi de cada setmana.",
        "Les visites signen el llibre a recepció i porten una etiqueta de paper.",
        "La comanda de roba surt dimarts i torna dijous.",
        "Mantén el passadís lliure perquè hi pugui passar el carro de neteja.",
        "El proper curs comença a la sala blava després de la pausa.",
        "Les etiquetes amb el nom s'imprimeixen a recepció si les demanes.",
        "Cada planta té la seva carpeta a la unitat compartida.",
        "Les portes tallafocs d'aquest nivell es revisen cada mes.",
        "Les noves incorporacions acompanyen un company durant la primera setmana.",
        "Les notes de la reunió s'envien per correu l'endemà al matí.",
        "Els permisos d'aparcament es renoven a l'oficina el gener.",
        "Les taquilles del personal són al final del passadís de la planta baixa.",
    ),
}

_SPECIAL = {
    "en": {
        "word_a": "Okay.",
        "word_b": "Right.",
        "hum": "Mm.",
        "comma_a": "The first box goes on the top shelf,|the second box goes under the desk.",
        "comma_b": "The blue form goes to the office,|the white copy stays in the folder.",
        "breath_a": "Sign the sheet at the door.|Then take a seat in the second row.",
        "breath_b": "Open the folder on the left.|Then read the first page with your team.",
        "long_a": "The induction runs across three mornings, and on each of them we walk through "
                  "one part of the building, look at the rooms on that floor, and go over who "
                  "to call when a room needs something.",
        "long_b": "When the delivery comes in, one person counts the boxes at the door, another "
                  "writes the numbers on the sheet, and the two of them compare the totals "
                  "before anything goes up to the store room.",
        "long_c": "Every department keeps its own copy of the schedule, so when a shift changes "
                  "the team lead updates the board in the corridor, then the shared folder, and "
                  "then the printed sheet in the staff room.",
        "long_d": "At the end of the week the supervisor collects the forms from each floor, "
                  "checks the room numbers against the list, and files them in the cabinet by "
                  "date so the next team can find them.",
        "cover": "Starting the module. Give me a moment while I set everything up, and we will "
                 "begin as soon as it is ready.",
    },
    "ca": {
        "word_a": "D'acord.",
        "word_b": "Molt bé.",
        "hum": "Mm.",
        "comma_a": "La primera caixa va al prestatge de dalt,|la segona caixa va sota la taula.",
        "comma_b": "El full blau va a l'oficina,|la còpia blanca queda a la carpeta.",
        "breath_a": "Signa el full a la porta.|Després seu a la segona fila.",
        "breath_b": "Obre la carpeta de l'esquerra.|Després llegeix la primera pàgina amb l'equip.",
        "long_a": "La formació inicial dura tres matins, i a cada un recorrem una part de "
                  "l'edifici, mirem les sales d'aquell pis i repassem a qui cal trucar quan una "
                  "sala necessita alguna cosa.",
        "long_b": "Quan arriba l'entrega, una persona compta les caixes a la porta, una altra "
                  "apunta els números al full, i totes dues comparen els totals abans de pujar "
                  "res al magatzem.",
        "long_c": "Cada departament té la seva còpia de l'horari, de manera que quan canvia un "
                  "torn el cap d'equip actualitza el tauler del passadís, després la carpeta "
                  "compartida i després el full imprès de la sala del personal.",
        "long_d": "Al final de la setmana la supervisora recull els formularis de cada pis, "
                  "comprova els números d'habitació amb la llista i els arxiva per data perquè "
                  "el següent equip els pugui trobar.",
        "cover": "Comencem el mòdul. Dona'm un moment mentre ho preparo tot, i començarem tan "
                 "bon punt estigui a punt.",
    },
}


@dataclass(frozen=True)
class Sentence:
    """One entry of the running order, in one language."""

    case: str
    # What the speaker says, split at the case's pause. Empty for a sentence with no audio.
    parts: tuple[str, ...]
    # What the event carries: the page sends the spoken text with its cue brackets removed,
    # which is empty for a sentence that was only a cue.
    text: str
    family: Optional[str] = None
    cue_body: Optional[str] = None
    expect_family: Optional[str] = None
    audio: bool = True
    # False: audio with no event of its own (the covering line).
    posted: bool = True
    # The page tells the hostapp when this line starts and ends.
    cover: bool = False
    # The silence in front of this sentence. Negative mixes it under the previous one.
    gap_ms: float = TURN_GAP_MS
    event_offset_ms: float = 0.0
    pause_s: float = 0.0
    source: str = "turn"
    # The page posts pokamind_tts_ended after the last sentence of a reply.
    turn_end: bool = True
    engagement_at_s: tuple[float, ...] = ()
    # Position inside a burst of events: None, or 0, 1, 2.
    burst: Optional[int] = None
    expected_exception: Optional[str] = None
    # Filled in by :func:`build` for every sentence that posts an event.
    seq: int = -1


def _parts(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(PART_BREAK))


def _posted(text: str) -> str:
    return " ".join(_parts(text))


def build(lang: str) -> tuple[Sentence, ...]:
    """The running order for ``lang``. The same cases, in the same order, in every language."""

    if lang not in LANGS:
        raise ValueError(f"no fixture for language {lang!r}")
    pool = _NORMAL[lang]
    special = _SPECIAL[lang]
    used = 0

    def line() -> str:
        nonlocal used
        text = pool[used % len(pool)]
        used += 1
        return text

    def say(case: str, text: Optional[str] = None, **fields) -> Sentence:
        spoken = line() if text is None else text
        return Sentence(case=case, parts=_parts(spoken), text=_posted(spoken), **fields)

    order: list[Sentence] = []
    # Every delivery offset, twice. Each sentence stands alone in its own turn: an early
    # event gets enough silence in front of it to arrive after the previous sentence ended.
    for offset in EVENT_OFFSETS_MS:
        for take in ("a", "b"):
            order.append(say(f"offset_{offset:+d}_{take}", event_offset_ms=float(offset)))
    # Every gap between two sentences of one reply.
    for gap in GAPS_MS:
        for take in ("a", "b"):
            order.append(say(f"gap_{gap}_{take}_first", turn_end=False))
            order.append(say(f"gap_{gap}_{take}_second", gap_ms=float(gap)))
    # Lengths and pauses. A single word is shorter than a plan's first move: E5.
    order.append(say("one_word_a", special["word_a"], expected_exception="E5"))
    order.append(say("one_word_b", special["word_b"], expected_exception="E5"))
    order.append(say("normal_a"))
    order.append(say("normal_b"))
    order.append(say("long_a", special["long_a"]))
    order.append(say("long_b", special["long_b"]))
    order.append(say("comma_pause_a", special["comma_a"], pause_s=COMMA_PAUSE_S))
    order.append(say("comma_pause_b", special["comma_b"], pause_s=COMMA_PAUSE_S))
    order.append(say("breath_a", special["breath_a"], pause_s=BREATH_PAUSE_S))
    order.append(say("breath_b", special["breath_b"], pause_s=BREATH_PAUSE_S))
    # Every family, twice, recordings allowed.
    for family in FAMILIES:
        for take in ("a", "b"):
            order.append(say(f"family_{family}_{take}", family=family, expect_family=family))
    # A sentence that outlasts its plan has to stay in its family to its last word.
    order.append(say("family_long_a", special["long_a"], family="tension",
                     expect_family="tension"))
    order.append(say("family_long_b", special["long_b"], family="intimacy",
                     expect_family="intimacy"))
    # No family at all, then a family carried across a reply past its decay into a tint.
    order.append(say("no_family_a"))
    order.append(say("no_family_b"))
    order.append(say("carry_authored", family="energy", expect_family="energy", turn_end=False))
    for depth in (1, 2, 3):
        order.append(say(f"carried_{depth}", expect_family="energy", gap_ms=100.0,
                         turn_end=depth == 3))
    # A direction that names another family outranks the category it came with.
    order.append(say("body_forces_tension", family="levity",
                     cue_body="in a nervous, uneasy voice", expect_family="tension"))
    order.append(say("body_forces_softening", family="tension",
                     cue_body="warm and reassuring", expect_family="softening"))
    # The held pose outlasts the wait for it, so the next sentence comes straight after.
    for take in ("a", "b"):
        order.append(say(f"firm_hold_{take}", family="tension", cue_body="firm and serious",
                         expect_family="tension", turn_end=False))
        order.append(say(f"firm_successor_{take}", expect_family="tension", gap_ms=100.0))
    # A sentence that is only a cue: its text is empty, and with or without a sound the
    # family belongs to the sentence after it.
    order.append(Sentence(case="cue_only_audio", parts=_parts(special["hum"]), text="",
                          family="release", expect_family="release", turn_end=False))
    order.append(say("cue_only_audio_next", expect_family="release", gap_ms=100.0))
    order.append(Sentence(case="cue_only_silent", parts=(), text="", family="levity",
                          expect_family="levity", audio=False, turn_end=False,
                          expected_exception="E6"))
    order.append(say("cue_only_silent_next", expect_family="levity", gap_ms=100.0))
    # The event arrives and the voice never does: only the settle may move.
    for take in ("a", "b"):
        order.append(Sentence(case=f"no_audio_{take}", parts=(), text=line(), audio=False,
                              expected_exception="E6"))
    # Three events inside 50 ms for three sentences that play one after another. The page plays
    # sentences strictly in turn and only does this after one fails, so these are kept out of
    # the timing figures: E8.
    for take in ("a", "b"):
        for position in (0, 1, 2):
            order.append(say(f"burst_{take}_{position}", burst=position,
                             gap_ms=TURN_GAP_MS if position == 0 else 0.0,
                             turn_end=position == 2, expected_exception="E8"))
    # Analysis cycles land while a long sentence is being spoken.
    order.append(say("engagement_inside_a", special["long_c"], engagement_at_s=ENGAGEMENT_AT_S))
    order.append(say("engagement_inside_b", special["long_d"], engagement_at_s=ENGAGEMENT_AT_S))
    # The page's covering line is still on the speaker when the session's first sentence
    # arrives, so the event comes while other audio is playing.
    order.append(Sentence(case="covering_line", parts=_parts(special["cover"]),
                          text=_posted(special["cover"]), posted=False, turn_end=False,
                          cover=True))
    # Nothing can tell its sound from the line's, so it moves when the line ends: E8.
    order.append(say("covering_overlap_first", gap_ms=-COVER_OVERLAP_MS, source="scenario",
                     turn_end=False, event_offset_ms=-1000.0, expected_exception="E8"))
    order.append(say("covering_overlap_second", gap_ms=100.0, source="scenario"))

    numbered = []
    seq = 0
    for sentence in order:
        if sentence.posted:
            seq += 1
            sentence = replace(sentence, seq=seq)
        numbered.append(sentence)
    return tuple(numbered)
