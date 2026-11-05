"""The deploy side of the hub: kiosk, units, the stack script, the deploy scripts.

The shell scripts are RUN in bash against a scratch root, with stubs for ``sudo``,
``systemctl``, ``docker``, ``curl`` and ``sleep`` that record what they were asked. Pieces
that cannot be run here (the installer is root, apt and systemd on the robot) are held to
their text, and the computer half of the deploy is called as functions.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from test_v2_stack import _Robot, _bash, _stub

_ROOT = Path(__file__).resolve().parents[1]
_ROBOT = _ROOT / "deploy" / "robot"
_HUB = _ROOT / "deploy" / "hub"
_REFRESH = _HUB / "hub-refresh.sh"
_ONE_CLICK = _ROBOT / "one-click-deploy.sh"
_INSTALL = _ROBOT / "install.sh"
_ROBOT_MODE = _ROBOT / "robot-mode.sh"
_KIOSK = _ROBOT / "kiosk.service"
_FENCE = _ROBOT / "emori-hostapp-hub-fence.conf"
_DEPLOY = _ROOT / "scripts" / "deploy_to_robot.py"


def _unit(path: Path) -> dict[str, list[str]]:
    """``Key -> [values]`` of a unit file's non-comment lines."""

    found: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith(("#", "[")) and "=" in line:
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


# --------------------------------------------------------------------------
# kiosk and the fence
# --------------------------------------------------------------------------
def test_the_kiosk_follows_whichever_gateway_runs_and_is_stopped_by_neither():
    unit = _unit(_KIOSK)

    for key in ("After", "Wants"):
        assert "emori-hostapp.service" in unit[key][0].split()
        assert "emori-hub.service" in unit[key][0].split()
    assert unit["PartOf"] == ["emori-hostapp.service emori-hub.service"]
    assert "Conflicts" not in unit


def test_the_kiosk_keeps_the_substrings_older_checks_look_for():
    text = _KIOSK.read_text(encoding="utf-8")

    assert "After=emori-hostapp.service" in text
    assert "PartOf=emori-hostapp.service" in text


def test_the_fence_skips_the_host_app_exactly_while_the_flag_is_up():
    assert _unit(_FENCE) == {"ConditionPathExists": ["!/opt/emori/HUB_IS_ROS"]}
    hub = _unit(_HUB / "emori-hub.service")
    assert hub["ConditionPathExists"] == ["/opt/emori/HUB_IS_ROS"]


def test_the_installer_puts_the_units_and_the_fence_in_place_without_enabling_the_hub():
    text = _INSTALL.read_text(encoding="utf-8")

    assert "emori-hostapp-hub-fence.conf" in text
    assert "/etc/systemd/system/emori-hostapp.service.d/zz-hub-fence.conf" in text
    for unit in ("emori-hub.service", "emori-hub-resolv.path", "emori-hub-resolv.service"):
        assert unit in text
    assert "enable --now emori-hub" not in text and "enable emori-hub" not in text


