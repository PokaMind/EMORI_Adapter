"""The ROS 2 stack the one-click deploy leaves a robot on: v2 by default.

``deploy/robot/ros2_native/v2-stack.sh`` RUN in bash against a scratch root, with stubs
for ``sudo``, ``systemctl``, ``docker``, ``curl`` and ``sleep`` that record what they were
asked and play the robot (the daemon answers on :8000 while it runs and the fence is down;
the v2 node answers once compose started it). Then the computer half's part
(``scripts/deploy_to_robot.py``): which stack, which image, and how it is shipped.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_STACK = _ROOT / "deploy" / "robot" / "ros2_native" / "v2-stack.sh"
_ONE_CLICK = _ROOT / "deploy" / "robot" / "one-click-deploy.sh"
_DOCKERFILE = _ROOT / "deploy" / "ros2" / "Dockerfile.v2.robot"
_DEPLOY = _ROOT / "scripts" / "deploy_to_robot.py"


def _bash():
    found = shutil.which("bash")
    if found is None or "system32" in found.lower():  # the WSL launcher is not a shell here
        return None
    return found


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="ascii", newline="\n")
    path.chmod(0o755)


class _Robot:
    """A scratch /opt/emori with the files the script copies, and a robot in stubs."""

    def __init__(self, tmp_path: Path, *, fenced: bool = False, image: bool = True,
                 docker: bool = True, comes_up: bool = True, v1_graph: bool = False,
                 tee: str | None = None, env: str = "") -> None:
        self.root = tmp_path / "opt"
        native = self.root / "deploy" / "robot" / "ros2_native"
        native.mkdir(parents=True)
        shutil.copy2(_STACK, native / "v2-stack.sh")
        _stub(self.root / "deploy/robot/session-busy.sh", 'exit "${STUB_SESSION_BUSY:-1}"\n')
        shutil.copy2(_ROOT / "deploy/robot/ros2_native/reachy-mini-daemon-v2-fence.conf", native)
        shutil.copy2(_ROOT / "deploy/robot/reachy-mini-daemon-override.conf",
                     self.root / "deploy" / "robot")
        (self.root / ".env").write_text(env, encoding="utf-8", newline="\n")
        self.state = tmp_path / "state"
        self.state.mkdir()
        self.fence = tmp_path / "V2_OWNS_THE_BUS"
        self.dropin = tmp_path / "dropin"
        self.camera = tmp_path / "emori-camera"
        self.camera.write_text("EMORI_CAM_FPS=30\n" + (
            f"EMORI_CAM_ROS_VIDEO_NR={tee}\n" if tee is not None else ""), encoding="ascii",
            newline="\n")
        if fenced:
            self.fence.touch()
            (self.state / "v2_up").touch()
        else:
            (self.state / "daemon_up").touch()
        self.log = tmp_path / "calls.log"
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        log, state, fence = self.log.as_posix(), self.state.as_posix(), self.fence.as_posix()
        _stub(stubs / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
        _stub(stubs / "sleep", "exit 0\n")
        for name in ("apt-get", "usermod"):
            _stub(stubs / name, f'echo "{name} $*" >> "{log}"\n')
        _stub(stubs / "systemctl", (
            f'echo "systemctl $*" >> "{log}"\n'
            'case "$*" in\n'
            '  "cat emori-ros2") [ -n "${STUB_V1_GRAPH:-}" ] ;;\n'
            f'  *"disable --now reachy-mini-daemon"*) rm -f "{state}/daemon_up" ;;\n'
            f'  *"enable --now reachy-mini-daemon"*) touch "{state}/daemon_up" ;;\n'
            f'  "start docker") [ -z "${{STUB_DOCKER_STARTS:-}}" ] || touch "{state}/docker_up" ;;\n'
            "  *) exit 0 ;;\n"
            "esac\n"))
        _stub(stubs / "docker", (
            f'echo "docker $*" >> "{log}"\n'
            f'[ ! -f "{state}/docker_hung" ] || exit 124\n'
            'case "$*" in *" up -d"*) if [ "${STUB_HANGS_ON_UP:-0}" = 1 ]; then\n'
            f'  touch "{state}/docker_hung"; exit 124; fi ;; esac\n'
            'case "$1" in\n'
            f'  info) [ "${{STUB_DOCKER:-1}}" = 1 ] || [ -f "{state}/docker_up" ] ;;\n'
            '  image) case "$*" in\n'
            '           *ros2-humble*) [ "${STUB_V1_IMAGE:-0}" = 1 ] ;;\n'
            '           *) [ "${STUB_IMAGE:-1}" = 1 ] ;;\n'
            "         esac ;;\n"
            f'  inspect) [ -f "{state}/v2_up" ] && echo true || echo false ;;\n'
            '  rm) exit "${STUB_RM_RC:-0}" ;;\n'
            '  compose)\n'
            '    case "$*" in\n'
            f'      *" up -d"*) [ "${{STUB_COMES_UP:-1}}" = 1 ] && touch "{state}/v2_up"; '
            'exit "${STUB_COMPOSE_RC:-0}" ;;\n'
            '      *" down"*) [ "${STUB_DOWN_RC:-0}" = 0 ] || exit "$STUB_DOWN_RC"\n'
            f'        rm -f "{state}/v2_up" ;;\n'
            "    esac ;;\n"
            "esac\n"))
        # :8000 answers as whoever owns the bus: the v2 node, or Pollen's daemon (no backend).
        _stub(stubs / "curl", (
            f'if [ -f "{fence}" ] && [ -f "{state}/v2_up" ]; then\n'
            '  echo \'{"state":"running","backend":"reachy_mini_ros2"}\'; exit 0; fi\n'
            f'if [ ! -f "{fence}" ] && [ -f "{state}/daemon_up" ]; then\n'
            "  echo '{\"state\":\"running\"}'; exit 0; fi\n"
            "exit 7\n"))
        self.env = {
            **os.environ,
            "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
            "EMORI_V2_FENCE": fence, "EMORI_DAEMON_DROPIN_DIR": self.dropin.as_posix(),
            "EMORI_CAMERA_DEFAULTS": self.camera.as_posix(),
            "EMORI_DAEMON_STATUS_URL": "http://127.0.0.1:8000/api/daemon/status",
            "EMORI_V2_WAIT_S": "6",
            "STUB_DOCKER": "1" if docker else "0", "STUB_IMAGE": "1" if image else "0",
            "STUB_COMES_UP": "1" if comes_up else "0", "STUB_V1_GRAPH": "1" if v1_graph else "",
        }

    def run(self, *args: str) -> subprocess.CompletedProcess:
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        result = subprocess.run(
            [bash, (self.root / "deploy/robot/ros2_native/v2-stack.sh").as_posix(), *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60, check=False,
            cwd=self.root, env=self.env)
        result.calls = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        result.kv = dict(line.split("=", 1) for line in result.stdout.splitlines()
                         if re.fullmatch(r"[a-z_]+=\S*", line))
        return result

    def tee(self) -> str:
        values = re.findall(r"^EMORI_CAM_ROS_VIDEO_NR=(.*)$", self.camera.read_text(), re.M)
        return values[-1] if values else ""

    def recorded(self) -> dict[str, str]:
        text = (self.root / "artifacts" / "last-ros2-stack.env").read_text(encoding="utf-8")
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


# --- the robot half: deploy/robot/ros2_native/v2-stack.sh -------------------------------


def test_a_v1_robot_with_its_image_is_committed_to_v2_in_the_readme_order(tmp_path):
    robot = _Robot(tmp_path, v1_graph=True)
    result = robot.run("apply", "v2")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.kv == {"stack_before": "v1", "stack": "v2", "changed": "yes",
                         "reason": "switched"}
    assert robot.recorded()["stack"] == "v2"
    assert robot.fence.exists()
    assert (robot.dropin / "zz-v2-fence.conf").read_text() == (
        _ROOT / "deploy/robot/ros2_native/reachy-mini-daemon-v2-fence.conf").read_text()
    calls = result.calls.splitlines()
    up = calls.index("docker compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml up -d")
    # Nothing else owns the bus before the node opens it.
    assert calls.index("systemctl disable --now emori-ros2") < up
    assert calls.index("systemctl disable --now reachy-mini-daemon") < up
    assert calls.index("systemctl daemon-reload") < up
    assert robot.tee() == "41"
    assert "systemctl try-restart emori-camera-bridge emori-kiosk" in calls
    assert calls[-1] == "systemctl try-restart emori-hostapp emori-admin"


def test_without_its_image_a_robot_stays_on_v1_untouched(tmp_path):
    robot = _Robot(tmp_path, image=False)
    result = robot.run("apply", "v2")

    assert result.returncode == 5
    assert result.kv["stack"] == "v1" and result.kv["reason"] == "no_image"
    assert not robot.fence.exists()
    assert "reachy-mini-daemon" not in result.calls
    assert "compose" not in result.calls


def test_without_docker_a_robot_stays_on_v1(tmp_path):
    result = _Robot(tmp_path, docker=False).run("apply", "v2")

    assert result.returncode == 5
    assert result.kv["reason"] == "no_docker"
    assert "reachy-mini-daemon" not in result.calls


def test_going_back_on_a_docker_that_is_down_starts_it_to_take_the_container_down(tmp_path):
    """The container restarts with Docker, and the next boot starts Docker, so one left there
    would run beside the daemon on the same bus."""

    robot = _Robot(tmp_path, fenced=True, docker=False)
    robot.env["STUB_DOCKER_STARTS"] = "1"
    result = robot.run("apply", "v2")

    assert result.returncode == 6, result.stdout
    assert result.kv["stack"] == "v1" and result.kv["reason"] == "no_docker"
    calls = result.calls.splitlines()
    assert calls.index("systemctl start docker") < calls.index(
        "docker compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml down")
    assert "!!" not in result.stdout.split("putting the robot back", 1)[1]


def test_a_docker_that_will_not_start_says_what_to_run_when_it_is_back(tmp_path):
    robot = _Robot(tmp_path, fenced=True, docker=False)
    result = robot.run("apply", "v2")

    assert result.returncode == 6, result.stdout
    assert "cannot start with the fence down" in result.stdout and "apply v1" in result.stdout
    assert " down" not in result.calls
    assert "systemctl enable --now reachy-mini-daemon" in result.calls.splitlines()


@pytest.mark.parametrize("args", [("apply", "v1"), ("apply", "v2")])
def test_a_docker_that_hangs_leaves_the_bus_as_it_is(tmp_path, args):
    """It may still be running the motor container, and neither stops nor reports it. Going to v1
    regardless would have Pollen's daemon open the bus beside it."""

    robot = _Robot(tmp_path, fenced=True)
    (robot.state / "docker_hung").touch()
    result = robot.run(*args)

    assert result.returncode == 5, result.stdout
    assert result.kv["reason"] == "docker_hung" and result.kv["stack"] == "v2"
    assert robot.fence.exists()
    assert "reachy-mini-daemon" not in result.calls


