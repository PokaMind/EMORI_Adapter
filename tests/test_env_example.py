"""`.env.example` must not itself satisfy an on-robot measurement attestation."""

from __future__ import annotations

import re
from pathlib import Path


_ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _assignments(name: str) -> list[str]:
    """Every uncommented `NAME=value` assignment in .env.example."""

    pattern = re.compile(rf"^\s*{re.escape(name)}=(.*)$")
    values: list[str] = []
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = pattern.match(line)
        if match is not None:
            values.append(match.group(1).strip())
    return values


def test_shipped_env_would_pass_the_gesture_readycheck_gate():
    """A copied .env.example must not be a robot that reads head turns the wrong way.

    push_turn and EMORI_GESTURE_INVERT are one physical fact on this firmware
    (an operator LEFT reads d_roll=+0.236), and readycheck now fails a robot that
    sets the first without the second.  This file is what an installer copies.
    """

    assert _assignments("EMORI_GESTURE_MODE") == ["push_turn"]
    assert _assignments("EMORI_GESTURE_INVERT") == ["1"]


def test_add_on_discovery_is_documented_and_ships_unset():
    assert _assignments("EMORI_ADDONS") == []
    assert "EMORI_ADDONS" in _ENV_EXAMPLE.read_text(encoding="utf-8")


def test_the_language_toggle_ships_off_like_the_code_default():
    """An armed toggle holds every lone directional head turn for 2.5 s."""

    assert _assignments("EMORI_LANG_TOGGLE") == ["0"]


def test_the_on_rig_tuning_knobs_are_documented_where_they_are_tuned():
    """A knob that exists only in a source file is a knob nobody on the robot has."""

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")

    for name in (
        # the menu mic's end-of-speech flush (server._menu_tuning -> pages._STT_JS).
        # Room timing: how long a hesitant visitor pauses
        # mid-phrase and how loud the site's impulse noise is are not code questions.
        "EMORI_MENU_EOS_SILENCE_MS",
        "EMORI_MENU_EOS_MIN_VOICED_MS",
        "EMORI_MENU_FLUSH_MS",
        # the QR/badge + session-start fail-safes (identity.py / server.py / driver.py) -- all four are site data or room timing, and a wrong
        # value in any of them can wedge a badge scan without a word
        "EMORI_PATHWAYS",
        "EMORI_SESSION_START_MS",
        "EMORI_SESSION_START_S",
        # journal verbosity (logging_setup.py, applied by BOTH deployed units'
        # main(): hostapp/server.py and admin/server.py) -- the knob that decides
        # whether the appliance's only debugging surface carries the INFO
        # diagnostics at all
        "EMORI_LOG_LEVEL",
        # ...and the separate wire knob, which is the one an operator must be
        # able to read about BEFORE setting it: it is the only way to get
        # full URLs, tokens, frame contents, synthesized speech and
        # uvicorn's query-string-bearing access log into journald.
        "EMORI_LOG_WIRE",
        # gesture trajectory-shape guards (gestures.py / driver.py)
        "EMORI_GESTURE_SETTLE_EPSILON_RAD",
        "EMORI_GESTURE_SETTLE_TICKS",
        "EMORI_GESTURE_QUIET_EPSILON_RAD",
        "EMORI_GESTURE_QUIET_TICKS",
        "EMORI_GESTURE_BACK_EXIT",
        "EMORI_GESTURE_BACK_THRESHOLD_RAD",
        "EMORI_GESTURE_BACK_HOLD_TICKS",
        "EMORI_GESTURE_DEBUG",
    ):
        assert name in text, f"{name} is tunable in code but undocumented in .env.example"


def test_the_ros4hri_cue_and_skill_knobs_are_documented():
    """The ros4hri cue and skill lane reads ten variables; a robot needs all ten.

    Three of them are the ones an operator reaches for under pressure and cannot guess:
    EMORI_CUE_MOTION is the kill-switch for per-sentence head motion, EMORI_CUE_Z_MM is
    the clamp on a translation axis (measured on a Reachy Mini Wireless, to be
    bisected on any other robot), and
    EMORI_ROS_MOTION_OWNER is the one knob that decides which lane may move the robot
    at all -- the double-drive trap in a single word.
    """

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")

    for name in (
        # per-sentence expression cues (behavior/cues.py, read in LiveDriver.__init__)
        "EMORI_CUE_MOTION",
        "EMORI_CUE_MAX_S",
        "EMORI_CUE_Z_MM",
        "EMORI_CUE_BODY_YAW",
        "EMORI_CUE_DEESCALATE_AT",
        # the ROS graph's only motion intake (hostapp/skill_api.py)
        "EMORI_SKILL_API",
        "EMORI_SKILL_KEY",
        "EMORI_SKILL_MIN_INTERVAL_S",
        # which lane submits, and whether the container runs at all
        "EMORI_ROS_MOTION_OWNER",
        "EMORI_WITH_ROS2",
    ):
        assert name in text, f"{name} is read in code but undocumented in .env.example"