# --------------------------------------------------------------------------
# hub-refresh.sh
# --------------------------------------------------------------------------
class _Refresh:
    """A scratch install root and stubs: a hub image with a label, a unit that is or is not up."""

    def __init__(self, tmp_path: Path, *, active: bool = True, label: str = "abc123",
                 image: bool = True, expected: str | None = None,
                 expected_file: str | None = None, addons_script: bool = False) -> None:
        self.root = tmp_path / "opt"
        (self.root / "artifacts").mkdir(parents=True)
        (self.root / "deploy" / "hub").mkdir(parents=True)
        if addons_script:
            _stub(self.root / "deploy" / "hub" / "hub-addons.sh",
                  f'echo "hub-addons ran" >> "{tmp_path.as_posix()}/calls.log"\n')
        if expected_file is not None:
            (self.root / "artifacts" / "hub-source.expected").write_text(
                expected_file + "\n", encoding="utf-8", newline="\n")
        self.log = tmp_path / "calls.log"
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        log = self.log.as_posix()
        _stub(stubs / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
        _stub(stubs / "systemctl", (
            f'echo "systemctl $*" >> "{log}"\n'
            'case "$1" in is-active) [ "${STUB_ACTIVE:-1}" = 1 ] ;; *) exit 0 ;; esac\n'))
        _stub(stubs / "docker", (
            f'echo "docker $*" >> "{log}"\n'
            '[ "${STUB_IMAGE:-1}" = 1 ] || exit 1\n'
            'case "$*" in *Labels*) printf "%s\\n" "${STUB_LABEL:-}" ;; esac\n'))
        _stub(stubs / "curl", (
            'url=""; cfg=""\n'
            "while [ $# -gt 0 ]; do\n"
            '  case "$1" in -K) cfg="$(cat)" ;; http*) url="$1" ;; esac\n'
            "  shift\n"
            "done\n"
            f'echo "curl $url" >> "{log}"\n'
            '[ "${STUB_ANSWERS:-1}" = 1 ] || exit 7\n'
            'case "$url" in\n'
            '  */healthz) [ "${STUB_HEALTHZ_SILENT:-0}" != 1 ] || exit 28 ;;\n'
            "  */api/admin/summary)\n"
            '    case "$cfg" in *"X-EMORI-Admin-Token: tok-1"*) ;; *) exit 22 ;; esac\n'
            '    if [ -n "${STUB_SUMMARY:-}" ]; then echo "$STUB_SUMMARY"; exit 0; fi\n'
            '    if [ "${STUB_SESSION:-0}" = 1 ]; then '
            "echo '{\"idle\":false,\"session_active\":true}'\n"
            "    else echo '{\"idle\":true,\"session_active\":false}'; fi ;;\n"
            "  */api/status)\n"
            '    [ "${STUB_STATUS_FAILS:-0}" != 1 ] || exit 22\n'
            '    if [ "${STUB_SESSION:-0}" = 1 ]; then echo \'{"session":{"session_id":"s1"}}\'\n'
            "    else echo '{\"session\":null}'; fi ;;\n"
            "  *) exit 22 ;;\n"
            "esac\n"))
        self.env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                    "EMORI_ROOT": self.root.as_posix(),
                    "STUB_ACTIVE": "1" if active else "0",
                    "STUB_IMAGE": "1" if image else "0", "STUB_LABEL": label}
        self.env.pop("EMORI_HUB_SOURCE_EXPECTED", None)
        if expected is not None:
            self.env["EMORI_HUB_SOURCE_EXPECTED"] = expected

    def run(self, *args: str) -> subprocess.CompletedProcess:
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        result = subprocess.run([bash, _REFRESH.as_posix(), *args], capture_output=True,
                                encoding="utf-8", errors="replace", env=self.env, timeout=60,
                                check=False)
        result.calls = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return result


def test_a_running_hub_is_restarted_when_its_image_was_built_from_the_shipped_code(tmp_path):
    robot = _Refresh(tmp_path, expected="abc123")

    result = robot.run("restart")

    assert result.returncode == 0
    assert "systemctl try-restart emori-hub" in result.calls
    assert "restarted emori-hub" in result.stdout


@pytest.mark.parametrize("with_token", [True, False])
def test_a_deploy_does_not_restart_the_hub_under_a_session(tmp_path, with_token):
    robot = _Refresh(tmp_path, expected="abc123")
    if with_token:
        (robot.root / "secrets").mkdir()
        (robot.root / "secrets" / "admin.env").write_text("EMORI_ADMIN_TOKEN=tok-1\n",
                                                          encoding="utf-8", newline="\n")
    robot.env["STUB_SESSION"] = "1"

    result = robot.run("restart")

    assert result.returncode == 0
    assert "try-restart" not in result.calls
    assert "left running" in result.stdout and "sudo systemctl restart emori-hub" in result.stdout
    assert "tok-1" not in result.stdout + result.stderr + result.calls


def test_a_hub_that_does_not_answer_has_no_session_to_protect(tmp_path):
    robot = _Refresh(tmp_path, expected="abc123")
    robot.env["STUB_ANSWERS"] = "0"

    assert "systemctl try-restart emori-hub" in robot.run("restart").calls


@pytest.mark.parametrize("with_token", [True, False])
def test_a_session_behind_a_healthz_that_times_out_keeps_the_hub_running(tmp_path, with_token):
    """/healthz asks the robot, so with the motor stalled it can time out while a session runs."""

    robot = _Refresh(tmp_path, expected="abc123")
    if with_token:
        (robot.root / "secrets").mkdir()
        (robot.root / "secrets" / "admin.env").write_text("EMORI_ADMIN_TOKEN=tok-1\n",
                                                          encoding="utf-8", newline="\n")
    robot.env.update(STUB_SESSION="1", STUB_HEALTHZ_SILENT="1")

    result = robot.run("restart")

    assert "try-restart" not in result.calls
    assert "left running" in result.stdout