def test_a_docker_that_hangs_part_way_does_not_put_the_daemon_beside_the_container(tmp_path):
    robot = _Robot(tmp_path, fenced=True)
    robot.env["STUB_HANGS_ON_UP"] = "1"
    result = robot.run("apply", "v2")

    assert result.returncode == 7, result.stdout
    assert result.kv["reason"] == "compose_failed_and_docker_hung" and result.kv["stack"] == "v2"
    assert robot.fence.exists()
    assert "enable --now reachy-mini-daemon" not in result.calls
    assert "may still own the bus" in result.stdout


def test_a_container_that_would_not_stop_does_not_get_the_daemon_beside_it(tmp_path):
    """Docker answers and stopping the container hangs: both ways of removing it fail."""

    robot = _Robot(tmp_path, fenced=True)
    robot.env.update(STUB_DOWN_RC="124", STUB_RM_RC="1")
    result = robot.run("apply", "v1")

    assert result.returncode == 7, result.stdout
    assert result.kv["reason"] == "container_not_down" and result.kv["stack"] == "v2"
    assert robot.fence.exists()
    assert "enable --now reachy-mini-daemon" not in result.calls


@pytest.mark.parametrize("busy,kept", [("0", True), ("1", False)])
def test_a_deploy_does_not_recreate_the_motor_under_a_session(tmp_path, busy, kept):
    """A change to the container recreates it, and that parks the robot."""

    robot = _Robot(tmp_path, fenced=True)
    robot.env["STUB_SESSION_BUSY"] = busy
    result = robot.run("apply", "v2")

    assert result.returncode == 0, result.stdout
    assert ("--no-recreate" in result.calls) is kept
    assert ("left as it is" in result.stdout) is kept


