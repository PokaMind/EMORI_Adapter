"""Switching the robot between the hostapp and the hub: ``deploy/robot/hub-stack.sh``.

The script RUN in bash against a scratch root, with stubs for ``sudo``, ``systemctl``,
``systemd-run``, ``docker``, ``curl``, ``python3`` and ``sleep`` that record what they were
asked and play the robot. The two programs answer on the same port, as they do: the
hostapp's ``/healthz`` has no ``runtime`` and the hub's says ``ros``. The unit stubs honour
the flag file the way the units' Conditions do. ``restart emori-hub`` writes the environment
snapshot from the root's ``.env`` with the real ``env-snapshot.py``, so what readycheck sees
is what the hub would have been started with.

What is held here, in the order the script has to get right:

* nothing that stops or restarts a program, or recreates the motor, happens while a session
  runs, on any path, including the rollback of a failed switch;
* a docker call that never returns cannot keep ``apply hostapp`` from ending;
* a switch to the hub is a restart, and a switch of the motor stops the running hub first;
* the failure path puts the hostapp back, and the motor when this run changed it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from emori_adapter.admin.system import hub_graph_on

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "deploy" / "robot" / "hub-stack.sh"
_FENCE = _ROOT / "deploy" / "robot" / "emori-hostapp-hub-fence.conf"
_SNAPSHOT = _ROOT / "deploy" / "hub" / "env-snapshot.py"
_HUB_UNIT = _ROOT / "deploy" / "hub" / "emori-hub.service"
_HOSTAPP_UNIT = _ROOT / "deploy" / "robot" / "emori-hostapp.service"
_V2_FENCE = _ROOT / "deploy" / "robot" / "ros2_native" / "reachy-mini-daemon-v2-fence.conf"

TOKEN = "tok-from-admin-env"
DECOY = "tok-from-dot-env"
V2 = "v2.launch.py"
ROBOT = "robot.launch.py"
NODES = ("reachy_mini", "emori_adapter", "hri_context_analyzer", "hri_engagement_behaviors",
         "skill_facade")


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


def _stub(path: Path, body: str, **names: str) -> None:
    for name, value in names.items():
        body = body.replace(f"@{name}@", value)
    path.write_text("#!/bin/sh\n" + body, encoding="ascii", newline="\n")
    path.chmod(0o755)


class _Robot:
    """A scratch /opt/emori and a robot in stubs. ``mode`` is the program it starts on."""

    def __init__(self, tmp_path: Path, *, mode: str = "hostapp", launch: str | None = V2,
                 graph: str | None = None, token: bool = True, addon: bool = False,
                 addons_script: bool = False, expected: str | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.tmp = tmp_path
        self.root = tmp_path / "opt"
        for sub in ("deploy/robot", "deploy/hub", "deploy/ros2", "secrets", "artifacts"):
            (self.root / sub).mkdir(parents=True)
        shutil.copy2(_SCRIPT, self.root / "deploy/robot/hub-stack.sh")
        shutil.copy2(_FENCE, self.root / "deploy/robot")
        shutil.copy2(_SNAPSHOT, self.root / "deploy/hub")
        dot_env = f"EMORI_ADMIN_TOKEN={DECOY}\n" if token else ""
        if graph is not None:
            dot_env += f"EMORI_HUB_GRAPH={graph}\n"
        (self.root / ".env").write_text(dot_env, encoding="utf-8", newline="\n")
        if token:
            (self.root / "secrets/admin.env").write_text(
                f"EMORI_ADMIN_TOKEN={TOKEN}\n", encoding="utf-8", newline="\n")
        if expected is not None:
            (self.root / "artifacts/hub-source.expected").write_text(expected + "\n")
        if addon:
            (self.root / "addons/sample").mkdir(parents=True)
            (self.root / "addons/sample/pyproject.toml").write_text("[project]\n")
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.flag = tmp_path / "HUB_IS_ROS"
        self.dropin = tmp_path / "dropin"
        self.log = tmp_path / "calls.log"
        if launch is not None:
            (self.state / "launch").write_text(launch + "\n")
        if mode == "hub":
            self.flag.touch()
            (self.state / "hub_active").touch()
        else:
            (self.state / "hostapp_active").touch()
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        names = {"LOG": self.log.as_posix(), "S": self.state.as_posix(),
                 "FLAG": self.flag.as_posix(), "TOKEN": TOKEN,
                 "PYTHON": Path(sys.executable).as_posix()}
        _stub(stubs / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
        _stub(stubs / "sleep", "exit 0\n")
        _stub(stubs / "python3", 'exec "@PYTHON@" "$@"\n', **names)
        _stub(stubs / "systemd-run", (
            'echo "systemd-run $*" >> "@LOG@"\n'
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in\n'
            '    --setenv=*) export "${1#--setenv=}" ;;\n'
            "    -p) shift ;;\n"
            "    --) shift; break ;;\n"
            "  esac\n"
            "  shift\n"
            "done\n"
            'exec "$@"\n'), **names)
        _stub(stubs / "systemctl", (
            'echo "systemctl $*" >> "@LOG@"\n'
            'case "$1" in\n'
            '  cat) case " ${STUB_MISSING_UNITS:-} " in *" $2 "*) exit 1 ;; esac; exit 0 ;;\n'
            "  is-active)\n"
            '    case "$2" in\n'
            "      emori-hostapp) f=hostapp_active ;;\n"
            "      emori-hub) f=hub_active ;;\n"
            "      emori-hub-stack) f=stack_active ;;\n"
            "      *) f=none ;;\n"
            "    esac\n"
            '    if [ -e "@S@/$f" ]; then echo active; exit 0; fi\n'
            "    echo inactive; exit 3 ;;\n"
            "  stop)\n"
            '    case "$2" in\n'
            '      emori-hostapp) rm -f "@S@/hostapp_active" ;;\n'
            '      emori-hub) rm -f "@S@/hub_active" ;;\n'
            "    esac\n"
            "    exit 0 ;;\n"
            "  start)\n"
            '    case "$*" in\n'
            "      *emori-hostapp*)\n"
            '        if [ ! -e "@FLAG@" ] && [ "${STUB_HOSTAPP_STARTS:-1}" = 1 ]; then\n'
            '          touch "@S@/hostapp_active"; fi ;;\n'
            "      *emori-kiosk*)\n"
            '        case "$*" in *--no-block*) ;; *)\n'
            '          echo "kiosk start would block" >> "@LOG@"; exit 99 ;; esac ;;\n'
            "    esac\n"
            "    exit 0 ;;\n"
            "  restart)\n"
            '    [ "$2" = emori-hub ] || exit 0\n'
            '    [ -e "@FLAG@" ] || exit 0\n'
            '    if [ "${STUB_HUB_STARTS:-1}" != 1 ]; then exit 1; fi\n'
            '    ( set -a; [ -f .env ] && . ./.env; set +a; '
            'python3 deploy/hub/env-snapshot.py "@S@/env.json" )\n'
            '    touch "@S@/hub_active"; exit 0 ;;\n'
            "  *) exit 0 ;;\n"
            "esac\n"), **names)
        _stub(stubs / "docker", (
            'echo "docker $*" >> "@LOG@"\n'
            'if [ "${STUB_DOCKER_HANG:-0}" = 1 ]; then PATH=/usr/bin:/bin:$PATH; '
            "exec sleep 30; fi\n"
            'case "$1" in\n'
            '  info) [ "${STUB_DOCKER_INFO:-1}" = 1 ] ;;\n'
            "  image)\n"
            '    case "$*" in\n'
            '      *" -f "*) printf \'%s\\n\' "${STUB_HUB_LABEL-abc123}" ;;\n'
            '      *) [ "${STUB_HUB_IMAGE:-1}" = 1 ] ;;\n'
            "    esac ;;\n"
            "  inspect)\n"
            '    case "$*" in *" emori-hub")\n'
            '      [ -e "@S@/hub_container" ] || exit 1\n'
            '      echo "${STUB_CONTAINER_RUNNING:-true}"; exit 0 ;;\n'
            "    esac\n"
            '    [ -f "@S@/launch" ] || exit 1\n'
            '    echo "[\\"ros2\\",\\"launch\\",\\"pkg\\",\\"$(cat "@S@/launch")\\"]" ;;\n'
            "  exec)\n"
            '    if [ -e "@S@/recreated" ] && [ "${STUB_MOTOR_DOWN_AFTER:-0}" = 1 ]; then\n'
            "      exit 1; fi\n"
            '    case "$*" in\n'
            '      *"service list"*)\n'
            '        [ -z "${STUB_LIST_SECONDS:-}" ] || /usr/bin/sleep "$STUB_LIST_SECONDS"\n'
            '        if [ "${STUB_SERVES_FIRST:-0}" = 1 ] && [ ! -e "@S@/recreated" ]; then\n'
            "          echo /reachy_mini/submit_move; exit 0; fi\n"
            '        [ "${STUB_SERVES:-1}" = 1 ] || exit 0\n'
            "        echo /reachy_mini/submit_move\n"
            # The shell itself writes, so a closed pipe kills it and the list's status says so.
            '        if [ "${STUB_LIST_LONG:-0}" = 1 ]; then\n'
            "          i=0; while [ $i -lt 20000 ]; do echo /reachy_mini/wake_up; i=$((i+1)); done\n"
            "        fi\n"
            "        exit 0 ;;\n"
            '      *"node list"*)\n'
            '        if [ -e "@S@/recreated" ] && [ "${STUB_NODES_AFTER:-1}" != 1 ]; then\n'
            "          echo /reachy_mini; exit 0; fi\n"
            f"        printf '/%s\\n' {' '.join(NODES)} ;;\n"
            "    esac ;;\n"
            '  rm) rm -f "@S@/hub_container" ;;\n'
            "  compose)\n"
            '    case "$*" in *" up -d"*)\n'
            '      echo "compose-up hub=$([ -e "@S@/hub_active" ] && echo yes || echo no) '
            "$(grep -E '^EMORI_HUB_GRAPH=' .env | tail -n1)\" >> \"@LOG@\"\n"
            '      if [ "${STUB_COMPOSE_FAILS_ONCE:-0}" = 1 ] && [ ! -e "@S@/failed_once" ]; then\n'
            '        touch "@S@/failed_once"; exit 1; fi\n'
            "      want=$(grep -E '^EMORI_MOTOR_LAUNCH=' deploy/ros2/.env 2>/dev/null "
            "| tail -n1 | cut -d= -f2-)\n"
            '      if [ -n "$want" ] && [ "${STUB_COMPOSE_IGNORES_LAUNCH:-0}" != 1 ]; then\n'
            '        echo "$want" > "@S@/launch"; fi\n'
            '      touch "@S@/recreated" ;;\n'
            "    esac ;;\n"
            "esac\n"), **names)
        _stub(stubs / "curl", (
            'url=""; cfg=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in\n'
            '    -K) cfg="$(cat)" ;;\n'
            '    http*) url="$1" ;;\n'
            "  esac\n"
            "  shift\n"
            "done\n"
            'echo "curl $url" >> "@LOG@"\n'
            'if [ -e "@S@/hub_container" ] && [ "${STUB_CONTAINER_ANSWERS:-1}" = 1 ]; then who=hub\n'
            'elif [ -e "@S@/hub_active" ] && [ "${STUB_HUB_ANSWERS:-1}" = 1 ]; then who=hub\n'
            'elif [ -e "@S@/hostapp_active" ]; then who=hostapp\n'
            "else exit 7; fi\n"
            'case "$url" in\n'
            "  */healthz)\n"
            '    [ "${STUB_HEALTHZ_SILENT:-0}" != 1 ] || exit 28\n'
            '    if [ "$who" = hub ]; then echo \'{"status":"ok","runtime":"ros"}\'\n'
            "    else echo '{\"status\":\"ok\"}'; fi ;;\n"
            "  */api/admin/summary)\n"
            '    case "$cfg" in *"X-EMORI-Admin-Token: @TOKEN@"*) ;; *) exit 22 ;; esac\n'
            '    if [ "${STUB_SUMMARY_UNREADABLE:-0}" = 1 ]; then echo "not json"; exit 0; fi\n'
            '    if [ -e "@S@/session" ]; then echo \'{"idle":false,"session_active":true}\'\n'
            "    else echo '{\"idle\":true,\"session_active\":false}'; fi ;;\n"
            "  */api/status)\n"
            '    if [ -e "@S@/session" ]; then echo \'{"session":{"session_id":"s1"}}\'\n'
            "    else echo '{\"session\":null}'; fi ;;\n"
            "  *) exit 22 ;;\n"
            "esac\n"), **names)
        _stub(self.root / "deploy/robot/readycheck.sh", (
            'echo "readycheck $* graph=$(grep -o \'"EMORI_HUB_GRAPH": *"[01]"\' '
            '"@S@/env.json" 2>/dev/null) hub=$([ -e "@S@/hub_active" ] && echo up || echo down)"'
            ' >> "@LOG@"\n'
            'if [ "${STUB_READYCHECK_FAILS_ONCE:-0}" = 1 ] && [ ! -e "@S@/rc_failed" ]; then\n'
            '  touch "@S@/rc_failed"; exit 1; fi\n'
            'if [ "${STUB_READYCHECK_RC:-0}" != 0 ]; then\n'
            '  [ "${STUB_READYCHECK_SESSION:-0}" = 1 ] && touch "@S@/session"\n'
            "  exit 1; fi\n"), **names)
        if addons_script:
            _stub(self.root / "deploy/hub/hub-addons.sh", (
                'echo "hub-addons ran" >> "@LOG@"\n'
                '[ "${STUB_ADDONS_STARTS_SESSION:-0}" = 1 ] && touch "@S@/session"\n'
                'exit "${STUB_ADDONS_RC:-0}"\n'), **names)
        self.env = {
            **os.environ,
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "EMORI_HUB_FLAG": self.flag.as_posix(),
            "EMORI_HOSTAPP_DROPIN_DIR": self.dropin.as_posix(),
            "EMORI_HUB_HEALTHZ_WAIT_S": "1", "EMORI_HUB_NODES_WAIT_S": "1",
            "EMORI_HUB_RESTART_LIMIT_S": "20",
            "EMORI_HUB_STACK_INSIDE": "1",
            **(env or {}),
        }
        for name in ("EMORI_HUB_STACK_RUN",):
            self.env.pop(name, None)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        self.log.write_text("", encoding="utf-8")
        run_env = {**self.env, **env}
        result = subprocess.run(
            [bash, (self.root / "deploy/robot/hub-stack.sh").as_posix(), *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=90, check=False,
            cwd=self.root, env=run_env)
        result.calls = self.log.read_text(encoding="utf-8").splitlines()
        result.kv = dict(line.split("=", 1) for line in result.stdout.splitlines()
                         if re.fullmatch(r"[a-z_]+=\S*", line))
        return result

    def session(self) -> None:
        (self.state / "session").touch()

    def hostapp_active(self) -> bool:
        return (self.state / "hostapp_active").exists()

    def hub_active(self) -> bool:
        return (self.state / "hub_active").exists()

    def launch(self) -> str:
        return (self.state / "launch").read_text().strip()

    def dot_env(self) -> str:
        return (self.root / ".env").read_text()

    def motor_env(self) -> str:
        path = self.root / "deploy/ros2/.env"
        return path.read_text() if path.exists() else ""


def _index(calls: list[str], needle: str, start: int = 0) -> int:
    for number, line in enumerate(calls[start:], start):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} not in calls:\n" + "\n".join(calls))


def _has(calls: list[str], needle: str) -> bool:
    return any(needle in line for line in calls)


#: Calls that change something: a program stopped, started or restarted, the motor
#: recreated, a build run. Reads (cat, is-active, inspect, exec, curl) are not here.
_CHANGES = ("systemctl stop", "systemctl start", "systemctl restart", "systemctl enable",
            "systemctl daemon-reload", "up -d", "hub-addons ran")


def _changed_anything(calls: list[str]) -> bool:
    return any(change in line for line in calls for change in _CHANGES)


def _everything_printed(result: subprocess.CompletedProcess, robot: _Robot) -> str:
    parts = [result.stdout, result.stderr, "\n".join(result.calls)]
    for path in (robot.root / "artifacts").glob("last-hub-stack.*"):
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


# --- arguments, detaching, the lock -----------------------------------------------------

@pytest.mark.parametrize("args", [(), ("deploy",), ("apply",), ("apply", "both"),
                                  ("apply", "ros", "--force"), ("apply", "ros", "--with-motor", "x"),
                                  ("apply", "hostapp", "motor")])
def test_a_bad_command_line_is_a_usage_error_and_touches_nothing(tmp_path, args):
    robot = _Robot(tmp_path)
    result = robot.run(*args)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "usage" in result.stderr
    assert result.calls == []
    assert not robot.flag.exists()


def test_status_reads_and_changes_nothing(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=ROBOT, graph="1")
    result = robot.run("status")
    assert result.returncode == 0, result.stderr
    assert result.kv == {"mode": "hub", "hostapp": "inactive", "hub": "active", "graph": "1",
                         "launch": ROBOT}
    assert not _changed_anything(result.calls)


def test_the_wait_for_the_motor_is_bounded_by_the_clock_and_not_by_a_count_of_checks(tmp_path):
    """One check takes as long as ``ros2`` needs to start and spin. Counting checks turned a
    limit of 180 s into eight minutes on a motor that never came up."""

    robot = _Robot(tmp_path, env={"EMORI_HUB_NODES_WAIT_S": "3"})
    result = robot.run("apply", "ros", "--with-motor", STUB_SERVES="0", STUB_SERVES_FIRST="1",
                       STUB_LIST_SECONDS="2")
    assert result.kv["reason"] == "motor_not_up", result.stdout + result.stderr
    checks = sum("service list" in line for line in result.calls)
    # The check before the recreate, then one at the start and one once the clock has passed the
    # limit. A count of checks made from three seconds would stop after one.
    assert checks == 3, result.calls


def test_a_hub_container_still_on_the_port_with_the_flag_down_is_not_unchanged(tmp_path):
    """A way back that Docker could not finish leaves the flag down and the container up. A
    second run with Docker answering again has to finish it, not report nothing to do."""

    robot = _Robot(tmp_path)
    (robot.state / "hostapp_active").unlink()
    (robot.state / "hub_container").touch()
    result = robot.run("apply", "hostapp")
    assert result.kv["reason"] == "switched", result.stdout + result.stderr
    assert result.returncode == 0
    assert not (robot.state / "hub_container").exists()
    assert robot.hostapp_active()


def test_a_hub_container_that_does_not_answer_is_on_the_port_all_the_same(tmp_path):
    """The kind of hub a switch rolls back from does not answer, and holds the port regardless."""

    robot = _Robot(tmp_path)
    (robot.state / "hostapp_active").unlink()
    (robot.state / "hub_container").touch()
    result = robot.run("apply", "hostapp", STUB_CONTAINER_ANSWERS="0")
    assert result.kv["reason"] == "switched", result.stdout + result.stderr
    assert not (robot.state / "hub_container").exists()
    assert robot.hostapp_active()


def test_a_stopped_hub_container_left_behind_changes_nothing(tmp_path):
    robot = _Robot(tmp_path)
    (robot.state / "hub_container").touch()
    result = robot.run("apply", "hostapp", STUB_CONTAINER_ANSWERS="0", STUB_CONTAINER_RUNNING="false")
    assert result.kv["reason"] == "unchanged", result.stdout + result.stderr
    assert result.returncode == 0


@pytest.mark.parametrize("target", ["ros", "hostapp"])
def test_a_run_says_which_way_it_goes_from_its_start_to_its_result(tmp_path, target):
    robot = _Robot(tmp_path)
    robot.run("apply", target, EMORI_HUB_STACK_INSIDE="1")
    assert (robot.root / "artifacts/hub-stack.target").read_text().strip() == target
    assert f"target={target}" in (robot.root / "artifacts/last-hub-stack.env").read_text()


def test_the_way_back_removes_a_hub_container_its_unit_left_behind(tmp_path):
    """Up on the port, a leftover would keep the host app from binding it."""

    robot = _Robot(tmp_path, mode="hub")
    result = robot.run("apply", "hostapp")
    assert result.returncode == 0, result.stdout + result.stderr
    stopped = _index(result.calls, "systemctl stop emori-hub")
    removed = _index(result.calls, "docker rm -f emori-hub")
    started = _index(result.calls, "systemctl start emori-hostapp")
    assert stopped < removed < started


def test_a_gate_still_warming_up_gets_a_second_look_before_the_switch_is_rolled_back(tmp_path):
    """The link's first heartbeat can come after the gateway's first answer, and readycheck reads
    one moment. Two failures in a row still roll the switch back."""

    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_READYCHECK_FAILS_ONCE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv["reason"] == "switched" and result.kv["readycheck"] == "pass"
    assert sum("readycheck " in line for line in result.calls) == 2

    again = tmp_path / "again"
    again.mkdir()
    robot = _Robot(again)
    result = robot.run("apply", "ros", STUB_READYCHECK_RC="1")
    assert result.returncode == 6 and result.kv["reason"] == "readycheck_failed"
    assert sum("readycheck " in line for line in result.calls) == 2
    assert robot.hostapp_active() and not robot.hub_active()


@pytest.mark.parametrize("extra", [(), ("--no-wait",)])
def test_the_transient_unit_has_the_runtime_directory_its_readycheck_needs(tmp_path, extra):
    """A transient unit has no login session. The readycheck it runs asks PipeWire about the
    audio devices, and without the directory the switch would roll itself back over it."""

    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", *extra, EMORI_HUB_STACK_INSIDE="")
    call = next(line for line in result.calls if line.startswith("systemd-run"))
    assert re.search(r"--setenv=XDG_RUNTIME_DIR=/run/user/\d+( |$)", call), call


def test_a_run_outside_the_unit_re_executes_in_a_transient_unit_and_returns_its_result(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", EMORI_HUB_STACK_INSIDE="")
    assert result.returncode == 0, result.stdout + result.stderr
    call = next(line for line in result.calls if line.startswith("systemd-run"))
    assert "--unit=emori-hub-stack" in call and "--collect" in call and "--wait" in call
    assert "--setenv=EMORI_HUB_STACK_INSIDE=1" in call
    assert call.rstrip().endswith("hub-stack.sh apply ros")
    # What the detached run printed reaches the caller, and so does its exit code.
    assert result.kv["mode"] == "hub" and result.kv["reason"] == "switched"
    assert robot.flag.exists()


def test_the_caller_gets_the_exit_code_the_detached_run_recorded(tmp_path):
    robot = _Robot(tmp_path)
    robot.session()
    result = robot.run("apply", "ros", EMORI_HUB_STACK_INSIDE="")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.kv["reason"] == "session_running"


def test_a_second_run_while_one_is_going_exits_3_and_starts_nothing(tmp_path):
    robot = _Robot(tmp_path)
    (robot.state / "stack_active").touch()
    result = robot.run("apply", "ros", EMORI_HUB_STACK_INSIDE="")
    assert result.returncode == 3
    assert "reason=busy" in result.stdout
    assert not _has(result.calls, "systemd-run")
    assert not _changed_anything(result.calls)


def test_apply_hostapp_detaches_the_same_way(tmp_path):
    robot = _Robot(tmp_path, mode="hub")
    result = robot.run("apply", "hostapp", EMORI_HUB_STACK_INSIDE="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _has(result.calls, "systemd-run")
    assert robot.hostapp_active() and not robot.hub_active()


def test_no_wait_starts_the_run_and_answers_at_once(tmp_path):
    robot = _Robot(tmp_path, mode="hub")
    result = robot.run("apply", "hostapp", "--no-wait", EMORI_HUB_STACK_INSIDE="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "reason=started" in result.stdout
    call = next(line for line in result.calls if line.startswith("systemd-run"))
    assert "--wait" not in call.split(" -- ")[0]


def test_the_result_is_kept_for_the_next_reader(tmp_path):
    robot = _Robot(tmp_path)
    robot.run("apply", "ros", EMORI_HUB_STACK_INSIDE="1", EMORI_HUB_STACK_RUN="run-7")
    recorded = dict(line.split("=", 1) for line in
                    (robot.root / "artifacts/last-hub-stack.env").read_text().splitlines())
    assert recorded["mode_before"] == "hostapp" and recorded["mode"] == "hub"
    assert recorded["changed"] == "yes" and recorded["exit"] == "0" and recorded["run"] == "run-7"
    assert (robot.root / "artifacts/last-hub-stack.log").read_text().strip()


# --- apply ros --------------------------------------------------------------------------

def test_apply_ros_switches_in_order_and_restarts_the_hub(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv["mode"] == "hub" and result.kv["changed"] == "yes"
    assert result.stderr == ""
    calls = result.calls
    assert robot.flag.exists()
    assert not robot.hostapp_active() and robot.hub_active()
    stop = _index(calls, "systemctl stop emori-hostapp")
    restart = _index(calls, "systemctl restart emori-hub")
    kiosk = _index(calls, "systemctl start --no-block emori-kiosk")
    ready = _index(calls, "readycheck --no-speak")
    reload_ = _index(calls, "systemctl daemon-reload")
    enable = _index(calls, "systemctl enable emori-hub emori-hub-resolv.path")
    assert reload_ < stop < restart < kiosk < ready
    assert enable < stop
    assert _index(calls, "systemctl start emori-hub-resolv.path") < stop
    # `start` would change nothing in a hub that is already up: the snapshot is written at start.
    assert not any(re.search(r"systemctl start emori-hub(\s|$)", line) for line in calls)
    # No motor work on a switch that does not change the graph.
    assert not _has(calls, "up -d")


def test_apply_ros_installs_the_fence_dropin_that_matches_the_hub_units_condition(tmp_path):
    robot = _Robot(tmp_path)
    assert robot.run("apply", "ros").returncode == 0
    installed = (robot.dropin / "zz-hub-fence.conf").read_text(encoding="utf-8")
    assert installed == _FENCE.read_text(encoding="utf-8")
    conditions = [line for line in installed.splitlines() if line.startswith("Condition")]
    assert conditions == ["ConditionPathExists=!/opt/emori/HUB_IS_ROS"]
    hub = [line for line in _HUB_UNIT.read_text().splitlines() if line.startswith("Condition")]
    assert hub == ["ConditionPathExists=/opt/emori/HUB_IS_ROS"]


def test_the_two_units_do_not_conflict_and_the_fence_is_a_dropin_not_an_edit():
    assert not re.search(r"^Conflicts=", _HUB_UNIT.read_text(), re.M)
    assert not re.search(r"^Conflicts=", _HOSTAPP_UNIT.read_text(), re.M)
    assert "ConditionPathExists" not in _HOSTAPP_UNIT.read_text()
    # The same shape as the fence that keeps Pollen's daemon off a bus the node owns.
    assert _V2_FENCE.read_text().rstrip().splitlines()[-2:] == [
        "[Unit]", "ConditionPathExists=!/opt/emori/V2_OWNS_THE_BUS"]
    assert _FENCE.read_text().rstrip().splitlines()[-2:] == [
        "[Unit]", "ConditionPathExists=!/opt/emori/HUB_IS_ROS"]


def test_apply_ros_reads_the_motor_with_a_spin_time_and_no_resident_daemon(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros")
    service_calls = [line for line in result.calls if "service list" in line]
    assert service_calls
    for line in service_calls:
        assert "--no-daemon" in line and "--spin-time 5" in line and "emori-ros2-v2" in line


def test_apply_ros_reads_a_service_list_longer_than_a_pipe(tmp_path):
    """Under pipefail a `grep -q` on the list closes the pipe at the first match and the list's
    writer dies of it, so a motor that serves moves read as one that does not."""

    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_LIST_LONG="1")
    assert result.kv["reason"] == "switched", result.stdout + result.stderr
    assert result.returncode == 0


def test_every_docker_call_in_the_script_runs_under_timeout():
    text = _SCRIPT.read_text(encoding="utf-8")
    bare = []
    for line in text.splitlines():
        code = re.sub(r"#.*$", "", re.sub(r'"[^"]*"|\'[^\']*\'', "", line))
        if re.search(r"(?<![\w-])docker(?![\w-])", code):
            bare.append(line.strip())
    # The one place the binary is named is the wrapper every call goes through.
    assert bare == ['sudo -n timeout -k 5 "$limit" docker "$@"']


def test_apply_ros_accepts_a_graph_hub_with_the_graph_launch_file(tmp_path):
    robot = _Robot(tmp_path, launch=ROBOT, graph="1")
    result = robot.run("apply", "ros")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv["launch"] == ROBOT
    assert not _has(result.calls, "up -d")


@pytest.mark.parametrize("launch, graph, reason", [
    (V2, "1", "graph_launch_mismatch"),
    (ROBOT, "0", "graph_launch_mismatch"),
    (ROBOT, None, "graph_launch_mismatch"),
    (None, "0", "launch_unknown"),
])
def test_apply_ros_refuses_a_hub_whose_graph_setting_disagrees_with_the_motors_launch_file(
        tmp_path, launch, graph, reason):
    robot = _Robot(tmp_path, launch=launch, graph=graph)
    result = robot.run("apply", "ros")
    assert result.returncode == 5, result.stdout + result.stderr
    assert result.kv["reason"] == reason
    assert not _changed_anything(result.calls)
    assert not robot.flag.exists() and robot.hostapp_active()


@pytest.mark.parametrize("name, kwargs, env, reason", [
    ("no hub unit", {}, {"STUB_MISSING_UNITS": "emori-hub"}, "no_hub_unit"),
    ("docker down", {}, {"STUB_DOCKER_INFO": "0"}, "no_docker"),
    ("no image", {}, {"STUB_HUB_IMAGE": "0"}, "no_hub_image"),
    ("unlabelled", {}, {"STUB_HUB_LABEL": ""}, "hub_image_unlabelled"),
    ("unknown label", {}, {"STUB_HUB_LABEL": "unknown"}, "hub_image_unlabelled"),
    ("stale image", {"expected": "def456"}, {}, "hub_image_stale"),
    ("low disk", {}, {"EMORI_HUB_MIN_FREE_MB": "999999999"}, "low_disk"),
    ("motor silent", {}, {"STUB_SERVES": "0"}, "motor_not_serving"),
])
def test_apply_ros_refuses_what_cannot_work_and_changes_nothing(tmp_path, name, kwargs, env, reason):
    robot = _Robot(tmp_path, **kwargs)
    result = robot.run("apply", "ros", **env)
    assert result.returncode == 5, (name, result.stdout + result.stderr)
    assert result.kv["reason"] == reason
    assert result.kv["changed"] == "no" and result.kv["mode"] == "hostapp"
    assert not _changed_anything(result.calls)
    assert not robot.flag.exists() and robot.hostapp_active()


def test_apply_ros_goes_ahead_when_the_images_label_is_the_shipped_one(tmp_path):
    robot = _Robot(tmp_path, expected="abc123")
    assert robot.run("apply", "ros").returncode == 0


def test_apply_ros_says_so_when_no_shipped_hash_was_recorded(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros")
    assert result.returncode == 0
    assert "not compared" in result.stdout


# --- a session is never interrupted -----------------------------------------------------

@pytest.mark.parametrize("args, mode, launch", [
    (("apply", "ros"), "hostapp", V2),
    (("apply", "ros", "--with-motor"), "hostapp", V2),
    (("apply", "ros", "--with-motor"), "hub", V2),
    (("apply", "hostapp"), "hub", V2),
    (("apply", "hostapp", "--with-motor"), "hub", ROBOT),
])
def test_no_path_changes_anything_while_a_session_runs(tmp_path, args, mode, launch):
    robot = _Robot(tmp_path, mode=mode, launch=launch, graph="1" if launch == ROBOT else None)
    robot.session()
    before = (robot.flag.exists(), robot.hostapp_active(), robot.hub_active(), robot.launch(),
              robot.dot_env(), robot.motor_env())
    result = robot.run(*args)
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.kv["reason"] == "session_running"
    assert result.kv["changed"] == "no"
    assert not _changed_anything(result.calls)
    assert before == (robot.flag.exists(), robot.hostapp_active(), robot.hub_active(),
                      robot.launch(), robot.dot_env(), robot.motor_env())


def test_a_session_that_starts_during_the_preparation_is_caught_by_the_second_check(tmp_path):
    robot = _Robot(tmp_path, addon=True, addons_script=True)
    result = robot.run("apply", "ros", STUB_ADDONS_STARTS_SESSION="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.kv["reason"] == "session_running"
    assert _has(result.calls, "hub-addons ran")
    assert not _has(result.calls, "systemctl stop")
    assert not robot.flag.exists() and robot.hostapp_active()


def test_apply_ros_wants_the_idle_answer_and_not_only_the_absence_of_a_session(tmp_path):
    robot = _Robot(tmp_path, token=False)
    result = robot.run("apply", "ros")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.kv["reason"] == "session_unknown"
    assert not _changed_anything(result.calls)


def test_apply_ros_treats_an_unreadable_answer_as_busy(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_SUMMARY_UNREADABLE="1")
    # The summary is unreadable; the unauthenticated status shows no session, which is not
    # the same as confirmed idle.
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.kv["reason"] == "session_unknown"
    assert not _changed_anything(result.calls)


def test_the_admin_token_comes_from_the_file_that_outranks_and_is_never_printed(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros")
    assert result.returncode == 0, result.stdout + result.stderr
    everything = _everything_printed(result, robot)
    assert TOKEN not in everything and DECOY not in everything


def test_a_token_with_a_character_that_would_break_the_curl_config_is_not_used(tmp_path):
    robot = _Robot(tmp_path)
    (robot.root / "secrets/admin.env").write_text('EMORI_ADMIN_TOKEN=a"b\n')
    result = robot.run("apply", "ros")
    assert result.returncode == 3 and result.kv["reason"] == "session_unknown"


def test_with_no_program_answering_and_none_running_there_is_nothing_to_interrupt(tmp_path):
    robot = _Robot(tmp_path)
    (robot.state / "hostapp_active").unlink()
    result = robot.run("apply", "ros")
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_program_that_is_running_but_silent_is_not_taken_for_idle(tmp_path):
    robot = _Robot(tmp_path, mode="hub")
    result = robot.run("apply", "ros", STUB_HUB_ANSWERS="0")
    # The hub's unit is active and nothing answers: the script cannot tell what it is doing.
    assert result.returncode == 3 and result.kv["reason"] == "session_unknown"
    assert not _changed_anything(result.calls)


# --- the add-on's packages --------------------------------------------------------------

def test_the_addon_packages_are_built_when_hub_site_has_none(tmp_path):
    robot = _Robot(tmp_path, addon=True, addons_script=True)
    result = robot.run("apply", "ros")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _index(result.calls, "hub-addons ran") < _index(result.calls, "systemctl stop")


def test_the_addon_packages_are_not_rebuilt_when_hub_site_is_current(tmp_path):
    robot = _Robot(tmp_path, addon=True, addons_script=True)
    site = robot.root / "hub-site/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (site / "sample.pth").write_text("x\n")
    pyproject = robot.root / "addons/sample/pyproject.toml"
    os.utime(pyproject, (1_000_000, 1_000_000))
    result = robot.run("apply", "ros")
    assert result.returncode == 0
    assert not _has(result.calls, "hub-addons ran")


def test_the_addon_packages_are_rebuilt_when_the_project_is_newer(tmp_path):
    robot = _Robot(tmp_path, addon=True, addons_script=True)
    site = robot.root / "hub-site/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    (site / "sample.pth").write_text("x\n")
    os.utime(site, (1_000_000, 1_000_000))
    assert robot.run("apply", "ros").returncode == 0


def test_a_robot_with_no_addon_builds_nothing(tmp_path):
    robot = _Robot(tmp_path, addons_script=True)
    assert robot.run("apply", "ros").returncode == 0
    assert not _has(robot.log.read_text().splitlines(), "hub-addons ran")


def test_a_failed_addon_build_is_a_warning_and_not_a_failed_switch(tmp_path):
    robot = _Robot(tmp_path, addon=True, addons_script=True)
    result = robot.run("apply", "ros", STUB_ADDONS_RC="1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hub-addons.sh failed" in result.stdout


# --- the failure path -------------------------------------------------------------------

def test_a_hub_that_does_not_start_puts_the_hostapp_back(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_HUB_STARTS="0")
    assert result.returncode == 6, result.stdout + result.stderr
    assert result.kv["reason"] == "hub_not_started"
    assert result.kv["mode"] == "hostapp"
    assert not robot.flag.exists()
    assert robot.hostapp_active() and not robot.hub_active()
    calls = result.calls
    restart = _index(calls, "systemctl restart emori-hub")
    # The way back goes through the same function as `apply hostapp`: flag down, hub stopped,
    # hostapp started, kiosk without waiting.
    assert _index(calls, "systemctl stop emori-hub", restart) < _index(
        calls, "systemctl start emori-hostapp", restart)
    assert _has(calls[restart:], "systemctl start --no-block emori-kiosk")
    assert not _has(calls, "up -d")


def test_a_hub_that_starts_but_never_answers_puts_the_hostapp_back(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_HUB_ANSWERS="0")
    assert result.returncode == 6, result.stdout + result.stderr
    assert result.kv["reason"] == "hub_not_answering"
    assert not robot.flag.exists() and robot.hostapp_active()


def test_a_failed_readycheck_puts_the_hostapp_back(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_READYCHECK_RC="1")
    assert result.returncode == 6, result.stdout + result.stderr
    assert result.kv["reason"] == "readycheck_failed" and result.kv["readycheck"] == "fail"
    assert not robot.flag.exists() and robot.hostapp_active()


def test_a_failure_with_a_session_on_the_hub_leaves_the_hub_alone(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_READYCHECK_RC="1", STUB_READYCHECK_SESSION="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.kv["reason"] == "session_running"
    assert robot.flag.exists() and robot.hub_active() and not robot.hostapp_active()
    calls = result.calls
    assert not _has(calls[_index(calls, "readycheck"):], "systemctl stop emori-hub")


def test_the_hostapp_not_coming_back_after_a_failed_switch_is_exit_7(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_HUB_STARTS="0", STUB_HOSTAPP_STARTS="0")
    assert result.returncode == 7, result.stdout + result.stderr
    assert result.kv["reason"] == "hub_not_started_and_hostapp_not_up"


# --- apply ros --with-motor -------------------------------------------------------------

def test_with_motor_stops_the_running_hub_before_any_setting_changes_and_restarts_it(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=V2, graph="0")
    result = robot.run("apply", "ros", "--with-motor")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = result.calls
    stop = _index(calls, "systemctl stop emori-hub")
    compose = _index(calls, "compose-up")
    restart = _index(calls, "systemctl restart emori-hub")
    assert stop < compose < restart
    # At the moment of the recreate the hub was already down and the new setting was in place.
    assert calls[compose] == "compose-up hub=no EMORI_HUB_GRAPH=1"
    assert robot.launch() == ROBOT
    assert "EMORI_MOTOR_LAUNCH=robot.launch.py" in robot.motor_env()
    assert re.findall(r"^EMORI_HUB_GRAPH=(.*)$", robot.dot_env(), re.M) == ["1"]
    # The hub that readycheck judged was started with the graph on.
    assert '"EMORI_HUB_GRAPH": "1"' in calls[_index(calls, "readycheck")]
    assert result.kv["launch"] == ROBOT and result.kv["mode"] == "hub"


def test_a_site_value_in_the_motors_env_file_reaches_the_hub_started_with_the_graph(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=V2, graph="0")
    motor_env = robot.root / "deploy/ros2/.env"
    motor_env.write_text("EMORI_ROS_ID_SALT=a-site-value\n", encoding="utf-8", newline="\n")
    result = robot.run("apply", "ros", "--with-motor")
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = json.loads((robot.state / "env.json").read_text(encoding="utf-8"))
    assert snapshot["graph"]["EMORI_ROS_ID_SALT"] == "a-site-value"
    assert "a-site-value" not in result.stdout + result.stderr


def test_the_phone_admins_way_back_reads_what_this_script_wrote(tmp_path):
    """The admin decides on --with-motor from the file, so writer and reader must agree."""

    robot = _Robot(tmp_path, mode="hub", launch=V2, graph="0")
    assert not hub_graph_on(str(robot.root))
    assert robot.run("apply", "ros", "--with-motor").returncode == 0
    assert hub_graph_on(str(robot.root))
    assert robot.run("apply", "hostapp", "--with-motor").returncode == 0
    assert not hub_graph_on(str(robot.root))


def test_with_motor_from_the_hostapp_recreates_the_motor_then_switches(tmp_path):
    robot = _Robot(tmp_path, launch=V2)
    result = robot.run("apply", "ros", "--with-motor")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = result.calls
    assert _index(calls, "compose-up") < _index(calls, "systemctl stop emori-hostapp")
    assert _index(calls, "systemctl stop emori-hostapp") < _index(calls, "systemctl restart emori-hub")
    assert not _has(calls, "systemctl stop emori-hub ")


def test_with_motor_does_not_touch_the_motor_that_already_runs_the_graph_launch_file(tmp_path):
    robot = _Robot(tmp_path, launch=ROBOT, graph="1")
    result = robot.run("apply", "ros", "--with-motor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert robot.launch() == ROBOT


def test_a_motor_that_keeps_its_old_launch_file_fails_the_switch_and_restores_it(tmp_path):
    robot = _Robot(tmp_path, launch=V2)
    result = robot.run("apply", "ros", "--with-motor", STUB_COMPOSE_IGNORES_LAUNCH="1")
    assert result.returncode == 6, result.stdout + result.stderr
    assert result.kv["reason"] == "motor_compose_failed"
    assert robot.hostapp_active() and not robot.flag.exists()
    assert re.findall(r"^EMORI_HUB_GRAPH=(.*)$", robot.dot_env(), re.M) == ["0"]


def test_a_failed_hub_after_a_motor_recreate_gets_the_motor_back_too(tmp_path):
    robot = _Robot(tmp_path, launch=V2, graph="0")
    result = robot.run("apply", "ros", "--with-motor", STUB_HUB_STARTS="0")
    assert result.returncode == 6, result.stdout + result.stderr
    assert result.kv["changed"] == "yes" and result.kv["nodes"] == "present"
    assert robot.launch() == V2
    assert re.findall(r"^EMORI_HUB_GRAPH=(.*)$", robot.dot_env(), re.M) == ["0"]
    assert re.findall(r"^EMORI_MOTOR_LAUNCH=(.*)$", robot.motor_env(), re.M) == [V2]
    assert len([line for line in result.calls if line.startswith("compose-up")]) == 2
    assert robot.hostapp_active() and not robot.hub_active() and not robot.flag.exists()


def test_a_failed_hub_on_the_hubs_launch_file_gets_the_motor_back_though_this_run_left_it(tmp_path):
    robot = _Robot(tmp_path, launch=ROBOT, graph="1")
    result = robot.run("apply", "ros", STUB_HUB_STARTS="0")
    assert result.returncode == 6, result.stdout + result.stderr
    assert robot.launch() == V2
    assert re.findall(r"^EMORI_HUB_GRAPH=(.*)$", robot.dot_env(), re.M) == ["0"]
    assert robot.hostapp_active() and not robot.flag.exists()


def test_apply_hostapp_finishes_a_motor_left_on_the_hubs_launch_file(tmp_path):
    robot = _Robot(tmp_path, launch=ROBOT, graph="1")
    (robot.root / "deploy/ros2/.env").write_text(f"EMORI_MOTOR_LAUNCH={ROBOT}\n", newline="\n")
    result = robot.run("apply", "hostapp", "--with-motor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv["reason"] != "unchanged"
    assert robot.launch() == V2
    assert re.findall(r"^EMORI_MOTOR_LAUNCH=(.*)$", robot.motor_env(), re.M) == [V2]


def test_settings_left_saying_the_hub_are_put_back_when_the_motor_never_moved(tmp_path):
    """A run cut off between writing the settings and recreating the motor."""

    robot = _Robot(tmp_path, launch=V2, graph="1")
    (robot.root / "deploy/ros2/.env").write_text(f"EMORI_MOTOR_LAUNCH={ROBOT}\n", newline="\n")
    result = robot.run("apply", "hostapp", "--with-motor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv["changed"] == "yes"
    assert re.findall(r"^EMORI_HUB_GRAPH=(.*)$", robot.dot_env(), re.M) == ["0"]
    assert re.findall(r"^EMORI_MOTOR_LAUNCH=(.*)$", robot.motor_env(), re.M) == [V2]


def test_a_failed_hub_without_a_motor_change_does_not_recreate_the_motor(tmp_path):
    robot = _Robot(tmp_path, launch=V2)
    result = robot.run("apply", "ros", STUB_HUB_STARTS="0")
    assert result.returncode == 6
    assert not _has(result.calls, "compose-up")


def test_a_motor_that_does_not_come_back_is_a_failed_switch(tmp_path):
    robot = _Robot(tmp_path, launch=V2)
    result = robot.run("apply", "ros", "--with-motor", STUB_MOTOR_DOWN_AFTER="1")
    assert result.returncode == 6, result.stdout + result.stderr
    assert result.kv["reason"] == "motor_not_up"
    assert not robot.flag.exists()


def test_a_compose_failure_on_the_way_in_is_rolled_back_with_the_motor(tmp_path):
    robot = _Robot(tmp_path, launch=V2)
    result = robot.run("apply", "ros", "--with-motor", STUB_COMPOSE_FAILS_ONCE="1")
    assert result.returncode == 6, result.stdout + result.stderr
    assert result.kv["reason"] == "motor_compose_failed"
    assert robot.launch() == V2 and robot.hostapp_active()


# --- apply hostapp ----------------------------------------------------------------------

def test_apply_hostapp_from_the_hub_is_one_short_sequence_with_no_image_or_motor_work(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=V2)
    result = robot.run("apply", "hostapp")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv["mode"] == "hostapp" and result.kv["reason"] == "switched"
    assert not robot.flag.exists() and robot.hostapp_active() and not robot.hub_active()
    calls = result.calls
    stop = _index(calls, "systemctl stop emori-hub")
    reload_ = _index(calls, "systemctl daemon-reload")
    start = _index(calls, "systemctl start emori-hostapp")
    kiosk = _index(calls, "systemctl start --no-block emori-kiosk")
    assert stop < reload_ < start < kiosk
    assert not _has(calls, "compose") and not _has(calls, "readycheck")
    # The hub stays enabled: the flag decides which unit starts, and the next boot too.
    assert not _has(calls, "systemctl disable")


def test_the_kiosk_is_never_started_in_a_way_that_waits_for_the_hub(tmp_path):
    for args, mode, launch in ((("apply", "hostapp"), "hub", V2),
                               (("apply", "ros"), "hostapp", V2)):
        robot = _Robot(tmp_path / mode, mode=mode, launch=launch)
        result = robot.run(*args)
        assert not _has(result.calls, "kiosk start would block")
        kiosk = [line for line in result.calls if "emori-kiosk" in line]
        assert kiosk and all("--no-block" in line for line in kiosk)


def test_apply_hostapp_with_nothing_to_do_touches_nothing(tmp_path):
    robot = _Robot(tmp_path, mode="hostapp")
    result = robot.run("apply", "hostapp")
    assert result.returncode == 0
    assert result.kv["reason"] == "unchanged" and result.kv["changed"] == "no"
    assert not _changed_anything(result.calls)
    # All that is asked of Docker: whether the hub's container is on the port, and what the motor runs.
    asked = [call for call in result.calls if call.startswith("docker")]
    assert asked and all(call.startswith("docker inspect") for call in asked)
    assert robot.hostapp_active()


def test_a_session_behind_a_healthz_that_times_out_is_not_taken_away(tmp_path):
    """/healthz asks the robot, so with the motor stalled it can time out while a session runs."""

    robot = _Robot(tmp_path, mode="hub")
    (robot.state / "session").touch()
    result = robot.run("apply", "hostapp", STUB_HEALTHZ_SILENT="1")
    assert result.returncode == 3, result.stdout + result.stderr
    assert result.kv["reason"] == "session_running"
    assert robot.flag.exists() and robot.hub_active()


def test_a_way_back_is_not_finished_while_the_host_app_does_not_answer(tmp_path):
    """The first run removed the flag, the hub and its container and stopped short of the host
    app, so with nothing else left to see, only the host app can say it is done."""

    robot = _Robot(tmp_path)
    (robot.state / "hostapp_active").unlink()
    result = robot.run("apply", "hostapp")
    assert result.kv["reason"] == "switched", result.stdout + result.stderr
    assert result.returncode == 0
    assert robot.hostapp_active()


def test_a_docker_that_gives_no_answer_does_not_read_as_nothing_to_do(tmp_path):
    """It cannot say the motor left the hub's launch file, so the result must not say all is well."""

    robot = _Robot(tmp_path, launch=ROBOT)
    result = robot.run("apply", "hostapp", STUB_DOCKER_HANG="1", EMORI_HUB_DOCKER_TIMEOUT_S="1")
    assert result.kv["reason"] != "unchanged", result.stdout + result.stderr


