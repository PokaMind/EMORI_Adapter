#!/usr/bin/env python3
"""Write down the environment systemd gave this process, for the hub container to read.

``emori-hub.service`` lists the same environment files the host application's unit does, and
systemd parses them: quoting, ``export`` prefixes, line continuations. The container cannot
reproduce that parser, and Compose's ``env_file`` is a third dialect. So this runs as a start
step of the unit, inherits what systemd parsed, and writes it as JSON to a file in a
directory the container mounts read-only. ``emori_hub.main`` loads it before the adapter
core is imported, leaving out the names that describe the host's process
(``emori_hub.main.ENV_DENYLIST``), so the order stays: what systemd set first, ``.env``
filling in only what is missing.

Standard library only: this runs under the host's system Python. The file holds secrets, so
it is created with mode 0600 and replaced atomically; the unit's runtime directory is 0700
and is removed when the unit stops.

The file also gets a ``graph`` block: what decides what leaves on DDS, and what the graph's
nodes read when they run in the hub, taken from ``deploy/ros2/.env``, the file Compose reads for
the motor container and the one the documentation names for these keys. The values keep
Compose's meaning, so an empty value is the default. ``emori_hub.main`` gives the block
precedence over the host's files.

Usage: ``env-snapshot.py [OUTPUT]`` (default ``/run/emori-hub/env.json``).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any, Mapping

DEFAULT_OUTPUT = "/run/emori-hub/env.json"
#: The motor container's Compose settings, beside this directory.
MOTOR_ENV_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ros2", ".env")

GRAPH_BLOCK = "graph"
#: What goes in the block, each with the default the motor's Compose file gives it.
GRAPH_DEFAULTS = {
    "EMORI_ROS_DEIDENTIFY": "1",
    "EMORI_ROS_SPEECH_TEXT": "0",
    "EMORI_ROS_ID_SALT": "",
    "EMORI_ROS_BEHAVIOR_CONFIG_HOST": "/opt/emori/configs/behavior.yaml",
}

#: Where the host puts the voice-delay sink, and the setting that names the tap's target.
#: ``emori_adapter.hostapp.voice_link.VOICE_DELAY_SINK`` is the same name.
VOICE_DELAY_SINK = "emori_voice"
VOICE_DELAY_VAR = "EMORI_VOICE_DELAY_MS"
VOICE_SINK_VAR = "EMORI_VOICE_LINK_SINK"


def voice_delay_on(environ: Mapping[str, str]) -> bool:
    """A positive whole number of milliseconds, as the voice tap reads it."""

    try:
        return int((environ.get(VOICE_DELAY_VAR) or "").strip() or 0) > 0
    except ValueError:
        return False


def parse_env_file(text: str) -> dict[str, str]:
    """``NAME=value`` lines as Compose reads its ``.env``: ``export``, quotes, comments.

    The last line for a name wins. A line that is none of these is skipped.
    """

    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not name or name.startswith("#"):
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            comment = value.find(" #")
            value = (value[:comment] if comment >= 0 else value).rstrip()
        found[name] = value
    return found


def graph_block(path: str) -> dict[str, str]:
    """The graph's values from the motor's env file. Values are never printed.

    Compose goes without the file, so a robot that has none gets the defaults. So does one
    whose file cannot be read, which says so: the defaults are the closed ones, and a robot
    that does not start over this would be worse.
    """

    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            given = parse_env_file(handle.read())
    except FileNotFoundError:
        given = {}
    except OSError as error:
        print(f"env-snapshot: cannot read {path}, the graph gets its defaults: "
              f"{error.strerror}", file=sys.stderr)
        given = {}
    return {name: given.get(name) or default for name, default in GRAPH_DEFAULTS.items()}


def snapshot(environ: Mapping[str, str], motor_env: str = MOTOR_ENV_FILE) -> dict[str, Any]:
    """What the container is given: the environment, plus what only the host can say.

    With a voice delay set, the kiosk plays into a sink that exists only on the host, and
    the tap has to listen to that one. The container cannot create it, so it is named here,
    from the one setting that turns the delay on. A value the operator set is kept.
    """

    body: dict[str, Any] = {str(name): str(value) for name, value in environ.items()}
    if voice_delay_on(body) and not body.get(VOICE_SINK_VAR):
        body[VOICE_SINK_VAR] = VOICE_DELAY_SINK
    body[GRAPH_BLOCK] = graph_block(motor_env)
    return body


def write_atomically(path: str, body: Mapping[str, Any]) -> None:
    """Create ``path`` with mode 0600, whole or not at all."""

    directory = os.path.dirname(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(prefix=".env-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(body, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main(argv: list[str], environ: Mapping[str, str] | None = None,
         motor_env: str = MOTOR_ENV_FILE) -> int:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] in ("-h", "--help")):
        print(__doc__, file=sys.stderr)
        return 2
    path = argv[1] if len(argv) == 2 else DEFAULT_OUTPUT
    try:
        write_atomically(path, snapshot(os.environ if environ is None else environ, motor_env))
    except OSError as error:
        print(f"env-snapshot: cannot write {path}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