def test_a_node_that_never_answers_puts_the_robot_back_on_v1(tmp_path):
    """A fence with no node behind it is a robot whose motors have no owner."""

    robot = _Robot(tmp_path, comes_up=False)
    result = robot.run("apply", "v2")

    assert result.returncode == 6, result.stdout
    assert result.kv["stack"] == "v1" and result.kv["reason"] == "v2_not_up"
    assert result.kv["changed"] == "no"
    assert not robot.fence.exists()
    assert (robot.dropin / "override.conf").exists()
    calls = result.calls.splitlines()
    assert "docker compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml down" in calls
    assert calls.index("systemctl enable --now reachy-mini-daemon") > calls.index(
        "docker compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml down")
    assert robot.tee() == ""


def test_compose_failing_also_puts_the_robot_back_on_v1(tmp_path):
    robot = _Robot(tmp_path)
    robot.env["STUB_COMPOSE_RC"] = "1"
    result = robot.run("apply", "v2")

    assert result.returncode == 6
    assert result.kv["reason"] == "compose_failed"
    assert not robot.fence.exists()


def test_an_already_committed_robot_is_left_as_it_is(tmp_path):
    robot = _Robot(tmp_path, fenced=True, tee="41")
    result = robot.run("apply")

    assert result.returncode == 0
    assert result.kv == {"stack_before": "v2", "stack": "v2", "changed": "no",
                         "reason": "unchanged"}
    # `up -d` again: it recreates the container only for a new image or new settings.
    assert "compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml up -d" in result.calls
    assert "try-restart emori-hostapp" not in result.calls
    assert "try-restart emori-camera-bridge" not in result.calls