def test_a_motor_left_on_the_hubs_launch_file_is_not_nothing_to_do(tmp_path):
    """A way back whose compose failed has already written the settings back, so they read as
    the host app's while the container still runs the hub's launch file."""

    robot = _Robot(tmp_path, launch=ROBOT)
    refused = robot.run("apply", "hostapp")
    assert refused.returncode == 5 and refused.kv["reason"] == "needs_motor_recreate"
    result = robot.run("apply", "hostapp", "--with-motor")
    assert result.returncode == 0 and result.kv["reason"] == "switched", result.stdout
    assert _has(result.calls, "compose-up")


def test_apply_hostapp_is_not_possible_once_the_unit_is_gone(tmp_path):
    robot = _Robot(tmp_path, mode="hub")
    result = robot.run("apply", "hostapp", STUB_MISSING_UNITS="emori-hostapp")
    assert result.returncode == 5 and result.kv["reason"] == "removed"
    assert robot.flag.exists() and robot.hub_active()


def test_apply_hostapp_recovers_a_hub_that_answers_nothing_without_a_token(tmp_path):
    robot = _Robot(tmp_path, mode="hub", token=False)
    (robot.state / "hub_active").touch()
    result = robot.run("apply", "hostapp", STUB_HUB_ANSWERS="0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert robot.hostapp_active() and not robot.flag.exists()


def test_apply_hostapp_recovers_a_hub_whose_status_shows_no_session_without_a_token(tmp_path):
    robot = _Robot(tmp_path, mode="hub", token=False)
    result = robot.run("apply", "hostapp")
    assert result.returncode == 0, result.stdout + result.stderr
    assert robot.hostapp_active()


def test_apply_hostapp_refuses_an_unreadable_answer_from_a_hub_that_is_up(tmp_path):
    robot = _Robot(tmp_path, mode="hub")
    robot.session()
    result = robot.run("apply", "hostapp", STUB_SUMMARY_UNREADABLE="1")
    # Summary unreadable, status shows the session.
    assert result.returncode == 3 and result.kv["reason"] == "session_running"


def test_apply_hostapp_without_the_motor_option_leaves_a_graph_hub_running(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=ROBOT, graph="1")
    result = robot.run("apply", "hostapp")
    assert result.returncode == 5, result.stdout + result.stderr
    assert result.kv["reason"] == "needs_motor_recreate" and result.kv["launch"] == ROBOT
    assert robot.flag.exists() and robot.hub_active() and not robot.hostapp_active()
    assert not _changed_anything(result.calls)


def test_apply_hostapp_with_motor_restores_the_motor_and_waits_for_the_five_nodes(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=ROBOT, graph="1")
    result = robot.run("apply", "hostapp", "--with-motor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv["nodes"] == "present" and result.kv["readycheck"] == "pass"
    assert robot.launch() == V2
    assert re.findall(r"^EMORI_HUB_GRAPH=(.*)$", robot.dot_env(), re.M) == ["0"]
    assert re.findall(r"^EMORI_MOTOR_LAUNCH=(.*)$", robot.motor_env(), re.M) == [V2]
    calls = result.calls
    assert _index(calls, "systemctl stop emori-hub") < _index(calls, "compose-up")
    assert calls[_index(calls, "compose-up")] == "compose-up hub=no EMORI_HUB_GRAPH=0"
    assert _index(calls, "compose-up") < _index(calls, "node list") < _index(
        calls, "systemctl start emori-hostapp") < _index(calls, "readycheck")
    for line in calls:
        if "node list" in line:
            assert "--no-daemon" in line and "--spin-time 5" in line


def test_missing_graph_nodes_after_the_restore_are_no_success_and_the_rollback_goes_on(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=ROBOT, graph="1")
    result = robot.run("apply", "hostapp", "--with-motor", STUB_NODES_AFTER="0")
    assert result.returncode == 7, result.stdout + result.stderr
    assert result.kv["reason"] == "nodes_missing" and result.kv["nodes"] == "missing"
    assert "docker logs" in result.stdout
    assert robot.hostapp_active() and not robot.flag.exists()


def test_with_motor_on_a_motor_that_needs_no_restore_does_not_recreate_it(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=V2)
    result = robot.run("apply", "hostapp", "--with-motor")
    assert result.returncode == 0
    assert not _has(result.calls, "compose-up")


def test_a_docker_that_never_answers_cannot_keep_apply_hostapp_from_ending(tmp_path):
    robot = _Robot(tmp_path, mode="hub", launch=ROBOT)
    result = robot.run("apply", "hostapp", STUB_DOCKER_HANG="1", EMORI_HUB_DOCKER_TIMEOUT_S="1")
    assert result.returncode == 0, result.stdout + result.stderr
    # The launch file is unknown, treated as the hostapp's, and the flag still comes down.
    assert result.kv["launch"] == "unknown"
    assert not robot.flag.exists() and robot.hostapp_active()
    assert "treating it as" in result.stdout
    assert _has(result.calls, "systemctl start --no-block emori-kiosk")


def test_a_docker_that_never_answers_refuses_apply_ros_before_anything_changes(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("apply", "ros", STUB_DOCKER_HANG="1", EMORI_HUB_DOCKER_TIMEOUT_S="1")
    assert result.returncode == 5 and result.kv["reason"] == "no_docker"
    assert not _changed_anything(result.calls)


def test_the_hostapp_that_does_not_answer_is_exit_7(tmp_path):
    robot = _Robot(tmp_path, mode="hub")
    result = robot.run("apply", "hostapp", STUB_HOSTAPP_STARTS="0")
    assert result.returncode == 7, result.stdout + result.stderr
    assert result.kv["reason"] == "hostapp_not_up"
    assert not robot.flag.exists()


# --- what is tracked --------------------------------------------------------------------

def test_the_files_are_plain_text_with_no_planning_ids_dates_or_private_names():
    for path in (_SCRIPT, _FENCE):
        text = path.read_text(encoding="utf-8")
        assert text.isascii(), path
        for number, line in enumerate(text.splitlines(), 1):
            assert not re.search(r"\b(?:T\d{2}|S-\d{2}|SP-\d{2}|SG-\d+)\b", line), (path, number)
            assert not re.search(r"\b20\d\d-\d\d-\d\d\b", line), (path, number)
            assert not re.search(r"verified live|the rig\b", line, re.I), (path, number)


def test_the_script_is_executable_in_the_index():
    listing = subprocess.run(["git", "ls-files", "-s", "deploy/robot/hub-stack.sh"],
                             capture_output=True, text=True, cwd=_ROOT, check=False).stdout
    if not listing.strip():
        pytest.skip("not in a git checkout")
    assert listing.startswith("100755"), listing