def test_a_hub_is_called_gone_only_when_healthz_has_failed_twice(tmp_path):
    robot = _Refresh(tmp_path, expected="abc123")
    robot.env["STUB_ANSWERS"] = "0"

    calls = robot.run("restart").calls

    assert calls.count("/healthz") == 2
    assert calls.index("/api/status") < calls.index("/healthz")


@pytest.mark.parametrize("answers,busy", [
    ({"summary": '{"idle":false,"session_active":true}'}, True),
    ({"summary": '{"idle":true,"session_active":false}'}, False),
    ({"status": '{"session":{"session_id":"s1"}}'}, True),
    ({"status": '{"session":null}'}, False),
    ({"healthz": "ok"}, True),
    ({}, False),
])
def test_what_can_park_the_robot_asks_about_a_session_the_same_way(tmp_path, answers, busy):
    """session-busy.sh is what the motor overlay and a deploy ask before the motor is recreated."""

    root = tmp_path / "opt"
    (root / "deploy" / "robot").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "secrets" / "admin.env").write_text("EMORI_ADMIN_TOKEN=tok-1\n", encoding="utf-8", newline="\n")
    script = root / "deploy" / "robot" / "session-busy.sh"
    shutil.copy2(_ROOT / "deploy" / "robot" / "session-busy.sh", script)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    body = 'url=""\nwhile [ $# -gt 0 ]; do case "$1" in -K) cat >/dev/null ;; http*) url="$1" ;; esac; shift; done\n'
    for route, name in (("*/api/admin/summary", "summary"), ("*/api/status", "status"), ("*/healthz", "healthz")):
        if name in answers:
            body += f"case \"$url\" in {route}) echo '{answers[name]}'; exit 0 ;; esac\n"
    _stub(stubs / "curl", body + "exit 7\n")
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    result = subprocess.run([bash, script.as_posix()], capture_output=True, env=env, timeout=30, check=False)
    assert result.returncode == (0 if busy else 1), result.stderr


def test_a_hub_that_answers_and_cannot_say_keeps_running(tmp_path):
    """A status that fails on a loaded robot is no sign that nobody is in a session."""

    robot = _Refresh(tmp_path, expected="abc123")
    robot.env["STUB_STATUS_FAILS"] = "1"

    result = robot.run("restart")

    assert "try-restart" not in result.calls
    assert "left running" in result.stdout


@pytest.mark.parametrize("session,restarted", [("0", True), ("1", False)])
def test_a_summary_that_does_not_say_goes_on_to_the_status(tmp_path, session, restarted):
    robot = _Refresh(tmp_path, expected="abc123")
    (robot.root / "secrets").mkdir()
    (robot.root / "secrets" / "admin.env").write_text("EMORI_ADMIN_TOKEN=tok-1\n",
                                                      encoding="utf-8", newline="\n")
    robot.env.update(STUB_SUMMARY='{"service":"hub"}', STUB_SESSION=session)

    assert ("systemctl try-restart emori-hub" in robot.run("restart").calls) is restarted


def test_the_shipped_hash_may_come_from_the_file_the_deploy_wrote(tmp_path):
    robot = _Refresh(tmp_path, expected_file="abc123")

    assert "systemctl try-restart emori-hub" in robot.run("restart").calls


def test_a_hub_built_from_other_code_is_left_running_and_the_operator_is_told(tmp_path):
    robot = _Refresh(tmp_path, label="old999", expected="abc123")

    result = robot.run("restart")

    assert result.returncode == 0
    assert "try-restart" not in result.calls
    assert "!! emori-hub left running" in result.stdout
    assert "old999" in result.stdout and "Deploy again" in result.stdout


def test_no_shipped_hash_counts_as_a_mismatch(tmp_path):
    robot = _Refresh(tmp_path)

    result = robot.run("restart")

    assert "try-restart" not in result.calls
    assert "no hash shipped" in result.stdout


def test_an_unlabelled_image_never_matches(tmp_path):
    robot = _Refresh(tmp_path, label="", expected="abc123")

    assert "try-restart" not in robot.run("restart").calls