def test_a_fenced_robot_without_an_image_is_given_back_to_the_daemon(tmp_path):
    robot = _Robot(tmp_path, fenced=True, image=False)
    result = robot.run("apply", "v2")

    assert result.returncode == 6
    assert result.kv["stack"] == "v1" and result.kv["changed"] == "yes"
    assert not robot.fence.exists()


def test_v1_is_the_readme_rollback(tmp_path):
    robot = _Robot(tmp_path, fenced=True, tee="41", env="EMORI_ROS_MOTION_OWNER=ros\n")
    result = robot.run("apply", "v1")

    assert result.returncode == 0, result.stdout
    assert result.kv == {"stack_before": "v2", "stack": "v1", "changed": "yes",
                         "reason": "switched"}
    assert robot.tee() == ""
    assert not robot.fence.exists()
    assert (robot.dropin / "override.conf").read_text() == (
        _ROOT / "deploy/robot/reachy-mini-daemon-override.conf").read_text()
    calls = result.calls.splitlines()
    down = calls.index("docker compose -p ros2 -f deploy/ros2/docker-compose.v2.robot.yml down")
    assert down < calls.index("systemctl enable --now reachy-mini-daemon")
    assert "EMORI_ROS_MOTION_OWNER=hostapp" in (robot.root / ".env").read_text()
    assert calls[-1] == "systemctl try-restart emori-hostapp emori-admin"


def test_v1_leaves_no_setting_that_points_readycheck_at_v2(tmp_path):
    """No v1 graph image here: EMORI_WITH_ROS2=1 would look for a graph that cannot run,
    and a readycheck override naming the tee just switched off would FAIL the gaze gate."""

    robot = _Robot(tmp_path, fenced=True, tee="41", env=(
        "EMORI_WITH_ROS2=1\nEMORI_READYCHECK_ROS_VIDEO_DEVICE=/dev/video41\nOTHER=1\n"))
    result = robot.run("apply", "v1")

    assert result.returncode == 0, result.stdout
    assert (robot.root / ".env").read_text() == (
        "EMORI_WITH_ROS2=0\n#EMORI_READYCHECK_ROS_VIDEO_DEVICE=/dev/video41\nOTHER=1\n")
    assert "enable --now emori-ros2" not in result.calls


