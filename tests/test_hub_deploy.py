"""The hub's deployment files (``deploy/hub``), checked as text and, where a shell is at hand, run.

No container runtime and no ROS in the test run, so most of this reads the files the robot
will be given and holds them to what the hub depends on:

* the container starts as the robot's user with no capabilities, no core files, a private
  IPC namespace and no log driver, and every bind source it needs must already exist, because
  with the long mount syntax Compose refuses a missing source and with the short one Docker
  would make it a root-owned directory;
* the unit has no ``Conflicts=`` against the host application's unit, makes the cache
  directory the container mounts, orders ``--ansi`` before ``up``, and reads the same four
  environment files the host application's unit does;
* the environment snapshot, the interpreter's path file and the four environment gates
  behave as described in their own docstrings;
* nothing in the directory carries a planning identifier, a date or the name of anything
  closed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_HUB_DIR = _ROOT / "deploy" / "hub"
_HUB_PACKAGE = _ROOT / "ros2_ws" / "src" / "emori" / "emori_hub"
if str(_HUB_PACKAGE) not in sys.path:
    sys.path.insert(0, str(_HUB_PACKAGE))

from emori_hub import main as hub_main  # noqa: E402

_COMPOSE = _HUB_DIR / "docker-compose.hub.robot.yml"
_UNIT = _HUB_DIR / "emori-hub.service"
_HOSTAPP_UNIT = _ROOT / "deploy" / "robot" / "emori-hostapp.service"
_V2_COMPOSE = _ROOT / "deploy" / "ros2" / "docker-compose.v2.robot.yml"
_BASE = _HUB_DIR / "Dockerfile.hub-base"
_IMAGE = _HUB_DIR / "Dockerfile.hub"
_CONSTRAINTS = _HUB_DIR / "constraints-hub.txt"
_PATH_FILE = _HUB_DIR / "emori-hub-site.pth"
_PEERS = _HUB_DIR / "fastdds_peers.xml"
_ENTRY = _HUB_DIR / "hub-entry.sh"
_EXPECTED = _HUB_DIR / "env_expected.txt"
_SHELL = shutil.which("sh") or shutil.which("bash")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


snap = _load("hub_env_snapshot", _HUB_DIR / "env-snapshot.py")
gates = _load("hub_env_gates", _HUB_DIR / "env_gates.py")


def _compose() -> dict:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]["hub"]


def _unit_lines(path: Path) -> list[tuple[str, str]]:
    """Every ``Key=value`` of a unit, in order, comments left out. Keys repeat."""

    pairs = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        pairs.append((key, value))
    return pairs


def _values(path: Path, key: str) -> list[str]:
    return [value for name, value in _unit_lines(path) if name == key]


def test_with_the_flag_down_the_hub_unit_holds_nothing_up_behind_docker():
    """A start job waits for its order before its condition is read, so After=docker.service would
    keep the kiosk, ordered after this unit, waiting on a Docker it does not need."""
    assert "docker.service" not in " ".join(_values(_UNIT, "After"))
    assert "docker.service" in " ".join(_values(_UNIT, "Wants"))
    assert "docker info" in _values(_UNIT, "ExecStartPre")[0]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _directives(path: Path) -> str:
    return " ".join(line for line in _text(path).splitlines()
                    if line.strip() and not line.lstrip().startswith("#"))


# --- the images ------------------------------------------------------------------------


def test_the_base_builds_the_pinned_environment_without_pulling_anything():
    steps = _directives(_BASE)
    assert "--mount=type=bind,source=debs,target=/p/debs" in steps
    assert "--mount=type=bind,source=wheels,target=/p/wheels" in steps
    for flag in ("--without-pip", "--system-site-packages", "--no-index", "--no-deps",
                 "--no-cache-dir", "-r /p/wheels/constraints-hub.txt"):
        assert flag in steps, flag
    assert "/opt/emori-hub/venv" in steps
    # The base starts from the motor image, which the motor compose file names.
    motor = yaml.safe_load(_text(_V2_COMPOSE))["services"]["ros2v2"]["image"]
    assert f"ARG MOTOR_IMAGE={motor}" in _text(_BASE)


def test_the_hub_image_is_copies_only_and_the_entrypoint_is_executable():
    steps = [line.strip() for line in _text(_IMAGE).splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert not [line for line in steps if line.startswith("RUN")]
    assert "COPY --chmod=755 hub/hub-entry.sh /usr/local/bin/hub-entry.sh" in steps
    assert "COPY --chmod=755 hub/pw-record.sh /usr/local/bin/pw-record" in steps
    assert 'ENTRYPOINT ["/usr/local/bin/hub-entry.sh"]' in steps
    assert any(line.startswith("LABEL org.pokamind.emori.hub-source=") for line in steps)
    # What it copies is what the compose file points the process at.
    assert "COPY hub/fastdds_peers.xml /etc/emori/fastdds_peers.xml" in steps
    assert _compose()["environment"].count("FASTRTPS_DEFAULT_PROFILES_FILE="
                                           "/etc/emori/fastdds_peers.xml") == 1
    # The image the compose file runs is the one the hub base feeds, by tag.
    assert "ARG HUB_BASE=emori/hub-base:robot" in steps
    assert _compose()["image"] == "emori/hub:robot"


def test_every_package_is_pinned_exactly_once_and_the_core_ones_are_there():
    names = {}
    for raw in _text(_CONSTRAINTS).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.\-]+)==([0-9][A-Za-z0-9.\-+]*)", line)
        assert match, line
        key = match.group(1).lower().replace("_", "-")
        assert key not in names, f"{key} pinned twice"
        names[key] = match.group(2)
    for required in ("piper-tts", "vosk", "fastapi", "uvicorn", "wsproto", "h11", "pyjwt",
                     "cryptography", "numpy", "pyyaml", "python-dotenv", "zxing-cpp", "pillow"):
        assert required in names, required
    # The event loop and parser the host application uses; others would change behaviour.
    assert "uvloop" not in names and "httptools" not in names


def test_the_system_packages_include_what_the_audio_path_needs():
    wanted = {line.strip() for line in _text(_HUB_DIR / "hub-packages.txt").splitlines()
              if line.strip() and not line.lstrip().startswith("#")}
    assert {"pipewire-bin", "pipewire-alsa", "libportaudio2", "wireplumber"} <= wanted


def test_the_path_file_puts_the_checkout_first_and_adds_nothing_when_the_site_is_absent():
    lines = [line for line in _text(_PATH_FILE).splitlines() if line.startswith("import ")]
    assert len(lines) == 2
    saved = list(sys.path)
    try:
        namespace: dict = {}
        for line in lines:
            exec(line, namespace)  # noqa: S102 - this is how site runs a .pth line
        assert sys.path[0] == "/opt/emori/src"
        assert not any("hub-site" in entry for entry in sys.path)
    finally:
        sys.path[:] = saved
    # The entrypoint asserts the same place.
    assert 'EXPECTED_CORE=/opt/emori/src/' in _text(_ENTRY)


def test_the_peers_profile_stays_on_the_loopback_with_one_fixed_peer():
    root = ET.parse(_PEERS).getroot()
    ns = {"p": "http://www.eprosima.com/XMLSchemas/fastRTPS_Profiles"}
    addresses = [e.text for e in root.findall(".//p:interfaceWhiteList/p:address", ns)]
    assert addresses == ["127.0.0.1"]
    peers = [e.text for e in root.findall(".//p:initialPeersList//p:address", ns)]
    assert peers == ["127.0.0.1"]
    assert root.find(".//p:useBuiltinTransports", ns).text == "false"
    assert root.find(".//p:participant", ns).get("is_default_profile") == "true"


def test_the_entrypoint_masks_sources_ros_then_checks_the_core_before_it_starts_the_hub():
    text = _text(_ENTRY)
    assert text.startswith("#!/bin/bash\n")
    order = [text.index(marker) for marker in (
        "umask 0077", "source /opt/ros/jazzy/setup.bash",
        "source /emori/ros2_ws/install/setup.bash", 'exec "$@"',
        "find_spec", "-m emori_hub.main")]
    assert order == sorted(order)
    assert "exec /opt/emori-hub/venv/bin/python" not in text   # the variable is used
    assert 'exec "$HUB_PYTHON" -m emori_hub.main' in text
    assert "HUB_PYTHON=/opt/emori-hub/venv/bin/python" in text


# --- the compose file ------------------------------------------------------------------


def test_the_container_runs_as_the_robots_user_with_nothing_extra():
    service = _compose()
    assert service["user"] == "1000:1000"
    assert service["network_mode"] == "host"
    assert service["ipc"] == "private"
    assert service["init"] is True
    assert service["restart"] == "no"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["pull_policy"] == "never"
    assert service["working_dir"] == "/opt/emori"
    assert "privileged" not in service and "devices" not in service


def test_a_crash_cannot_write_a_core_file():
    assert _compose()["ulimits"]["core"] == {"soft": 0, "hard": 0}


def test_stdout_goes_to_the_journal_through_the_attached_client_and_not_to_the_card():
    assert _compose()["logging"] == {"driver": "none"}


def test_the_stop_grace_matches_the_unit_and_the_hub_lifecycle():
    grace = int(str(_compose()["stop_grace_period"]).rstrip("s"))
    assert grace == hub_main.STOP_BUDGET_S
    assert int(_values(_UNIT, "TimeoutStopSec")[0]) > grace


def test_the_compose_client_alone_stops_the_container():
    """A second stopper races the client's own teardown, and the client can panic on that race
    and leave a unit that was only asked to stop in the failed state."""

    assert _values(_UNIT, "ExecStop") == []
    assert _values(_UNIT, "SuccessExitStatus") == ["130"]
    # Only the main process is signalled. The plugin takes a second signal as "kill it now", and
    # a log filter that dies with the stop leaves the client exiting with the container still up.
    assert _values(_UNIT, "KillMode") == ["mixed"]


def test_ros_logging_and_discovery_are_set_for_a_container_that_is_not_root():
    env = dict(item.split("=", 1) for item in _compose()["environment"])
    assert env["ROS_LOG_DIR"] == "/tmp/roslog"
    assert "/tmp" in " ".join(_compose()["tmpfs"])
    assert env["ROS_LOCALHOST_ONLY"] == "0"
    assert env["ROS_DOMAIN_ID"] == "42"
    assert env["RMW_IMPLEMENTATION"] == "rmw_fastrtps_cpp"
    assert env["HOME"] == "/home/pollen"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert env["EMORI_HUB_ENV_JSON"] == "/run/emori-hub/env.json"
    # The host application is the one that writes to FIWARE.
    assert env["EMORI_WITH_FIWARE"] == "false" and env["FIWARE_BRIDGE_ENABLED"] == "0"
    # The motor's thread setting is not the hub's.
    assert "OMP_NUM_THREADS" not in env


def test_the_hub_domain_matches_the_motor_container_and_is_in_neither_order_of_oom():
    motor = yaml.safe_load(_text(_V2_COMPOSE))["services"]["ros2v2"]
    hub_env = dict(item.split("=", 1) for item in _compose()["environment"])
    motor_env = dict(item.split("=", 1) for item in motor["environment"])
    assert motor_env["ROS_DOMAIN_ID"].endswith(hub_env["ROS_DOMAIN_ID"] + "}")
    assert motor["oom_score_adj"] < _compose()["oom_score_adj"]
    assert motor["container_name"] != _compose()["container_name"]


def test_every_bind_is_long_form_and_the_ones_that_must_exist_are_not_created():
    volumes = _compose()["volumes"]
    assert all(isinstance(volume, dict) for volume in volumes)
    by_source = {volume["source"]: volume for volume in volumes}
    for source in ("/run/user/1000/pipewire-0", "/run/emori-hub",
                   "/home/pollen/.cache/emori", "/run/emori-hub/resolv.conf",
                   "/run/emori-hub/timezone"):
        assert by_source[source]["bind"] == {"create_host_path": False}, source
    assert by_source["/run/emori-hub"]["read_only"] is True
    assert by_source["/run/emori-hub/resolv.conf"]["target"] == "/etc/resolv.conf"
    # A host can go without /etc/timezone, and Docker refuses a missing source: the container's
    # copy comes from the unit.
    assert by_source["/run/emori-hub/timezone"]["target"] == "/etc/timezone"
    assert "/etc/timezone" not in by_source
    for source in ("/etc/localtime", "/run/emori-hub/timezone"):
        assert by_source[source]["read_only"] is True
    assert by_source["/opt/emori"]["target"] == "/opt/emori"


def test_something_in_the_unit_makes_each_bind_source_before_compose_needs_it():
    pre = _values(_UNIT, "ExecStartPre")
    joined = "\n".join(pre)
    # The cache directory: nothing else makes it before the mount needs it.
    assert "/bin/mkdir -p -m 0700 /home/pollen/.cache/emori" in pre
    # The socket: waited for.
    assert "/opt/emori/deploy/hub/wait-pipewire.sh" in pre
    # resolv.conf and the snapshot, in the directory the unit owns.
    assert "RuntimeDirectory" in dict(_unit_lines(_UNIT))
    assert dict(_unit_lines(_UNIT))["RuntimeDirectory"] == "emori-hub"
    assert "cat /etc/resolv.conf > /run/emori-hub/resolv.conf" in joined
    assert "env-snapshot.py /run/emori-hub/env.json" in joined
    assert "> /run/emori-hub/timezone" in joined
    # In that order: mount sources first, `down` last.
    position = {name: next(i for i, line in enumerate(pre) if name in line)
                for name in ("mkdir", "wait-pipewire", "env-snapshot", "resolv.conf",
                             "/run/emori-hub/timezone", " down ")}
    assert list(position.values()) == sorted(position.values())


@pytest.mark.parametrize("has_file,link,expected", [
    (True, "/usr/share/zoneinfo/Etc/UTC", "Europe/Madrid"),
    (False, "/usr/share/zoneinfo/Etc/UTC", "Etc/UTC"),
    (False, "/etc/localtime", ""),
])
def test_the_units_time_zone_copy_works_with_and_without_the_hosts_file(tmp_path, has_file, link,
                                                                         expected):
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():
        pytest.skip("no bash")
    line = next(line for line in _values(_UNIT, "ExecStartPre") if "/run/emori-hub/timezone" in line)
    script = re.fullmatch(r"/bin/sh -c '(.*)'", line).group(1)
    timezone = tmp_path / "timezone"
    if has_file:
        timezone.write_text("Europe/Madrid\n", encoding="ascii", newline="\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # The stub answers as the link would, wherever this test runs.
    readlink = stubs / "readlink"
    readlink.write_text(f"#!/bin/sh\necho {link}\n", encoding="ascii", newline="\n")
    readlink.chmod(0o755)
    script = (script.replace("/etc/timezone", timezone.as_posix())
              .replace("/run/emori-hub/timezone", (tmp_path / "copy").as_posix()))
    subprocess.run([bash, "-c", script], check=True,
                   env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"})
    assert (tmp_path / "copy").read_text(encoding="ascii").strip() == expected


# --- the units -------------------------------------------------------------------------


def test_there_is_no_conflicts_line_and_the_flag_file_fences_the_unit():
    keys = [key for key, _ in _unit_lines(_UNIT)]
    assert "Conflicts" not in keys
    assert _values(_UNIT, "ConditionPathExists") == ["/opt/emori/HUB_IS_ROS"]
    for path in (_UNIT, _HUB_DIR / "emori-hub-resolv.path", _HUB_DIR / "emori-hub-resolv.service"):
        assert not re.search(r"^\s*Conflicts=", _text(path), re.M), path.name


def test_the_unit_reads_the_same_environment_files_as_the_host_applications_unit():
    assert _values(_UNIT, "EnvironmentFile") == _values(_HOSTAPP_UNIT, "EnvironmentFile")
    assert len(_values(_UNIT, "EnvironmentFile")) == 4
    assert _values(_UNIT, "UMask") == ["0077"]
    assert _values(_UNIT, "User") == ["pollen"]
    assert _values(_UNIT, "WorkingDirectory") == ["/opt/emori"]


def test_ansi_is_a_flag_of_compose_and_not_of_up():
    assert _values(_UNIT, "ExecStart") == ["/opt/emori/deploy/hub/compose-up.sh"]
    start = [_text(_HUB_DIR / "compose-up.sh").partition("exec /usr/bin/docker")[2]
             .replace("\\\n", " ")]
    words = start[0].split()
    assert words.index("--ansi") < words.index("up")
    assert words[words.index("--ansi") + 1] == "never"
    assert "--abort-on-container-exit" in words and "--no-build" in words
    assert words[words.index("--exit-code-from") + 1] in _compose()["container_name"].split("-")[-1:] \
        or words[words.index("--exit-code-from") + 1] == "hub"
    assert str(_COMPOSE.name) in start[0]


def test_the_start_job_stays_open_until_the_gateway_answers():
    assert _values(_UNIT, "ExecStartPost") == ["/opt/emori/deploy/hub/wait-healthz.sh"]
    assert _values(_UNIT, "Restart") == ["on-failure"]
    assert int(_values(_UNIT, "TimeoutStartSec")[0]) >= 120
    assert _values(_UNIT, "StartLimitIntervalSec") == ["0"]


def test_the_unit_is_ordered_after_the_voice_delay_without_requiring_it():
    after = " ".join(_values(_UNIT, "After")).split()
    assert "emori-voice-delay.service" in after
    for key in ("Wants", "Requires", "BindsTo"):
        assert "emori-voice-delay" not in " ".join(_values(_UNIT, key))
    # The unit the sink comes from exists in the tree.
    assert (_ROOT / "deploy" / "robot" / "emori-voice-delay.service").is_file()


def test_the_resolv_path_unit_triggers_the_service_that_copies_the_file():
    path = _text(_HUB_DIR / "emori-hub-resolv.path")
    service = _text(_HUB_DIR / "emori-hub-resolv.service")
    assert "PathChanged=/etc/resolv.conf" in path
    assert "Unit=emori-hub-resolv.service" in path
    assert "Type=oneshot" in service and "User=pollen" in service
    assert "cat /etc/resolv.conf > /run/emori-hub/resolv.conf" in service
    # Rewritten in place (cat >), never replaced, so the bind mount keeps showing it.
    assert "mv " not in service and "ln " not in service


# --- the shell scripts -----------------------------------------------------------------


def test_the_scripts_are_executable_in_the_index_and_use_unix_line_endings():
    scripts = sorted(_HUB_DIR.glob("*.sh"))
    assert {path.name for path in scripts} == {"compose-up.sh", "hub-addons.sh", "hub-build.sh",
                                               "hub-entry.sh", "pw-record.sh",
                                               "hub-refresh.sh", "wait-healthz.sh",
                                               "wait-pipewire.sh"}
    for path in scripts:
        assert b"\r" not in path.read_bytes(), path.name
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available")
    listing = subprocess.run([git, "ls-files", "-s", "--", "deploy/hub/*.sh"], cwd=_ROOT,
                             capture_output=True, text=True, check=False)
    if listing.returncode != 0 or not listing.stdout.strip():
        pytest.skip("not a git checkout")
    modes = {line.split("\t")[1]: line.split()[0] for line in listing.stdout.splitlines()}
    # core.filemode is false on a Windows checkout, so the index is the only record.
    for path in scripts:
        assert modes.get(f"deploy/hub/{path.name}") == "100755", path.name


@pytest.mark.skipif(_SHELL is None, reason="no shell")
@pytest.mark.parametrize("name", ["compose-up.sh", "hub-addons.sh", "hub-build.sh",
                                  "hub-entry.sh", "pw-record.sh",
                                  "hub-refresh.sh", "wait-healthz.sh", "wait-pipewire.sh"])
def test_the_scripts_parse(name):
    result = subprocess.run([_SHELL, "-n", str(_HUB_DIR / name)], capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_the_kiosk_reads_pipewires_list_whole_before_it_looks_for_the_voice_sink():
    """Under pipefail ``pw-cli | grep -q`` reads a sink that is there as missing, and the voice
    then skips its delay with nothing said."""

    text = (_HUB_DIR.parent / "robot" / "start-kiosk.sh").read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert "pw-cli ls Node" in text
    assert not re.search(r"pw-cli[^\n]*\|[^\n]*grep", text)


def test_the_images_pw_record_drops_the_flag_its_version_refuses_and_keeps_the_rest(tmp_path):
    """The voice tap's command, as ``hostapp/voice_link.py`` builds it. The image's pw-record
    exits on ``--raw``, which left the hub with no voice tap."""

    stub = tmp_path / "pw-record"
    stub.write_text('#!/bin/sh\nfor word in "$@"; do printf "%s\\n" "$word"; done\n',
                    encoding="ascii", newline="\n")
    stub.chmod(0o755)
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text((_HUB_DIR / "pw-record.sh").read_text(encoding="utf-8")
                       .replace("/usr/bin/pw-record", stub.as_posix()),
                       encoding="ascii", newline="\n")
    from emori_adapter.hostapp.voice_link import pw_record_command

    command = pw_record_command("a_sink")
    assert "--raw" in command
    result = subprocess.run([_SHELL, str(wrapper), *command[1:]], capture_output=True, text=True,
                            timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [word for word in command[1:] if word != "--raw"]


def test_the_journal_gets_plain_lines_and_the_unit_gets_the_clients_exit_code(tmp_path):
    """Compose begins each log line with an erase-line escape. journalctl shows such a line as
    blob data, and a grep through it finds nothing."""

    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():
        pytest.skip("no bash")
    stub = tmp_path / "docker"
    stub.write_text("#!/bin/bash\n"
                    "printf '\\033[2KINFO one\\n\\033[2KINFO two\\n'\n"
                    "printf '\\033[2KGracefully stopping\\n' >&2\n"
                    "exit 7\n", encoding="ascii", newline="\n")
    stub.chmod(0o755)
    script = tmp_path / "compose-up.sh"
    script.write_text(_text(_HUB_DIR / "compose-up.sh").replace("/usr/bin/docker", stub.as_posix()),
                      encoding="ascii", newline="\n")
    result = subprocess.run([bash, str(script)], capture_output=True, timeout=30, check=False)
    assert result.returncode == 7
    assert sorted(result.stdout.split(b"\n")) == [b"", b"Gracefully stopping", b"INFO one",
                                                  b"INFO two"]


def _run_script(name: str, env: dict, timeout: float = 30.0, script: Path | None = None):
    base = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    base.update(env)
    return subprocess.run([_SHELL, str(script or _HUB_DIR / name)], env=base,
                          capture_output=True, text=True, timeout=timeout, check=False)


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_waiting_for_a_socket_that_never_comes_fails_with_a_message(tmp_path):
    result = _run_script("wait-pipewire.sh", {"XDG_RUNTIME_DIR": tmp_path.as_posix(),
                                              "EMORI_HUB_PIPEWIRE_WAIT_S": "1"})
    assert result.returncode == 1
    assert "pipewire-0 did not appear" in result.stderr


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_a_plain_file_is_not_a_socket(tmp_path):
    (tmp_path / "pipewire-0").write_text("")
    result = _run_script("wait-pipewire.sh", {"XDG_RUNTIME_DIR": tmp_path.as_posix(),
                                              "EMORI_HUB_PIPEWIRE_WAIT_S": "1"})
    assert result.returncode == 1


@pytest.fixture
def pipewire(tmp_path):
    """A runtime directory with a PipeWire socket in it, and the wait script to run on it.

    Where this machine cannot make a Unix socket, the socket is a plain file and the script
    is a copy that tests for existence instead, which leaves everything after the wait
    for the socket as it ships.
    """

    script = _HUB_DIR / "wait-pipewire.sh"
    listener = None
    made = False
    if hasattr(socket, "AF_UNIX"):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(tmp_path / "pipewire-0"))
            made = True
        except OSError:
            listener.close()
            listener = None
    if not made:
        (tmp_path / "pipewire-0").write_text("")
        script = tmp_path / "wait-pipewire.sh"
        script.write_bytes(_text(_HUB_DIR / "wait-pipewire.sh").replace("-S ", "-e ")
                           .encode("utf-8"))
    try:
        yield tmp_path, script
    finally:
        if listener is not None:
            listener.close()


def _fake_pw_cli(directory: Path, node: str) -> str:
    tools = directory / "bin"
    tools.mkdir()
    fake = tools / "pw-cli"
    script = "#!/bin/sh\necho 'node.name = \"" + node + "\"'\n"
    fake.write_bytes(script.encode("utf-8"))
    fake.chmod(0o755)
    return tools.as_posix() + os.pathsep + os.environ.get("PATH", "")


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_with_the_socket_and_no_delay_it_returns_at_once(pipewire):
    directory, script = pipewire
    result = _run_script("", {"XDG_RUNTIME_DIR": directory.as_posix(),
                              "EMORI_ROOT": directory.as_posix()}, script=script)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_a_delay_without_its_sink_starts_anyway_and_says_so(pipewire):
    directory, script = pipewire
    result = _run_script("", {
        "XDG_RUNTIME_DIR": directory.as_posix(), "EMORI_ROOT": directory.as_posix(),
        "EMORI_VOICE_DELAY_MS": "120", "EMORI_HUB_VOICE_SINK_WAIT_S": "1",
        "PATH": _fake_pw_cli(directory, "alsa_output.speaker")}, script=script)
    assert result.returncode == 0
    assert "emori_voice sink is not there" in result.stderr


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_a_delay_from_the_env_file_with_its_sink_present_says_nothing(pipewire):
    directory, script = pipewire
    (directory / ".env").write_text("EMORI_VOICE_DELAY_MS=120\n", encoding="utf-8")
    result = _run_script("", {
        "XDG_RUNTIME_DIR": directory.as_posix(), "EMORI_ROOT": directory.as_posix(),
        "EMORI_HUB_VOICE_SINK_WAIT_S": "1",
        "PATH": _fake_pw_cli(directory, "emori_voice")}, script=script)
    assert result.returncode == 0
    assert result.stderr == ""


@pytest.mark.skipif(_SHELL is None, reason="no shell")
def test_a_delay_that_is_off_never_asks_pipewire_for_the_sink(pipewire):
    directory, script = pipewire
    (directory / ".env").write_text("EMORI_VOICE_DELAY_MS=0\n", encoding="utf-8")
    result = _run_script("", {
        "XDG_RUNTIME_DIR": directory.as_posix(), "EMORI_ROOT": directory.as_posix(),
        "EMORI_HUB_VOICE_SINK_WAIT_S": "1",
        "PATH": _fake_pw_cli(directory, "alsa_output.speaker")}, script=script)
    assert result.returncode == 0
    assert result.stderr == ""



class _Healthy(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200 if self.path == "/healthz" else 404)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.mark.skipif(_SHELL is None or shutil.which("curl") is None, reason="no shell or curl")
def test_the_start_job_waits_for_the_port_and_fails_when_it_never_opens():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    closed = _run_script("wait-healthz.sh", {"HOSTAPP_PORT": str(port),
                                             "EMORI_HUB_HEALTHZ_WAIT_S": "1"})
    assert closed.returncode == 1
    assert f":{port}" in closed.stderr

    server = HTTPServer(("127.0.0.1", port), _Healthy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        opened = _run_script("wait-healthz.sh", {"HOSTAPP_PORT": str(port)})
        assert opened.returncode == 0, opened.stderr
    finally:
        server.shutdown()
        server.server_close()


# --- the environment snapshot ----------------------------------------------------------


def test_the_snapshot_names_the_voice_sink_only_when_a_delay_is_set():
    sink = "emori_voice"
    assert snap.snapshot({"EMORI_VOICE_DELAY_MS": "120"})["EMORI_VOICE_LINK_SINK"] == sink
    for value in ("", "0", "-5", "abc", " "):
        assert "EMORI_VOICE_LINK_SINK" not in snap.snapshot({"EMORI_VOICE_DELAY_MS": value})
    assert "EMORI_VOICE_LINK_SINK" not in snap.snapshot({})
    # A sink the operator chose stays.
    chosen = snap.snapshot({"EMORI_VOICE_DELAY_MS": "120", "EMORI_VOICE_LINK_SINK": "other"})
    assert chosen["EMORI_VOICE_LINK_SINK"] == "other"


def test_the_snapshot_and_the_voice_tap_agree_on_the_sink_name():
    from emori_adapter.hostapp import voice_link

    assert snap.VOICE_DELAY_SINK == voice_link.VOICE_DELAY_SINK
    assert snap.VOICE_DELAY_VAR == "EMORI_VOICE_DELAY_MS"
    assert snap.VOICE_SINK_VAR == "EMORI_VOICE_LINK_SINK"


def test_the_snapshot_is_whole_private_and_loads_into_the_hub_minus_the_denylist(tmp_path):
    target = tmp_path / "env.json"
    given = {"POKAMIND_BASE": "https://example.invalid", "PATH": "/host/bin",
             "ROS_DOMAIN_ID": "7", "EMORI_VOICE_DELAY_MS": "90", "LC_ALL": "C"}
    assert snap.main(["env-snapshot.py", str(target)], given) == 0
    assert json.loads(target.read_text(encoding="utf-8"))["POKAMIND_BASE"] == given[
        "POKAMIND_BASE"]
    if os.name == "posix":
        assert (target.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["env.json"]

    loaded = {hub_main.ENV_JSON_VAR: str(target), "PATH": "/container/bin"}
    hub_main.load_env_snapshot(loaded)
    assert loaded["POKAMIND_BASE"] == "https://example.invalid"
    assert loaded["EMORI_VOICE_LINK_SINK"] == "emori_voice"
    assert loaded["PATH"] == "/container/bin"
    assert "ROS_DOMAIN_ID" not in loaded and "LC_ALL" not in loaded


def _graph_snapshot(tmp_path, motor_text, **environ):
    target = tmp_path / "env.json"
    motor = tmp_path / "motor.env"
    if motor_text is not None:
        motor.write_text(motor_text, encoding="utf-8")
    given = {"EMORI_HUB_GRAPH": "1", **environ}
    assert snap.main(["env-snapshot.py", str(target)], given, motor_env=str(motor)) == 0
    return json.loads(target.read_text(encoding="utf-8"))


@pytest.mark.parametrize("value", ["0", ""])
def test_without_the_graph_the_block_still_says_what_leaves_on_dds(tmp_path, value):
    body = _graph_snapshot(tmp_path, "EMORI_ROS_SPEECH_TEXT=1\n",
                           EMORI_HUB_GRAPH=value, graph="a host variable")
    assert body["graph"]["EMORI_ROS_SPEECH_TEXT"] == "1", "and a stray host name does not pass"


@pytest.mark.parametrize("value", ["0", ""])
def test_a_host_file_does_not_open_the_gate_for_the_session_events_either(tmp_path, value):
    _graph_snapshot(tmp_path, "", EMORI_HUB_GRAPH=value, EMORI_ROS_SPEECH_TEXT="1",
                    EMORI_ROS_DEIDENTIFY="0")
    environ = {hub_main.ENV_JSON_VAR: str(tmp_path / "env.json")}
    lost: list[str] = []
    assert hub_main.load_env_snapshot(environ, lost) is None
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "0" and environ["EMORI_ROS_DEIDENTIFY"] == "1"
    assert lost == ["EMORI_ROS_DEIDENTIFY", "EMORI_ROS_SPEECH_TEXT"]


def test_the_graphs_block_comes_from_the_motors_env_file_only(tmp_path):
    body = _graph_snapshot(
        tmp_path,
        "# the motor's settings\n"
        "EMORI_GAZE=1\n"
        "export EMORI_ROS_DEIDENTIFY=0\n"
        'EMORI_ROS_SPEECH_TEXT="1"\n'
        "EMORI_ROS_ID_SALT='pep#per' # kept for a study\n"
        "EMORI_ROS_BEHAVIOR_CONFIG_HOST=/opt/emori/configs/tuned.yaml  # a site's\n",
        EMORI_ROS_SPEECH_TEXT="0")
    assert body["graph"] == {
        "EMORI_ROS_DEIDENTIFY": "0", "EMORI_ROS_SPEECH_TEXT": "1",
        "EMORI_ROS_ID_SALT": "pep#per",
        "EMORI_ROS_BEHAVIOR_CONFIG_HOST": "/opt/emori/configs/tuned.yaml"}
    assert body["EMORI_ROS_SPEECH_TEXT"] == "0"

    environ = {hub_main.ENV_JSON_VAR: str(tmp_path / "env.json")}
    settings = hub_main.load_env_snapshot(environ)
    assert environ["EMORI_ROS_SPEECH_TEXT"] == "1"
    assert environ["EMORI_ROS_ID_SALT"] == "pep#per"
    assert settings.behavior_yaml == "/opt/emori/configs/tuned.yaml"
    assert settings.ignored == ("EMORI_ROS_SPEECH_TEXT",)


_GRAPH_DEFAULTS = {"EMORI_ROS_DEIDENTIFY": "1", "EMORI_ROS_SPEECH_TEXT": "0",
                   "EMORI_ROS_ID_SALT": "",
                   "EMORI_ROS_BEHAVIOR_CONFIG_HOST": "/opt/emori/configs/behavior.yaml"}


def test_the_block_has_the_defaults_the_motors_compose_file_gives():
    text = _V2_COMPOSE.read_text(encoding="utf-8")
    assert snap.GRAPH_DEFAULTS == _GRAPH_DEFAULTS
    for name, value in _GRAPH_DEFAULTS.items():
        assert "${%s:-%s}" % (name, value) in text
    assert {name: hub_main.GRAPH_PRIVACY_DEFAULTS[name] for name in hub_main.GRAPH_PRIVACY_DEFAULTS
            } == {name: value for name, value in _GRAPH_DEFAULTS.items()
                  if name != "EMORI_ROS_BEHAVIOR_CONFIG_HOST"}
    assert hub_main.GRAPH_BEHAVIOR_DEFAULT == _GRAPH_DEFAULTS["EMORI_ROS_BEHAVIOR_CONFIG_HOST"]


@pytest.mark.parametrize("motor_text", [
    None, "", "EMORI_ROS_DEIDENTIFY=\nEMORI_ROS_SPEECH_TEXT=  \nEMORI_ROS_BEHAVIOR_CONFIG_HOST=\n"])
def test_a_missing_file_or_an_empty_value_is_the_default_as_in_compose(tmp_path, motor_text):
    assert _graph_snapshot(tmp_path, motor_text)["graph"] == _GRAPH_DEFAULTS


def test_a_motor_file_that_cannot_be_read_gives_the_closed_defaults_and_says_so(tmp_path, capsys):
    (tmp_path / "motor.env").mkdir()
    assert _graph_snapshot(tmp_path, None)["graph"] == _GRAPH_DEFAULTS
    assert "cannot read" in capsys.readouterr().err


def test_the_script_finds_the_motors_file_beside_its_own_directory(tmp_path):
    for sub in ("deploy/hub", "deploy/ros2"):
        (tmp_path / sub).mkdir(parents=True)
    script = tmp_path / "deploy/hub/env-snapshot.py"
    shutil.copy2(_HUB_DIR / "env-snapshot.py", script)
    (tmp_path / "deploy/ros2/.env").write_text("EMORI_ROS_ID_SALT=site-value\n", encoding="utf-8")
    target = tmp_path / "env.json"
    subprocess.run([sys.executable, str(script), str(target)], check=True, timeout=30,
                   env={**os.environ, "EMORI_HUB_GRAPH": "1"})
    assert json.loads(target.read_text(encoding="utf-8"))["graph"]["EMORI_ROS_ID_SALT"] == (
        "site-value")


def test_no_value_of_the_graphs_is_printed(tmp_path, capfd):
    _graph_snapshot(tmp_path, "EMORI_ROS_ID_SALT=a-secret-salt\n")
    captured = capfd.readouterr()
    assert "a-secret-salt" not in captured.out + captured.err


@pytest.mark.parametrize("path, exists, passes", [
    ("/opt/emori/configs/behavior.yaml", True, True),
    ("/opt/emori/configs/behavior.yaml", False, False),
    ("/etc/behavior.yaml", True, False),
    ("/opt/emori/../../etc/behavior.yaml", True, False),
    ("", False, True),
])
def test_gate_two_holds_the_behaviour_file_to_the_tree_the_host_shares(path, exists, passes):
    report = gates.inside_report({}, [], exists=lambda _path: exists, resolve=lambda _host: True,
                                 ca_certs=lambda: 1, behavior_yaml=path)
    problems = gates.evaluate({}, report, {})["paths"]
    assert (problems == []) is passes


def test_the_graphs_names_are_on_the_expected_list():
    expected = gates.parse_expected(_EXPECTED.read_text(encoding="utf-8"))
    assert expected["EMORI_SKILL_TRANSPORT"] == hub_main.GRAPH_TRANSPORT
    for name in hub_main.GRAPH_PRIVACY_DEFAULTS:
        assert expected[name] == gates.ANY_VALUE


def test_a_snapshot_that_cannot_be_written_fails_the_start(tmp_path, capsys):
    missing = tmp_path / "no-such-directory" / "env.json"
    assert snap.main(["env-snapshot.py", str(missing)], {"A": "1"}) == 1
    assert "cannot write" in capsys.readouterr().err
    assert snap.main(["env-snapshot.py", "a", "b"], {}) == 2


def test_a_failed_write_leaves_neither_a_temporary_file_nor_a_half_snapshot(tmp_path,
                                                                           monkeypatch):
    target = tmp_path / "env.json"
    target.write_text('{"OLD": "1"}', encoding="utf-8")

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(snap.os, "replace", refuse)
    with pytest.raises(OSError):
        snap.write_atomically(str(target), {"NEW": "1"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"OLD": "1"}
    assert [p.name for p in tmp_path.iterdir()] == ["env.json"]


# --- the four environment gates --------------------------------------------------------

_DENY = ("PATH", "HOME", "ROS_*", "LC_*")


def _h(**values):
    return gates.digests(values)


def test_equal_environments_pass_and_the_denylist_is_ignored():
    assert gates.compare_environments(
        _h(A="1", PATH="/a", ROS_DOMAIN_ID="1"), _h(A="1", PATH="/b", HOME="/h"),
        _DENY, {}) == []


def test_a_name_that_differs_or_exists_on_one_side_is_reported_by_name_only():
    problems = gates.compare_environments(
        _h(A="1", B="2", ONLY_HOST="x"), _h(A="9", B="2", ONLY_HUB="y"), _DENY, {})
    assert sorted(problems) == [
        "A: set to different values",
        "ONLY_HOST: set for the host application and missing in the hub",
        "ONLY_HUB: set in the hub only, and not on env_expected.txt"]
    assert not any(secret in " ".join(problems) for secret in ("x", "y")
                   if len(secret) > 1)


def test_an_extra_name_passes_only_with_its_exact_value_or_a_wildcard():
    hub = _h(EXTRA="v1", ANY="whatever")
    assert gates.compare_environments({}, hub, _DENY, {"EXTRA": "v1", "ANY": "*"}) == []
    assert gates.compare_environments({}, hub, _DENY, {"EXTRA": "v2", "ANY": "*"}) == [
        "EXTRA: set in the hub only, with another value than env_expected.txt gives"]
    # A listed name that is absent is fine.
    assert gates.compare_environments({}, {}, _DENY, {"EXTRA": "v1"}) == []
    # Listing a name does not excuse a host application value the hub lacks.
    assert gates.compare_environments(_h(EXTRA="v1"), {}, _DENY, {"EXTRA": "v1"}) == [
        "EXTRA: set for the host application and missing in the hub"]


def test_the_expected_file_parses_and_names_nothing_the_denylist_already_covers():
    expected = gates.parse_expected(_text(_EXPECTED))
    assert expected["EMORI_HUB_ENV_JSON"] == "/run/emori-hub/env.json"
    assert expected["EMORI_VOICE_LINK_SINK"] == "emori_voice"
    for name in expected:
        assert not gates.is_denied(name, hub_main.ENV_DENYLIST), name
    # The compose file's own additions are all accounted for.
    compose_env = dict(item.split("=", 1) for item in _compose()["environment"])
    for name, value in compose_env.items():
        if not gates.is_denied(name, hub_main.ENV_DENYLIST):
            assert expected.get(name) in (value, "*"), name


def test_a_malformed_expected_line_is_refused():
    with pytest.raises(ValueError):
        gates.parse_expected("GOOD=1\nnot a pair\n")
    assert gates.parse_expected("# c\n\nA=1\nB=\n") == {"A": "1", "B": ""}


def test_only_a_value_that_is_one_absolute_path_is_checked_for_existence():
    found = gates.absolute_paths(
        {"A": "/opt/emori/x", "B": "relative/x", "C": "/a:/b", "D": "/with space",
         "E": "/", "F": " /padded ", "PATH": "/usr/bin", "ROS_X": "/ros"}, _DENY)
    assert found == {"A": "/opt/emori/x", "F": "/padded"}


def test_hosts_come_from_urls_and_host_settings_and_skip_addresses():
    found = gates.hostnames({
        "POKAMIND_BASE": "https://API.example.invalid/v1", "ORION_URL": "http://orion:1026",
        "EMORI_SMTP_HOST": "smtp.example.invalid", "HOSTAPP_HOST": "0.0.0.0",
        "REACHY_BASE_URL": "http://127.0.0.1:8000", "LOCAL_URL": "http://localhost:1",
        "V6_URL": "http://[::1]:8000/", "PLAIN": "example.invalid", "EMPTY_HOST": "",
        "FILE_URL": "file:///etc/x", "ROS_URL": "http://ros.example.invalid"}, _DENY)
    assert found == {"POKAMIND_BASE": "api.example.invalid", "ORION_URL": "orion",
                     "EMORI_SMTP_HOST": "smtp.example.invalid"}


def _report(**changes):
    body = {"denylist": list(_DENY), "environment": _h(A="1"), "paths": {"A": True},
            "hosts": {"POKAMIND_BASE": True}, "ca_certs": 140}
    body.update(changes)
    return body


def test_the_inside_report_holds_hashes_and_flags_and_no_values():
    environ = {"POKAMIND_BASE": "https://api.example.invalid", "CERT": "/missing/cert.pem",
               "HERE": "/exists", "PATH": "/usr/bin"}
    report = gates.inside_report(
        environ, _DENY, exists=lambda path: path == "/exists",
        resolve=lambda host: host == "api.example.invalid", ca_certs=lambda: 3)
    assert report["paths"] == {"CERT": False, "HERE": True}
    assert report["hosts"] == {"POKAMIND_BASE": True}
    assert report["ca_certs"] == 3
    assert "api.example.invalid" not in json.dumps(report)
    assert report["environment"]["HERE"] == gates.digest("/exists")
    json.dumps(report)


def test_the_four_gates_each_pass_on_a_clean_report():
    host = {"A": "1", "POKAMIND_BASE": "https://api.example.invalid"}
    report = _report(environment=_h(**host), hosts={"POKAMIND_BASE": True})
    results = gates.evaluate(host, report, {}, host_resolves=lambda _host: True)
    assert results == {"environment": [], "paths": [], "hosts": [], "certificates": []}


def test_each_gate_fails_for_its_own_reason():
    host = {"A": "1", "POKAMIND_BASE": "https://api.example.invalid"}
    report = _report(environment=_h(A="2", POKAMIND_BASE=host["POKAMIND_BASE"]),
                     paths={"SSL_CERT_FILE": False}, hosts={"POKAMIND_BASE": False},
                     ca_certs=0)
    results = gates.evaluate(host, report, {}, host_resolves=lambda _host: True)
    assert results["environment"] == ["A: set to different values"]
    assert results["paths"] == ["SSL_CERT_FILE: names a path that does not exist in the container"]
    assert results["hosts"] == ["POKAMIND_BASE: resolves on the host and not in the container"]
    assert results["certificates"] == [
        "the container's default TLS context has no CA certificates"]


def test_a_name_that_resolves_nowhere_is_not_a_difference():
    host = {"POKAMIND_BASE": "https://api.example.invalid"}
    report = _report(environment=_h(**host), hosts={"POKAMIND_BASE": False})
    assert gates.evaluate(host, report, {}, host_resolves=lambda _host: False)["hosts"] == []


# --- what the directory must not say ---------------------------------------------------

_TRACKED = sorted(path for path in _HUB_DIR.iterdir()
                  if path.is_file() and path.name != "golden_tolerances.yaml")

#: Planning identifiers, dates, and the words of a development log.
_FORBIDDEN = re.compile(
    r"\b(?:T\d{2,3}|S-\d{2}|SP-\d{2}|SG-\d{1,2}|DM-\d{2}|OPS?\d{1,2}|N\d{2}|R\d{1,2}"
    r"|D\d{1,2}|E\d{1,2}[a-z]?)\b"
    r"|\b20\d\d-\d\d-\d\d\b"
    r"|(?i:verified live|proven on|root cause|the rig\b|the reference robot)")


@pytest.mark.parametrize("path", _TRACKED, ids=lambda path: path.name)
def test_no_file_carries_a_plan_identifier_a_date_or_a_closed_name(path):
    hits = [(number, match.group(0)) for number, line in
            enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            for match in [_FORBIDDEN.search(line)] if match]
    assert not hits, f"{path.name}: {hits}"