def test_a_hub_that_is_not_running_is_not_started(tmp_path):
    robot = _Refresh(tmp_path, active=False, expected="abc123")

    result = robot.run("restart")

    assert result.returncode == 0 and result.stdout == ""
    assert "try-restart" not in result.calls and "docker" not in result.calls


def test_the_add_on_step_runs_only_with_the_script_and_the_image(tmp_path):
    ran = _Refresh(tmp_path / "a", addons_script=True)
    no_image = _Refresh(tmp_path / "b", addons_script=True, image=False)
    no_script = _Refresh(tmp_path / "c")
    for sub in "abc":
        (tmp_path / sub).mkdir(exist_ok=True)

    assert "hub-addons ran" in ran.run("addons").calls
    assert "hub-addons ran" not in no_image.run("addons").calls
    assert no_script.run("addons").returncode == 0


def test_a_failing_add_on_step_is_reported_and_does_not_fail_the_deploy(tmp_path):
    robot = _Refresh(tmp_path, addons_script=True)
    _stub(robot.root / "deploy" / "hub" / "hub-addons.sh", "exit 4\n")

    result = robot.run("addons")

    assert result.returncode == 0 and "!! the add-ons could not be installed" in result.stdout


def test_hub_refresh_names_no_add_on_directory():
    text = _REFRESH.read_text(encoding="utf-8")

    assert "addons/" not in text.replace("hub-addons", "")


# --------------------------------------------------------------------------
# the deploy scripts call it, in order
# --------------------------------------------------------------------------
def test_the_quick_deploy_installs_hub_add_ons_before_the_restart_and_restarts_the_hub_last():
    text = _ONE_CLICK.read_text(encoding="utf-8")
    quick = text[text.index('say "quick deploy (code only)"'):text.index("cat > artifacts/last-deploy.env")]

    assert quick.index("hub-refresh.sh addons") < quick.index("try-restart emori-hostapp emori-admin")
    assert quick.index("try-restart emori-hostapp emori-admin") < quick.index(
        "hub-refresh.sh restart")


def test_a_quick_deploy_refuses_only_when_neither_gateway_is_enabled():
    text = _ONE_CLICK.read_text(encoding="utf-8")

    assert "is-enabled --quiet emori-hostapp" in text and "is-enabled --quiet emori-hub" in text
    assert "neither emori-hostapp nor emori-hub is enabled" in text


def test_the_full_install_restarts_the_hub_after_the_host_apps_try_restart():
    text = _INSTALL.read_text(encoding="utf-8")
    restart = text.index("sudo systemctl try-restart emori-hostapp emori-admin")

    assert text.index("hub-refresh.sh addons") < text.index("# 8. Admin secrets")
    assert restart < text.index("hub-refresh.sh restart")


class _OneClick:
    """one-click-deploy.sh run as far as its guards, in a scratch root."""

    def __init__(self, tmp_path: Path, *, hub: bool) -> None:
        self.root = tmp_path / "opt"
        (self.root / "deploy" / "robot").mkdir(parents=True)
        shutil.copy2(_ONE_CLICK, self.root / "deploy" / "robot" / "one-click-deploy.sh")
        (self.root / ".env").write_text("", encoding="utf-8")
        self.flag = tmp_path / "HUB_IS_ROS"
        if hub:
            self.flag.touch()
        self.log = tmp_path / "calls.log"
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        log = self.log.as_posix()
        _stub(stubs / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
        _stub(stubs / "docker", f'echo "docker $*" >> "{log}"\necho false\n')
        _stub(stubs / "systemctl", f'echo "systemctl $*" >> "{log}"\nexit 0\n')
        self.env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
                    "EMORI_HUB_FLAG": self.flag.as_posix(),
                    "EMORI_V2_FENCE": (tmp_path / "fence").as_posix()}

    def run(self, *args: str) -> subprocess.CompletedProcess:
        bash = _bash()
        if bash is None:
            pytest.skip("no bash")
        result = subprocess.run(
            [bash, (self.root / "deploy/robot/one-click-deploy.sh").as_posix(), *args],
            capture_output=True, encoding="utf-8", errors="replace", env=self.env, timeout=60,
            check=False, cwd=self.root)
        result.calls = self.log.read_text(encoding="utf-8") if self.log.exists() else ""
        return result


def test_the_robot_half_refuses_v1_while_the_hub_runs_the_gateway(tmp_path):
    result = _OneClick(tmp_path, hub=True).run("--stack", "v1")

    assert result.returncode == 3
    assert "cannot be put on v1" in result.stdout and "hub-stack.sh apply hostapp" in result.stdout
    assert "systemctl" not in result.calls