def test_the_motion_owner_flip_documents_every_knob_it_added():
    """The motion-owner switch adds six more, and five of them are read INSIDE the
    container -- which is precisely why they need to be here.

    A knob that only exists in a compose file's ``${VAR:-default}`` is a knob an
    operator finds by reading YAML, and the two that matter most are not settings at
    all but postures: EMORI_ROS_DEIDENTIFY decides whether a real learner id goes onto
    a DDS domain whose only access control is ROS_LOCALHOST_ONLY, and
    EMORI_ROS_OWNER_FALLBACK_S decides whether a wedged ROS graph fails visibly (a
    still robot) or invisibly (the hostapp quietly resumes driving).
    """

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")

    for name in (
        # the hostapp's dead-man's handle on EMORI_ROS_MOTION_OWNER=ros (driver.py)
        "EMORI_ROS_OWNER_FALLBACK_S",
        # the host path bind-mounted over the container's /emori/configs/behavior.yaml
        "EMORI_ROS_BEHAVIOR_CONFIG_HOST",
        # what leaves the graph on DDS (emori_adapter_ros/privacy.py)
        "EMORI_ROS_DEIDENTIFY",
        "EMORI_ROS_SPEECH_TEXT",
        "EMORI_ROS_ID_SALT",
        # the facade's transport plug point (emori_skill_facade/transport.py)
        "EMORI_SKILL_TRANSPORT",
    ):
        assert name in text, f"{name} is read in code but undocumented in .env.example"


def test_the_owner_fallback_ships_off():
    """A shipped fallback window would make every owner=ros robot a hostapp robot after N
    seconds of ROS silence, without anyone having decided that."""

    assert [v for v in _assignments("EMORI_ROS_OWNER_FALLBACK_S") if v not in ("", "0")] == []


def test_the_skill_intake_ships_off_and_keyless():
    """A copied .env.example must never be a robot with an open motion intake.

    Both halves matter: an uncommented EMORI_SKILL_API=1 would open the route on every
    robot an installer configures from this file, and a shipped EMORI_SKILL_KEY would
    be a shared secret identical on every robot and published in the repository.
    """

    assert [value for value in _assignments("EMORI_SKILL_API") if value != "0"] == []
    assert [value for value in _assignments("EMORI_SKILL_KEY") if value] == []


def test_the_gesture_trace_ships_OFF_and_says_what_it_costs():
    """A shipped `1` is ~400 WARNING lines a day on a card measured 94% full.

    A trace left on from a debugging session survives when this file only COMMENTS
    the variable out -- a .env copied from an older file inherits whatever it had. An explicit
    assignment is what makes the shipped answer the shipped answer.
    """

    assert _assignments("EMORI_GESTURE_DEBUG") == ["0"]

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    block = text[text.index("EMORI_GESTURE_DEBUG") - 500:text.index("EMORI_GESTURE_DEBUG")]
    # The cost, not just the switch: WARNING level, a line volume, and the disk.
    assert "WARNING" in block
    assert "400 lines a day" in block
    assert "94%" in block


def test_the_residual_rollback_is_documented_as_a_rollback():
    """EMORI_GESTURE_RESIDUAL=0 is the single revert for the largest behavioural change
    in the v2 face look-at work, and it reverts THREE shipped consumers together.

    An operator who finds only "residual sensing: 1/0" here cannot know that flipping it
    also stops the cue lane planning, re-arms the skill intake's 423 and stands the idle
    scan down -- so the blast radius is written where the switch is.
    """

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")

    assert "#EMORI_GESTURE_RESIDUAL=1" in text
    # The comment BLOCK above the assignment: that is where an operator reads.
    block = text[:text.index("#EMORI_GESTURE_RESIDUAL=1")]
    block = block[block.index("EMORI_GESTURE_DEBUG=0"):]
    assert "ROLLBACK" in block
    for consumer in ("cues", "skill intake", "idle scan"):
        assert consumer in block, consumer
    # ...and it ships at the code default, unset, so copying this file changes nothing.
    assert [v for v in _assignments("EMORI_GESTURE_RESIDUAL") if v] == []