def test_v1_brings_back_a_v1_graph_whose_image_is_still_there(tmp_path):
    robot = _Robot(tmp_path, fenced=True, v1_graph=True, env="EMORI_WITH_ROS2=1\n")
    robot.env["STUB_V1_IMAGE"] = "1"
    result = robot.run("apply", "v1")

    assert result.returncode == 0, result.stdout
    assert "systemctl enable --now emori-ros2" in result.calls.splitlines()
    assert (robot.root / ".env").read_text() == "EMORI_WITH_ROS2=1\n"


def test_dot_env_decides_and_a_v1_robot_asked_for_v1_is_not_touched(tmp_path):
    robot = _Robot(tmp_path, env="EMORI_ROS2_STACK=V1\n")
    result = robot.run("apply")

    assert result.returncode == 0
    assert result.kv["reason"] == "unchanged" and result.kv["stack"] == "v1"
    assert "systemctl" not in result.calls


def test_an_unknown_stack_is_refused(tmp_path):
    result = _Robot(tmp_path, env="EMORI_ROS2_STACK=v3\n").run("apply")

    assert result.returncode == 2
    assert "v1 or v2" in result.stderr


def test_docker_installs_with_the_debian_packages(tmp_path):
    robot = _Robot(tmp_path)
    result = robot.run("docker")

    # The stub docker answers `compose version`, so nothing needs installing here.
    assert result.returncode == 0 and "docker=ready" in result.stdout
    assert "apt-get install" not in result.calls


def test_status_names_the_stack_and_the_image(tmp_path):
    result = _Robot(tmp_path, fenced=True).run("status")

    assert result.kv["stack"] == "v2"
    assert result.kv["image"] == "yes" and result.kv["container"] == "running"
    assert result.kv["backend"] == "reachy_mini_ros2"


# --- the robot half: one-click-deploy.sh step 5b ---------------------------------------


def _step_5b(tmp_path, env: str, stack: str) -> tuple[subprocess.CompletedProcess, str]:
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    text = _ONE_CLICK.read_text(encoding="utf-8")
    block = text[text.index("# 5b. The ROS 2 stack"):text.index("# 6. Verdict.")]
    native = tmp_path / "deploy" / "robot" / "ros2_native"
    native.mkdir(parents=True)
    (native / "v2-stack.sh").write_text('echo "v2-stack $*"; exit 5\n', encoding="ascii",
                                        newline="\n")
    (tmp_path / ".env").write_bytes(env.encode())
    script = tmp_path / "step.sh"
    script.write_text(f"say() {{ echo \"== $* ==\"; }}\nSTACK={stack}\n{block}",
                      encoding="utf-8", newline="\n")
    result = subprocess.run([bash, script.as_posix()], capture_output=True, encoding="utf-8",
                            timeout=30, check=False, cwd=tmp_path)
    return result, (tmp_path / ".env").read_text(encoding="utf-8")


def test_a_stack_choice_is_written_to_dot_env_once(tmp_path):
    result, env = _step_5b(tmp_path, "A=1\nEMORI_ROS2_STACK=v2\n", "v1")
    assert env == "A=1\nEMORI_ROS2_STACK=v1\n"
    assert "v2-stack apply" in result.stdout
    assert "did not finish" not in result.stdout  # 5 = v2 not possible yet: said above


def test_a_stack_choice_is_appended_after_a_last_line_without_a_newline(tmp_path):
    _, env = _step_5b(tmp_path / "a", "A=1", "v2")
    assert env == "A=1\nEMORI_ROS2_STACK=v2\n"
    _, env = _step_5b(tmp_path / "b", "A=1\n", "")
    assert env == "A=1\n"


def test_the_stack_step_runs_after_the_install_and_before_readycheck():
    text = _ONE_CLICK.read_text(encoding="utf-8")
    assert (text.index("bash deploy/robot/install.sh")
            < text.index("bash deploy/robot/ros2_native/v2-stack.sh apply")
            < text.index("bash deploy/robot/readycheck.sh"))
    assert "--stack must be v1 or v2" in text


# --- the computer half: scripts/deploy_to_robot.py --------------------------------------