def test_the_robot_half_records_the_hash_the_hub_image_must_match():
    text = _ONE_CLICK.read_text(encoding="utf-8")

    assert '--hub-source) HUB_SOURCE="${2:-}"; shift ;;' in text
    assert "artifacts/hub-source.expected" in text
    assert _REFRESH.read_text(encoding="utf-8").count("hub-source.expected") >= 1


# --------------------------------------------------------------------------
# v2-stack.sh in hub mode
# --------------------------------------------------------------------------
def _hub_robot(tmp_path: Path, **kw) -> _Robot:
    robot = _Robot(tmp_path, **kw)
    flag = tmp_path / "HUB_IS_ROS"
    flag.touch()
    robot.env["EMORI_HUB_FLAG"] = flag.as_posix()
    robot.flag = flag
    return robot


def test_v1_is_refused_under_the_hub_and_nothing_is_touched(tmp_path):
    robot = _hub_robot(tmp_path, fenced=True)

    result = robot.run("apply", "v1")

    assert result.returncode == 5
    assert result.kv["reason"] == "hub_mode" and result.kv["changed"] == "no"
    assert "hub-stack.sh apply hostapp" in result.stdout
    assert "reachy-mini-daemon" not in result.calls
    assert robot.fence.exists() and robot.flag.exists()


def test_v1_off_the_hub_is_unchanged_and_never_restarts_the_hub(tmp_path):
    robot = _Robot(tmp_path, fenced=True)

    result = robot.run("apply", "v1")

    assert result.returncode == 0 and result.kv["stack"] == "v1"
    assert "try-restart emori-hostapp emori-admin" in result.calls
    assert "emori-hub" not in result.calls


def test_a_v2_that_does_not_come_up_under_the_hub_hands_the_gateway_back(tmp_path):
    robot = _hub_robot(tmp_path, fenced=True, comes_up=False)
    (robot.state / "v2_up").unlink()

    result = robot.run("apply", "v2")

    assert result.returncode == 6 and result.kv["stack"] == "v1"
    calls = result.calls.splitlines()
    assert not robot.flag.exists()
    assert "systemctl stop emori-hub" in calls
    assert calls.index("systemctl stop emori-hub") < calls.index("systemctl start emori-hostapp")
    assert "systemctl start --no-block emori-kiosk" in calls


def test_a_v2_that_does_not_come_up_off_the_hub_starts_nothing_extra(tmp_path):
    robot = _Robot(tmp_path, fenced=True, comes_up=False)
    (robot.state / "v2_up").unlink()

    result = robot.run("apply", "v2")

    assert result.returncode == 6
    assert "emori-hub" not in result.calls
    assert "systemctl start emori-hostapp" not in result.calls


def test_the_stack_script_is_a_text_match_for_its_flag_variable():
    text = (_ROBOT / "ros2_native" / "v2-stack.sh").read_text(encoding="utf-8")

    assert 'HUB_FLAG="${EMORI_HUB_FLAG:-/opt/emori/HUB_IS_ROS}"' in text
    assert re.search(r"to_v1\(\)[^}]*emori-hub", text, re.S) is None


# --------------------------------------------------------------------------
# robot-mode.sh
# --------------------------------------------------------------------------
def _robot_mode(tmp_path: Path, *, hub_unit: bool, flag: bool, mode: str) -> str:
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    _stub(stubs / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
    _stub(stubs / "sleep", "exit 0\n")
    _stub(stubs / "curl", "exit 7\n")
    _stub(stubs / "systemctl", (
        f'echo "systemctl $*" >> "{log.as_posix()}"\n'
        'case "$1" in cat) [ "${STUB_HUB_UNIT:-0}" = 1 ] || [ "$2" != emori-hub ] ;; '
        "is-active) echo active ;; *) exit 0 ;; esac\n"))
    flag_path = tmp_path / "HUB_IS_ROS"
    if flag:
        flag_path.touch()
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
           "EMORI_HUB_FLAG": flag_path.as_posix(), "STUB_HUB_UNIT": "1" if hub_unit else "0"}
    subprocess.run([bash, _ROBOT_MODE.as_posix(), mode], capture_output=True, env=env,
                   timeout=60, check=False)
    return log.read_text(encoding="utf-8") if log.exists() else ""