def test_the_v2_face_lookat_knobs_are_documented_where_an_operator_looks():
    """Four variables an operator needs and cannot guess: two read inside the container,
    one that readycheck reads, and one that is the camera rollback."""

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")

    for name in (
        # the capability switch and the model path, read by the ROS robot node
        "EMORI_GAZE",
        "EMORI_GAZE_MODEL",
        # the override of the tee device readycheck's gaze gate otherwise reads from
        # /etc/default/emori-camera
        "EMORI_READYCHECK_ROS_VIDEO_DEVICE",
        # the camera-side rollback: empty it and the bridge tees nothing
        "EMORI_CAM_ROS_VIDEO_NR",
        # how the head moves to the face and how other moves are aimed at it; read by
        # the node, substituted by the robot compose file from deploy/ros2/.env
        "EMORI_GAZE_SERVO_HZ",
        "EMORI_GAZE_ANCHOR",
        "EMORI_GAZE_ANCHOR_RECORDED",
        "EMORI_RECORDED_BLEND_S",
    ):
        assert name in text, f"{name} decides robot behaviour but is undocumented in .env.example"

    # ...and they say where they are READ from, because it is not this file: compose
    # substitutes the robot profile from the .env beside it, and a knob set in
    # /opt/emori/.env is a knob that silently does nothing.
    block = text[text.index("#EMORI_GAZE_DETECT_HZ="):text.index("#EMORI_GAZE_MODEL=")]
    assert "deploy/ros2/.env" in block
    for name in ("EMORI_GAZE_SERVO_HZ", "EMORI_GAZE_ANCHOR", "EMORI_GAZE_LANE",
                 "EMORI_GAZE_ANCHOR_RECORDED", "EMORI_RECORDED_BLEND_S"):
        assert f"#{name}=" in block, f"{name} is not documented beside the detector rate"


def test_face_lookat_ships_fail_closed_and_privacy_is_stated_not_assumed():
    """Neither EMORI_GAZE nor the tee is ASSIGNED here.

    Both are commented: a copied .env must not turn a kiosk in a public corridor into a
    running face detector by inheritance. And the one paragraph an operator needs about
    what is and is not published has to be at the switch, not only in docs/.
    """

    assert _assignments("EMORI_GAZE") == []
    assert _assignments("EMORI_CAM_ROS_VIDEO_NR") == []

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    block = text[text.index("#EMORI_GAZE=1") - 1200:text.index("#EMORI_GAZE=1")]
    assert "Fail-closed" in block
    assert "no image, crop, embedding or identity" in block


def test_no_key_is_assigned_twice():
    """python-dotenv AND systemd's EnvironmentFile both take the LAST assignment.

    A second `NAME=` line 294 lines below the documented one silently discards
    whatever the installer configured -- a configured EMORI_ROBOT_NAME becomes ""
    and every results email and dashboard header falls back to the robot slug,
    with nothing anywhere reporting the override.
    """

    seen: dict[str, int] = {}
    for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", name or ""):
            seen[name] = seen.get(name, 0) + 1
    duplicates = sorted(k for k, n in seen.items() if n > 1)
    assert duplicates == [], "assigned more than once: %s" % duplicates


def test_the_robot_token_section_is_appended_last_and_ships_no_token():
    """THIS robot's token lives in a file; the example documents it and assigns nothing.

    Appended as the file's LAST section (the rest of the file is restructured
    elsewhere), and both variables stay commented: a copied .env.example must never be
    a robot with a token -- a token is one robot, and this file is copied onto many.
    """

    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    header = "# ============ THIS robot's Pokamind token (per robot) ============"
    sections = [line for line in text.splitlines() if line.startswith("# ============")]

    assert sections[-1] == header
    block = text[text.index(header):]
    for name in ("EMORI_POKAMIND_TOKEN_FILE", "POKAMIND_ROBOT_TOKEN"):
        assert f"#{name}=" in block, name
        assert _assignments(name) == [], name
    assert "docs/deploy-a-new-robot.md" in block
    assert "pkr1." in block and "AAEC" not in block  # the shape, never a value
    assert (_ENV_EXAMPLE.parent / "docs" / "deploy-a-new-robot.md").is_file()