@pytest.fixture
def deploy():
    name = "deploy_to_robot_v2_under_test"
    spec = importlib.util.spec_from_file_location(name, _DEPLOY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def _args(**values):
    return SimpleNamespace(**{"v1": False, "v2": False, "rebuild_v2_image": False,
                              "yes": False, **values})


def test_the_stack_is_the_flag_then_dot_env_then_v2(deploy):
    assert deploy.target_stack(_args(), {}) == "v2"
    assert deploy.target_stack(_args(), {"stack_env": "V1"}) == "v1"
    assert deploy.target_stack(_args(), {"stack_env": "nonsense"}) == "v2"
    assert deploy.target_stack(_args(v2=True), {"stack_env": "v1"}) == "v2"
    assert deploy.target_stack(_args(v1=True), {}) == "v1"


def test_an_image_inspect_line_is_read_with_or_without_a_source(deploy):
    assert deploy.parse_v2_image("") is None
    assert deploy.parse_v2_image("|||") is None
    labelled = deploy.parse_v2_image("sha256:ab|0123456789abcdef|2026-10-30T10:00:00Z")
    assert labelled == {"id": "sha256:ab", "source": "0123456789abcdef",
                        "created": "2026-10-30"}
    assert deploy.parse_v2_image("sha256:ab|<no value>|2026-09-28T10:19:25")["source"] == ""


def test_the_source_hash_covers_everything_the_dockerfile_copies(deploy):
    """A COPY the hash misses is an image the deploy calls current when it is not."""

    sources = []
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            sources += [word for word in line.split()[1:-1] if not word.startswith("--")]
    uncovered = [source for source in sources if source != "README.md" and not any(
        source == path or source.startswith(path + "/") for path in deploy.V2_IMAGE_INPUTS)]
    assert sources and uncovered == []
    assert deploy.V2_DOCKERFILE in deploy.V2_IMAGE_INPUTS
    assert re.fullmatch(r"[0-9a-f]{16}", deploy.v2_source_hash())


def test_the_image_carries_its_source_as_its_last_instructions():
    lines = [line for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    assert lines[-2:] == ["ARG EMORI_V2_SOURCE=unknown",
                          "LABEL org.pokamind.emori.v2-source=$EMORI_V2_SOURCE"]


def _plan(deploy, monkeypatch, info, *, docker=True, local=None, answer=True, **args):
    monkeypatch.setattr(deploy, "v2_source_hash", lambda: "0123456789abcdef")
    monkeypatch.setattr(deploy, "local_docker", lambda: docker)
    monkeypatch.setattr(deploy, "local_v2_image", lambda: local)
    asked = []
    monkeypatch.setattr(deploy, "confirm", lambda prompt, default=True: asked.append(prompt)
                        or answer)
    plan = deploy.plan_v2_image(info, _args(**args), interactive=True)
    return plan, asked


def test_a_robot_with_an_image_keeps_it(deploy, monkeypatch):
    current = {"v2_image": "sha256:ab|0123456789abcdef|2026-10-30T10:00:00Z"}
    plan, asked = _plan(deploy, monkeypatch, current, docker=False)
    assert plan["action"] == "keep" and "this checkout" in plan["why"] and asked == []
    older = {"v2_image": "sha256:ab||2026-09-28T10:19:25"}
    plan, _ = _plan(deploy, monkeypatch, older, docker=False)
    assert plan["action"] == "keep" and "--rebuild-v2-image" in plan["why"]


def test_without_docker_here_the_robot_stays_on_v1(deploy, monkeypatch):
    plan, asked = _plan(deploy, monkeypatch, {"v2_image": ""}, docker=False)
    assert plan["action"] == "none" and "no Docker" in plan["why"] and asked == []


def test_a_current_local_image_is_shipped_without_a_question(deploy, monkeypatch):
    local = {"id": "sha256:cd", "source": "0123456789abcdef", "created": "", "size_mb": "1600"}
    plan, asked = _plan(deploy, monkeypatch, {"docker": "yes"}, local=local)
    assert plan["action"] == "ship" and asked == []


def test_a_build_is_asked_for_first(deploy, monkeypatch):
    stale = {"id": "sha256:cd", "source": "", "created": "", "size_mb": "1600"}
    plan, asked = _plan(deploy, monkeypatch, {"docker": "yes"}, local=stale, answer=False)
    assert plan["action"] == "none" and len(asked) == 1
    plan, _ = _plan(deploy, monkeypatch, {"docker": "yes"}, local=stale, answer=True)
    assert plan["action"] == "build"
    plan, asked = _plan(deploy, monkeypatch, {"docker": "yes"}, local=None, yes=True)
    assert plan["action"] == "build" and asked == []


def test_a_robot_with_no_docker_and_no_internet_stays_on_v1(deploy, monkeypatch):
    plan, _ = _plan(deploy, monkeypatch, {"docker": "no", "internet": "no"})
    assert plan["action"] == "none" and "internet" in plan["why"]


def test_the_robot_half_gets_the_stack_only_when_one_was_asked_for(deploy, monkeypatch):
    seen = []
    monkeypatch.setattr(deploy.subprocess, "run",
                        lambda command, **_: seen.append(command[-1])
                        or subprocess.CompletedProcess(command, 0))
    robot = deploy.Robot("reachy-mini.local", "pollen")
    checkout = {"commit": "0" * 40, "branch": "master"}
    deploy.install(robot, checkout, False, None)
    deploy.install(robot, checkout, False, "v1")
    assert "--stack" not in seen[0]
    assert seen[1].endswith("--stack v1")


def test_the_image_travels_through_a_local_registry_and_a_reverse_tunnel(deploy, monkeypatch):
    docker_calls, remote = [], []

    def docker(*args, **_):
        docker_calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    class Tunnel:
        def __init__(self, command, **_):
            self.command = command
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    tunnels = []
    monkeypatch.setattr(deploy, "_docker", docker)
    monkeypatch.setattr(deploy, "_free_local_port", lambda: 55123)
    monkeypatch.setattr(deploy, "_wait_http", lambda url, seconds: True)
    monkeypatch.setattr(deploy.time, "sleep", lambda s: None)
    monkeypatch.setattr(deploy.subprocess, "Popen",
                        lambda command, **k: tunnels.append(Tunnel(command)) or tunnels[-1])
    robot = deploy.Robot("reachy-mini.local", "pollen")
    monkeypatch.setattr(robot, "run", lambda command, **_: remote.append(command)
                        or subprocess.CompletedProcess(command, 0, b"pulled=yes\n", b""))

    assert deploy.ship_v2_image(robot) is True
    ref = "127.0.0.1:55123/emori/ros2-jazzy-v2:robot"
    assert ("run", "-d", "--rm", "--name", "emori-deploy-registry",
            "-p", "127.0.0.1:55123:5000", "registry:2") in docker_calls
    assert ("push", "-q", ref) in docker_calls
    command = tunnels[0].command
    # The forward goes BEFORE the destination, which ssh takes as the last word.
    assert command[-1] == "pollen@reachy-mini.local"
    assert command[-3:-1] == ["-R", "55123:127.0.0.1:55123"]
    assert "ExitOnForwardFailure=yes" in command
    assert tunnels[0].terminated
    assert f"docker pull -q {ref}" in remote[0]
    assert f"docker tag {ref} emori/ros2-jazzy-v2:robot" in remote[0]
    # The registry and its tag are gone afterwards, whatever happened.
    assert docker_calls[-2:] == [("rm", "-f", "emori-deploy-registry"), ("rmi", ref)]


def test_the_last_screen_says_which_stack_the_robot_runs(deploy):
    class FakeRobot:
        def __init__(self, text):
            self.text = text

        def run(self, command, **_):
            return subprocess.CompletedProcess(command, 0, self.text.encode(), b"")

    def line(text):
        return deploy.stack_outcome(FakeRobot(text))

    assert line("stack=v2\nchanged=yes\nreason=switched\n").endswith(
        "v2 (the robot is a ROS 2 node) (switched by this deploy)")
    assert "not possible yet (no image)" in line("stack=v1\nchanged=no\nreason=no_image\n")
    assert "did not come up (v2_not_up)" in line("stack=v1\nchanged=no\nreason=v2_not_up\n")
    assert "daemon did not answer" in line("stack=v1\nchanged=yes\nreason=daemon_not_up\n")
    assert line("") == ""