def test_emori_mode_restarts_the_hub_not_the_host_app_when_the_flag_is_up(tmp_path):
    calls = _robot_mode(tmp_path, hub_unit=True, flag=True, mode="emori")

    assert "restart emori-camera-bridge emori-hub emori-kiosk" in calls
    assert "enable emori-hostapp emori-hub emori-camera-bridge emori-kiosk" in calls


def test_emori_mode_restarts_the_host_app_without_the_flag(tmp_path):
    calls = _robot_mode(tmp_path, hub_unit=True, flag=False, mode="emori")

    assert "restart emori-camera-bridge emori-hostapp emori-kiosk" in calls


def test_a_robot_without_the_hub_unit_is_handled_as_before(tmp_path):
    calls = _robot_mode(tmp_path, hub_unit=False, flag=False, mode="emori")

    assert "enable emori-hostapp emori-camera-bridge emori-kiosk" in calls
    assert "emori-hub" not in calls.replace("cat emori-hub", "")


def test_daemon_mode_stops_both_gateways(tmp_path):
    calls = _robot_mode(tmp_path, hub_unit=True, flag=True, mode="daemon")

    assert "stop emori-hostapp emori-hub emori-camera-bridge emori-kiosk" in calls
    assert "disable emori-hostapp emori-hub emori-camera-bridge emori-kiosk" in calls


# --------------------------------------------------------------------------
# the computer half
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def deploy():
    name = "deploy_to_robot_hub_under_test"
    spec = importlib.util.spec_from_file_location(name, _DEPLOY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def test_v1_is_refused_before_anything_is_sent_to_a_robot_that_runs_the_hub(deploy):
    with pytest.raises(deploy.DeployError, match="hub-stack.sh"):
        deploy.refuse_v1_in_hub_mode({"hub_flag": "yes"}, "v1")


@pytest.mark.parametrize("info, stack", [
    ({"hub_flag": "yes"}, "v2"), ({"hub_flag": "no"}, "v1"), ({}, "v1"), ({}, "v2")])
def test_every_other_combination_is_let_through(deploy, info, stack):
    deploy.refuse_v1_in_hub_mode(info, stack)


def test_the_preflight_reads_the_flag_and_stays_read_only(deploy):
    assert "HUB_IS_ROS" in deploy.PREFLIGHT and "hub_flag=" in deploy.PREFLIGHT
    for verb in ("systemctl", "rm ", "mv ", "tee ", "chown", "chmod", "docker run"):
        assert verb not in deploy.PREFLIGHT


def test_removing_add_ons_also_clears_the_hubs_site_directory(deploy, monkeypatch, tmp_path):
    bash = _bash()
    if bash is None:
        pytest.skip("no bash")
    root = tmp_path / "emori"
    (root / "addons" / "example").mkdir(parents=True)
    (root / "addons" / "example" / "pyproject.toml").write_text('name = "example"\n',
                                                                  encoding="utf-8")
    site = root / "hub-site" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "example.pth").write_text("x", encoding="utf-8")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs / "sudo", '[ "$1" = -n ] && shift\nexec "$@"\n')
    monkeypatch.setattr(deploy, "REMOTE_ROOT", root.as_posix())
    real_run = subprocess.run
    seen = {}

    def _run(argv, **kwargs):
        seen["answer"] = real_run(
            [bash, "-c", argv[-1]], capture_output=True, check=False,
            env={**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"})
        return seen["answer"]

    class _Ssh:
        host = "robot"

        def ssh(self, **_):
            return ["ssh", "robot"]

    monkeypatch.setattr(deploy.subprocess, "run", _run)

    deploy.remove_addons(_Ssh())

    assert not (root / "addons").exists()
    assert not (root / "hub-site").exists()
    assert "failed=" not in seen["answer"].stdout.decode()


# --------------------------------------------------------------------------
# nothing tracked carries a planning identifier
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", [
    _REFRESH, _FENCE, _KIOSK, _ROBOT_MODE, _ROBOT / "sudoers-emori-admin",
    _ROOT / "src" / "emori_adapter" / "admin" / "system.py",
])
def test_the_files_carry_no_planning_identifier(path):
    text = path.read_text(encoding="utf-8")

    assert not re.search(r"\b(T[0-9]{2,3}|S-[0-9]{2}|SP-[0-9]{2}|SG-[0-9]+|D[0-9]{1,2})\b", text)
