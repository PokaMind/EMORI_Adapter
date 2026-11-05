"""The payload builder, run against a fake runner.

Docker and pip are not run here. Every command the script issues goes through one function,
so these tests replace it, record each argument vector, and write the files a real step would
leave behind. They prove what the script asks for and what it keeps, not that the ABI of a
compiled package matches the robot: that needs the real build.
"""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "build_hub_payload.py"
_NAME = "build_hub_payload_under_test"
_BASE = "emori/robot-buildenv:4d4b21d31b23"


@pytest.fixture(scope="module")
def builder():
    spec = importlib.util.spec_from_file_location(_NAME, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(_NAME, None)


def _tar(files: dict[str, bytes], dirs=(), links=None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            archive.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            archive.addfile(info)
    return buffer.getvalue()


def _mount_source(argv, target: str) -> Path:
    for index, word in enumerate(argv):
        if word == "--mount" and f"target={target}" in argv[index + 1]:
            return Path(re.search(r"source=([^,]+)", argv[index + 1]).group(1))
    raise AssertionError(f"no mount at {target}")


class FakeRunner:
    """Stands in for the one function that runs commands."""

    def __init__(self, tags=(_BASE,), architecture="arm64", fail=None):
        self.calls: list[list[str]] = []
        self.tags = list(tags)
        self.architecture = architecture
        self.image_id = "sha256:one"
        self.fail = fail
        self.wheels_missing: set[str] = set()
        self.pip_dirs: list[Path] = []

    def __call__(self, argv, capture=False):
        argv = list(argv)
        self.calls.append(argv)
        if self.fail is not None and self.fail(argv):
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[0] == "docker":
            return self._docker(argv)
        if argv[0] == "git":
            return self._git(argv)
        return self._pip(argv)

    def _git(self, argv):
        """``git archive``, of this checkout's files as they are."""

        output = next(word.split("=", 1)[1] for word in argv if word.startswith("--output="))
        paths = argv[argv.index("HEAD") + 1:]
        with tarfile.open(output, "w") as archive:
            for path in paths:
                for file in sorted((_ROOT / path).rglob("*")):
                    if file.is_file() and "__pycache__" not in file.parts:
                        archive.add(file, arcname=file.relative_to(_ROOT).as_posix())
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _docker(self, argv):
        if argv[1:3] == ["image", "ls"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(self.tags) + "\n", "")
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                argv, 0, f"{self.architecture}|{self.image_id}\n", "")
        if argv[1] == "run":
            script = argv[-1]
            out = _mount_source(argv, "/out")
            if "download-only" in script:
                (out / "debs.tar").write_bytes(_tar(
                    {"libportaudio2_1_arm64.deb": b"deb", "pipewire-bin_1_arm64.deb": b"deb",
                     "README": b"not a package"}, dirs=["."]))
            else:
                names = re.search(r"--packages-select ((?:'[\w]+' ?)+)", script).group(1)
                packages = re.findall(r"'(\w+)'", names)
                files = {"unrelated_pkg/share/x": b"x"}
                if "build-essential" in script:
                    name = "interfaces.tar"
                else:
                    name = "python.tar"
                    files.update({"setup.bash": b"#", "local_setup.bash": b"#",
                                  ".colcon_install_layout": b"isolated"})
                for package in packages:
                    files[f"{package}/share/{package}/package.xml"] = b"<package/>"
                (out / name).write_bytes(_tar(
                    files, dirs=[*packages, "unrelated_pkg"],
                    links={f"{packages[0]}/lib/link": "../share"}))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _pip(self, argv):
        if "wheel" in argv:
            destination = Path(argv[argv.index("--wheel-dir") + 1])
            name, version = argv[-1].split("==")
            (destination / f"{name}-{version}-py3-none-any.whl").write_bytes(b"w")
            return subprocess.CompletedProcess(argv, 0, "", "")
        destination = Path(argv[argv.index("--dest") + 1])
        self.pip_dirs.append(destination)
        constraints = Path(argv[argv.index("--requirement") + 1])
        for line in constraints.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.split("==")[0].lower() in self.wheels_missing:
                continue
            name, version = line.split("==")
            (destination / f"{name.replace('-', '_')}-{version}-py3-none-any.whl").write_bytes(b"w")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def docker(self):
        return [call for call in self.calls if call[0] == "docker"]

    def runs(self):
        return [call for call in self.docker() if call[1] == "run"]


def _members(path: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(path) as archive:
        return {member.name: member for member in archive.getmembers()}


def _build(builder, tmp_path, layers, runner=None, **kwargs):
    runner = runner or FakeRunner()
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path / "addons")
    return maker, runner, maker.build(layers, tmp_path / "out" / "payload.tar", **kwargs)


# --- what the payload holds -------------------------------------------------------------


def test_a_top_payload_holds_the_hub_files_and_only_the_hub_install_trees(builder, tmp_path):
    _, _, output = _build(builder, tmp_path, "top")
    names = set(_members(output))
    assert {"Dockerfile.hub", "hub/emori-hub-site.pth", "hub/fastdds_peers.xml", "hub/pw-record.sh",
            "hub/hub-entry.sh"} <= names
    assert "hub/install-delta/emori_hub/share/emori_hub/package.xml" in names
    assert "hub/install-delta/reachy_mini_msgs/share/reachy_mini_msgs/package.xml" in names
    assert "hub/install-delta/emori_adapter_ros/share/emori_adapter_ros/package.xml" in names
    assert "hub/install-delta/setup.bash" in names
    assert "hub/install-delta/.colcon_install_layout" in names
    assert not any(name.startswith(("debs", "wheels", "motor")) for name in names)
    assert not any("unrelated_pkg" in name for name in names)
    assert "Dockerfile.hub-base" not in names


def test_the_install_tree_keeps_its_links_and_the_entry_script_is_executable(builder, tmp_path):
    _, _, output = _build(builder, tmp_path, "top")
    members = _members(output)
    assert members["hub/install-delta/reachy_mini_msgs/lib/link"].issym()
    assert members["hub/hub-entry.sh"].mode == 0o755
    assert members["hub/pw-record.sh"].mode == 0o755


def test_a_base_payload_adds_the_closure_the_wheels_and_a_copy_of_the_constraints(
        builder, tmp_path):
    _, _, output = _build(builder, tmp_path, "base")
    names = set(_members(output))
    assert {"Dockerfile.hub", "Dockerfile.hub-base", "debs/libportaudio2_1_arm64.deb",
            "debs/pipewire-bin_1_arm64.deb", "wheels/constraints-hub.txt"} <= names
    assert "debs/README" not in names
    pins = builder.read_pins(_ROOT / "deploy" / "hub" / "constraints-hub.txt")
    assert len(pins) > 30
    wheels = {name for name in names if name.startswith("wheels/") and name.endswith(".whl")}
    assert len(wheels) == len(pins)
    copied = tarfile.open(output).extractfile("wheels/constraints-hub.txt").read()
    assert copied == (_ROOT / "deploy" / "hub" / "constraints-hub.txt").read_bytes()


def test_a_motor_payload_holds_only_the_interface_layer(builder, tmp_path):
    _, runner, output = _build(builder, tmp_path, "motor")
    names = set(_members(output))
    assert "Dockerfile.v2.robot.delta" in names
    for package in ("reachy_mini_msgs", "reachy_mini_ros2", "emori_msgs"):
        assert f"motor/install-delta/{package}/share/{package}/package.xml" in names
    assert "motor/install-delta/setup.bash" in names
    assert not any(name.startswith(("hub/", "debs", "wheels")) for name in names)
    assert "Dockerfile.hub" not in names and "emori_hub" not in " ".join(names)
    assert len(runner.runs()) == 2 and runner.pip_dirs == []


def test_with_motor_adds_the_layer_to_a_hub_payload_from_one_build(builder, tmp_path):
    _, runner, output = _build(builder, tmp_path, "top", with_motor=True)
    names = set(_members(output))
    assert "hub/install-delta/emori_hub/share/emori_hub/package.xml" in names
    assert "motor/install-delta/reachy_mini_ros2/share/reachy_mini_ros2/package.xml" in names
    assert "hub/install-delta/reachy_mini_ros2/share/reachy_mini_ros2/package.xml" not in names
    assert "motor/install-delta/emori_hub/share/emori_hub/package.xml" not in names
    assert len(runner.runs()) == 2, "one compile, one install of the Python"


def test_a_change_that_keeps_a_files_size_changes_its_time(builder, tmp_path):
    """Docker's builder keeps its copy of a context file whose size and time are unchanged."""

    payload = tmp_path / "payload.tar"
    built = tarfile.open(fileobj=io.BytesIO(_tar({"pkg/one": b"AAAA"})))
    other = tarfile.open(fileobj=io.BytesIO(_tar({"pkg/one": b"BBBB"})))
    for source, name in ((built, "a"), (other, "b")):
        writer = builder.PayloadWriter(payload)
        writer.add_tree(name, source, lambda _name: True)
        writer.add_bytes(f"{name}.bytes", source.extractfile("pkg/one").read(), 0o644)
        writer.close()
        members = _members(payload)
        assert members[f"{name}/pkg/one"].size == 4
        assert members[f"{name}/pkg/one"].mtime == members[f"{name}.bytes"].mtime
        assert members[f"{name}/pkg/one"].mtime > 0
        if name == "a":
            first = members["a/pkg/one"].mtime
    assert members["b/pkg/one"].mtime != first


def test_two_builds_of_the_same_inputs_are_the_same_bytes(builder, tmp_path):
    _, _, first = _build(builder, tmp_path, "top")
    data = first.read_bytes()
    first.unlink()
    _, _, second = _build(builder, tmp_path, "top")
    assert second.read_bytes() == data


# --- the commands -----------------------------------------------------------------------


def test_the_compile_runs_in_the_imported_environment_with_its_env_set_again(builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "top")
    run = runner.runs()[0]
    assert run[run.index("--entrypoint") + 1] == "bash"
    assert run[run.index("--platform") + 1] == "linux/arm64"
    assert _BASE in run and "--rm" in run
    env = {run[index + 1] for index, word in enumerate(run) if word == "--env"}
    assert {"ROS_DISTRO=jazzy", "LANG=C.UTF-8", "DEBIAN_FRONTEND=noninteractive"} <= env
    assert any(entry.startswith("PATH=") and "/usr/bin" in entry for entry in env)
    script = run[-1]
    assert "build-essential" in script and "apt-get update" in script
    assert "MAKEFLAGS=-j1" in script
    assert "--executor sequential --parallel-workers 1" in script
    assert "source /opt/ros/jazzy/setup.bash" in script
    assert "--install-base /emori/ros2_ws/install" in script
    assert "--packages-select 'reachy_mini_msgs' 'emori_msgs'" in script


def test_only_the_interfaces_need_the_toolchain(builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "top")
    compiled, installed = runner.runs()
    assert "--packages-select 'reachy_mini_msgs' 'emori_msgs'" in compiled[-1]
    assert "build-essential" not in installed[-1] and "apt-get" not in installed[-1]
    for package in ("emori_hub", "emori_adapter_ros", "emori_skill_native",
                    "hri_context_analyzer", "hri_engagement_behaviors", "emori_skill_facade"):
        assert f"'{package}'" in installed[-1], package
        assert f"'{package}'" not in compiled[-1], package


def test_python_carries_bytecode_that_needs_no_timestamp(builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "top")
    for run in runner.runs():
        assert f"compileall -q -f --invalidation-mode {builder.PYC_MODE}" in run[-1]
        assert "/emori/ros2_ws/install/" in run[-1].split("compileall", 1)[1]


def test_a_source_replaced_by_a_later_layer_is_the_one_that_runs(builder, tmp_path):
    """The motor overlay copies .py files over a layer that holds their bytecode."""

    package = tmp_path / "package_under_test"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    module = package / "thing.py"
    module.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "compileall", "-q", "-f", "--invalidation-mode",
                    builder.PYC_MODE, str(package)], check=True)
    os.utime(module, (0, 0))
    module.write_text("VALUE = 'new'\n", encoding="utf-8")
    os.utime(module, (0, 0))
    done = subprocess.run(
        [sys.executable, "-c", "import package_under_test.thing as t; print(t.VALUE)"],
        cwd=tmp_path, capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "new"


def test_a_build_environment_imported_again_under_its_tag_compiles_again(builder, tmp_path):
    runner, _ = _cached(builder, tmp_path)
    assert any("build-essential" in run[-1] for run in runner.runs())
    again = FakeRunner()
    again.image_id = "sha256:two"
    _cached(builder, tmp_path, runner=again)
    assert any("build-essential" in run[-1] for run in again.runs())


def test_what_is_built_is_head_with_the_line_endings_in_the_repository(builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "top")
    git = [call for call in runner.calls if call[0] == "git"]
    assert len(git) == 1
    assert "core.autocrlf=false" in git[0] and "core.eol=lf" in git[0]
    assert "archive" in git[0] and "HEAD" in git[0]
    for path in ("deploy/hub", "deploy/ros2", "ros2_ws/src"):
        assert path in git[0]


def test_a_failed_export_builds_nothing(builder, tmp_path):
    runner = FakeRunner(fail=lambda argv: argv[0] == "git")
    with pytest.raises(builder.BuildError, match="git archive"):
        _build(builder, tmp_path, "top", runner=runner)
    assert runner.runs() == []


def test_the_exported_sources_are_mounted_read_only(builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "top")
    for run in runner.runs():
        sources = [run[i + 1] for i, word in enumerate(run) if word == "--mount"
                   and "target=/src" in run[i + 1]]
        assert len(sources) == 1 and sources[0].endswith(",readonly")
        assert "head/ros2_ws/src" in sources[0].replace("\\", "/")


def _cached(builder, tmp_path, layers="top", runner=None):
    runner = runner or FakeRunner()
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path / "addons",
                                   cache=tmp_path / "cache")
    return runner, maker.build(layers, tmp_path / "out" / "payload.tar")


def test_the_compiled_interfaces_are_kept_and_a_python_change_compiles_nothing(builder, tmp_path):
    first, output = _cached(builder, tmp_path)
    assert any("build-essential" in run[-1] for run in first.runs())
    data = output.read_bytes()
    second, output = _cached(builder, tmp_path)
    assert not any("build-essential" in run[-1] for run in second.runs())
    assert len(second.runs()) == 1
    assert output.read_bytes() == data


def test_a_change_to_an_interface_compiles_again(builder, tmp_path, monkeypatch):
    _cached(builder, tmp_path)
    original = builder.compile_script
    monkeypatch.setattr(builder, "compile_script", lambda packages: original(packages) + "\n#")
    again, _ = _cached(builder, tmp_path)
    assert any("build-essential" in run[-1] for run in again.runs())


def test_the_key_of_a_compile_follows_the_build_environment_and_every_file(builder, tmp_path):
    root = tmp_path / "ros2_ws" / "src" / "open" / "emori_msgs"
    root.mkdir(parents=True)
    (root / "A.msg").write_text("int32 a\n", encoding="utf-8")
    first = builder._interfaces_key("env:1", tmp_path, ["emori_msgs"])
    assert builder._interfaces_key("env:1", tmp_path, ["emori_msgs"]) == first
    assert builder._interfaces_key("env:2", tmp_path, ["emori_msgs"]) != first
    (root / "A.msg").write_text("int32 b\n", encoding="utf-8")
    assert builder._interfaces_key("env:1", tmp_path, ["emori_msgs"]) != first


def test_the_cache_keeps_a_few_and_lets_go_of_the_oldest(builder, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(builder.CACHE_KEEP + 2):
        old = cache / f"{index}.tar"
        old.write_bytes(b"x")
        os.utime(old, (index + 1, index + 1))
    _cached(builder, tmp_path)
    kept = sorted(path.name for path in cache.glob("*.tar"))
    assert len(kept) == builder.CACHE_KEEP and "0.tar" not in kept


def test_the_command_line_uses_the_cache_and_a_test_does_not(builder, tmp_path):
    assert builder.PayloadBuilder(_ROOT, FakeRunner()).cache is None
    source = _SCRIPT.read_text(encoding="utf-8")
    assert 'cache=repo / "build" / "hub-delta-cache"' in source


def test_the_closure_is_resolved_in_its_own_container_before_the_toolchain_goes_in(
        builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "base")
    first, second, _third = runner.runs()
    assert "download-only" in first[-1] and "build-essential" not in first[-1]
    assert "build-essential" in second[-1]
    for package in builder.read_packages(_ROOT / "deploy" / "hub" / "hub-packages.txt"):
        assert f"'{package}'" in first[-1]
    assert "--no-install-recommends" in first[-1]


def test_the_wheel_download_names_every_platform_tag_and_installs_no_dependencies(
        builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "base")
    pip = [call for call in runner.calls if "download" in call]
    assert len(pip) == 1
    argv = pip[0]
    tags = [argv[i + 1] for i, word in enumerate(argv) if word == "--platform"]
    assert tags == ["manylinux2014_aarch64", "manylinux_2_17_aarch64", "manylinux_2_26_aarch64",
                    "manylinux_2_27_aarch64", "manylinux_2_28_aarch64"]
    assert "--no-deps" in argv and "--only-binary=:all:" in argv
    assert argv[argv.index("--python-version") + 1] == "3.12"
    assert "--requirement" in argv


def test_a_pin_with_only_a_source_archive_is_built_into_a_wheel(builder, tmp_path):
    runner, seen = FakeRunner(), []
    original = runner._pip

    def pip(argv):
        if "download" in argv:
            seen.extend(Path(argv[argv.index("--requirement") + 1]).read_text(
                encoding="utf-8").splitlines())
        return original(argv)

    runner._pip = pip
    _, _, output = _build(builder, tmp_path, "base", runner=runner)
    built = [call for call in runner.calls if "wheel" in call]
    assert [call[-1] for call in built] == ["srt==3.5.3"]
    assert "--no-deps" in built[0]
    assert not any(line.startswith("srt==") for line in seen)
    assert "wheels/srt-3.5.3-py3-none-any.whl" in _members(output)


def test_a_wheel_that_cannot_be_built_fails_the_build(builder, tmp_path):
    runner = FakeRunner(fail=lambda argv: "wheel" in argv)
    with pytest.raises(builder.BuildError, match="srt==3.5.3"):
        _build(builder, tmp_path, "base", runner=runner)
    assert not (tmp_path / "out" / "payload.tar").exists()


def test_the_only_docker_commands_are_inspect_list_run_and_remove(builder, tmp_path):
    _, runner, _ = _build(builder, tmp_path, "base", with_motor=True)
    forbidden = {"build", "buildx", "commit", "save", "export", "import", "load", "push", "pull",
                 "tag", "rmi", "create", "cp", "start", "exec"}
    for call in runner.docker():
        words = [word for word in call[1:] if not word.startswith("-")]
        assert words[0] not in forbidden, call
        assert words[:2] == ["image", "inspect"] or words[:2] == ["image", "ls"] \
            or words[0] in ("run", "rm")
        assert call[1] != "run" or "--rm" in call


def test_the_runner_refuses_a_docker_command_that_would_make_or_store_an_image(builder):
    for argv in (["docker", "commit", "c", "i"], ["docker", "save", "i"],
                 ["docker", "export", "c"], ["docker", "build", "."], ["docker", "tag", "a", "b"],
                 ["docker", "image", "save", "i"], ["docker", "push", "i"]):
        with pytest.raises(builder.BuildError):
            builder.check_docker_argv(argv)
    with pytest.raises(builder.BuildError):
        builder.check_docker_argv(["docker", "run", "image", "bash"])
    builder.check_docker_argv(["docker", "run", "--rm", "image", "bash"])
    builder.check_docker_argv(["docker", "image", "inspect", "--format", "x", "image"])


# --- the build environment --------------------------------------------------------------


def test_the_base_defaults_to_the_only_buildenv_tag_present(builder, tmp_path):
    runner = FakeRunner(tags=["emori/robot-buildenv:abc"])
    _build(builder, tmp_path, "top", runner=runner)
    assert "emori/robot-buildenv:abc" in runner.runs()[0]
    listing = runner.docker()[0]
    assert listing[1:3] == ["image", "ls"] and "emori/robot-buildenv" in listing


@pytest.mark.parametrize("tags", [[], ["emori/robot-buildenv:a", "emori/robot-buildenv:b"]])
def test_no_default_base_is_chosen_when_there_are_none_or_several(builder, tmp_path, tags):
    runner = FakeRunner(tags=tags)
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path)
    with pytest.raises(builder.BuildError, match="--base"):
        maker.build("top", tmp_path / "p.tar")
    assert runner.runs() == [] and not (tmp_path / "p.tar").exists()


def test_a_named_base_is_used_as_given(builder, tmp_path):
    runner = FakeRunner(tags=["emori/robot-buildenv:a", "emori/robot-buildenv:b"])
    _build(builder, tmp_path, "top", runner=runner, base="emori/robot-buildenv:b")
    assert "emori/robot-buildenv:b" in runner.runs()[0]
    assert not any(call[1:3] == ["image", "ls"] for call in runner.docker())


def test_a_base_that_is_not_arm64_stops_before_any_container_runs(builder, tmp_path):
    runner = FakeRunner(architecture="amd64")
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path)
    with pytest.raises(builder.BuildError, match="amd64"):
        maker.build("top", tmp_path / "p.tar")
    assert runner.runs() == []


# --- failure leaves nothing -------------------------------------------------------------


def _leftovers(tmp_path):
    return sorted(path.name for path in (tmp_path / "out").glob("*")) \
        if (tmp_path / "out").exists() else []


@pytest.mark.parametrize("which", ["compile", "closure", "pip"])
def test_a_failed_step_leaves_no_output_and_no_partial_file(builder, tmp_path, which):
    marker = {"compile": "colcon", "closure": "download-only", "pip": "pip"}[which]
    runner = FakeRunner(fail=lambda argv: marker in " ".join(argv)
                        and (which != "pip" or argv[0] != "docker"))
    before = {path.name for path in Path(tempfile.gettempdir()).glob("emori-payload-*")}
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path)
    with pytest.raises(builder.BuildError):
        maker.build("base", tmp_path / "out" / "payload.tar")
    assert _leftovers(tmp_path) == []
    assert {path.name for path in Path(tempfile.gettempdir()).glob("emori-payload-*")} <= before


def test_a_failure_while_writing_removes_the_partial_tar(builder, tmp_path, monkeypatch):
    def broken(self, *args, **kwargs):
        raise builder.BuildError("boom")

    monkeypatch.setattr(builder.PayloadBuilder, "_delta", staticmethod(broken))
    maker = builder.PayloadBuilder(_ROOT, FakeRunner(), addons=tmp_path)
    with pytest.raises(builder.BuildError, match="boom"):
        maker.build("top", tmp_path / "out" / "payload.tar")
    assert _leftovers(tmp_path) == []


def test_an_interrupt_removes_the_container_that_was_running(builder, tmp_path):
    class Interrupted(FakeRunner):
        def __call__(self, argv, capture=False):
            if argv[0] == "docker" and argv[1] == "run":
                self.calls.append(list(argv))
                raise KeyboardInterrupt
            return super().__call__(argv, capture)

    runner = Interrupted()
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path)
    with pytest.raises(KeyboardInterrupt):
        maker.build("top", tmp_path / "out" / "payload.tar")
    removed = [call for call in runner.docker() if call[1] == "rm"]
    named = runner.runs()[0][runner.runs()[0].index("--name") + 1]
    assert removed and removed[0][-1] == named
    assert _leftovers(tmp_path) == []


def test_a_wheel_the_download_did_not_produce_fails_the_build(builder, tmp_path):
    runner = FakeRunner()
    runner.wheels_missing = {"vosk"}
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path)
    with pytest.raises(builder.BuildError, match="vosk==0.3.45"):
        maker.build("base", tmp_path / "out" / "payload.tar")
    assert _leftovers(tmp_path) == []


def test_a_failed_build_keeps_an_earlier_output_untouched(builder, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "payload.tar").write_bytes(b"earlier")
    runner = FakeRunner(fail=lambda argv: "colcon" in " ".join(argv))
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path)
    with pytest.raises(builder.BuildError):
        maker.build("top", out / "payload.tar")
    assert (out / "payload.tar").read_bytes() == b"earlier"
    assert _leftovers(tmp_path) == ["payload.tar"]


# --- add-on wheels ----------------------------------------------------------------------


def _addon(tmp_path, name, pins):
    deploy = tmp_path / "addons" / name / "deploy"
    deploy.mkdir(parents=True)
    (deploy / "hub-constraints.txt").write_text(pins, encoding="utf-8")
    return deploy


def test_add_on_wheels_are_downloaded_per_add_on_with_the_same_tags(builder, tmp_path):
    first = _addon(tmp_path, "alpha", "# pins\nfoo==1.0\nbar-baz==2.1\n")
    second = _addon(tmp_path, "beta", "qux==3\n")
    (tmp_path / "addons" / "gamma" / "deploy").mkdir(parents=True)
    runner = FakeRunner()
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path / "addons")
    assert maker.addon_wheels() == 2
    assert sorted(path.name for path in (first / "hub-wheels").iterdir()) \
        == ["bar_baz-2.1-py3-none-any.whl", "foo-1.0-py3-none-any.whl"]
    assert [path.name for path in (second / "hub-wheels").iterdir()] == ["qux-3-py3-none-any.whl"]
    assert not (first / "hub-wheels.partial").exists()
    assert runner.docker() == []
    for argv in runner.calls:
        tags = [argv[i + 1] for i, word in enumerate(argv) if word == "--platform"]
        assert tags == list(builder.PIP_PLATFORMS) and "--no-deps" in argv


def test_a_failed_add_on_download_keeps_the_wheels_it_had_and_leaves_no_partial(
        builder, tmp_path):
    deploy = _addon(tmp_path, "alpha", "foo==1.0\n")
    (deploy / "hub-wheels").mkdir()
    (deploy / "hub-wheels" / "old-0-py3-none-any.whl").write_bytes(b"old")
    runner = FakeRunner(fail=lambda argv: True)
    maker = builder.PayloadBuilder(_ROOT, runner, addons=tmp_path / "addons")
    with pytest.raises(builder.BuildError):
        maker.addon_wheels()
    assert [path.name for path in deploy.iterdir() if path.is_dir()] == ["hub-wheels"]
    assert (deploy / "hub-wheels" / "old-0-py3-none-any.whl").is_file()


# --- the command line -------------------------------------------------------------------


def test_the_command_line_needs_layers_or_add_on_wheels(builder, capsys):
    with pytest.raises(SystemExit) as stop:
        builder.main([], runner=FakeRunner())
    assert stop.value.code == 2
    with pytest.raises(SystemExit):
        builder.main(["--layers", "motor", "--with-motor"], runner=FakeRunner())
    with pytest.raises(SystemExit):
        builder.main(["--addon-wheels", "--base", "x"], runner=FakeRunner())


def test_the_command_line_builds_what_deploy_to_robot_asks_for(builder, tmp_path):
    runner = FakeRunner()
    destination = tmp_path / "x" / "hub-payload.tar"
    assert builder.main(["--layers", "top", "--output", str(destination)], runner=runner) == 0
    assert destination.is_file()
    deploy = (_ROOT / "scripts" / "deploy_to_robot.py").read_text(encoding="utf-8")
    assert '"--layers", action' in deploy and '"--output", str(destination)' in deploy


def test_the_command_line_reports_a_failure_and_exits_one(builder, tmp_path, capsys):
    code = builder.main(["--layers", "top", "--output", str(tmp_path / "p.tar")],
                        runner=FakeRunner(tags=[]))
    assert code == 1 and "--base" in capsys.readouterr().err
    assert not (tmp_path / "p.tar").exists()


def test_the_script_is_executable_with_unix_line_endings():
    assert b"\r" not in _SCRIPT.read_bytes()
    mode = subprocess.run(["git", "ls-files", "-s", str(_SCRIPT)], cwd=_ROOT, capture_output=True,
                          text=True, check=False).stdout
    assert mode.startswith("100755"), mode


# --- the motor overlay takes the compiled layer -----------------------------------------


_OVERLAY = _ROOT / "deploy" / "robot" / "ros2_native" / "motor-overlay.sh"
_DELTA_FILE = _ROOT / "deploy" / "ros2" / "Dockerfile.v2.robot.delta"
_bash = shutil.which("bash")


def test_the_delta_dockerfile_copies_either_form_and_labels_the_interfaces():
    lines = [line for line in _DELTA_FILE.read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    assert "COPY install-delta/ /emori/ros2_ws/install/" in lines
    assert "ARG EMORI_V2_INTERFACES=none" in lines
    assert "LABEL org.pokamind.emori.v2-interfaces=$EMORI_V2_INTERFACES" in lines
    assert "ARG BASE=emori/ros2-jazzy-v2:robot-base" in lines
    assert "LABEL org.pokamind.emori.v2-source=$EMORI_V2_SOURCE" in lines


def test_the_overlay_keeps_its_python_form_and_its_rollback():
    text = _OVERLAY.read_text(encoding="utf-8")
    for needle in ("FILES=(arbiter.py rest_shim.py robot_node.py ros_surface.py "
                   "state_snapshot.py)", "--rollback)", "docker tag \"$BASE\" \"$IMAGE\"",
                   "--delta)", "EMORI_V2_INTERFACES=$interfaces", "session-busy.sh"):
        assert needle in text, needle
    assert "docker build --pull=false" in text.replace("sudo -n ", "")


class _OverlayRobot:
    """motor-overlay.sh on a scratch root, against stubs for sudo and docker."""

    def __init__(self, tmp_path: Path, images: tuple[str, ...]) -> None:
        self.root = tmp_path / "opt"
        script = self.root / "deploy/robot/ros2_native/motor-overlay.sh"
        script.parent.mkdir(parents=True)
        shutil.copy2(_OVERLAY, script)
        self.script = script
        (self.root / "deploy/ros2").mkdir(parents=True)
        (self.root / "deploy/ros2/Dockerfile.v2.robot.delta").write_text("FROM x\n")
        source = self.root / "ros2_ws/src/open/reachy_mini_ros2/reachy_mini_ros2"
        source.mkdir(parents=True)
        for name in ("arbiter.py", "rest_shim.py", "robot_node.py", "ros_surface.py",
                     "state_snapshot.py"):
            (source / name).write_text("#\n")
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        self.log = tmp_path / "docker.log"
        self.images = tmp_path / "images"
        self.images.write_text("\n".join(images) + "\n")
        (stubs / "sudo").write_text('#!/bin/bash\n[ "$1" = -n ] && shift\nexec "$@"\n', newline="\n")
        (stubs / "docker").write_text(
            '#!/bin/bash\n'
            f'echo "docker $*" >> "{self.log.as_posix()}"\n'
            'if [ "$1" = image ] && [ "$2" = inspect ]; then\n'
            '  last="${!#}"\n'
            f'  grep -qxF "$last" "{self.images.as_posix()}" || exit 1\n'
            '  case "$*" in\n'
            '    *Labels*) echo abc123 ;;\n'
            '    *"-f"*) if grep -q "^same $last \\|^same .* $last$" '
            f'"{self.images.as_posix()}"; then echo sha256:same; '
            'else echo "sha256:$(echo "$last" | cut -d: -f2)"; fi ;;\n'
            '  esac\n'
            'fi\n', newline="\n")
        for stub in (stubs / "sudo", stubs / "docker"):
            stub.chmod(0o755)
        self.stubs = stubs

    def same_image(self, first: str, second: str) -> None:
        """Two tags on one id. The stub reads an id from what follows the colon."""

        text = self.images.read_text(encoding="utf-8")
        self.images.write_text(text + f"same {first} {second}\n", encoding="utf-8")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.stubs.as_posix()}{os.pathsep}{os.environ['PATH']}"}
        return subprocess.run([_bash, self.script.as_posix(), *args], capture_output=True, text=True,
                              check=False, env=env, cwd=self.root, timeout=60)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


_MOTOR = "emori/ros2-jazzy-v2:robot"
_MOTOR_BASE = "emori/ros2-jazzy-v2:robot-base"
_MOTOR_INTERFACES = "emori/ros2-jazzy-v2:robot-interfaces"


@pytest.mark.skipif(_bash is None, reason="needs bash")
def test_a_python_layer_goes_on_the_base_when_no_interface_layer_exists(tmp_path):
    robot = _OverlayRobot(tmp_path, (_MOTOR, _MOTOR_BASE))
    done = robot.run()
    assert done.returncode == 0, done.stderr
    build = next(line for line in robot.calls() if line.startswith("docker build"))
    assert f"BASE={_MOTOR_BASE}" in build and "EMORI_V2_INTERFACES=none" in build


@pytest.mark.skipif(_bash is None, reason="needs bash")
def test_a_python_layer_keeps_the_compiled_interfaces_under_it(tmp_path):
    robot = _OverlayRobot(tmp_path, (_MOTOR, _MOTOR_BASE, _MOTOR_INTERFACES))
    done = robot.run()
    assert done.returncode == 0, done.stderr
    build = next(line for line in robot.calls() if line.startswith("docker build"))
    assert f"BASE={_MOTOR_INTERFACES}" in build and "EMORI_V2_INTERFACES=abc123" in build
    assert not any(line.startswith(f"docker tag {_MOTOR} ") for line in robot.calls())


@pytest.mark.skipif(_bash is None, reason="needs bash")
def test_an_interface_layer_is_remembered(tmp_path):
    robot = _OverlayRobot(tmp_path, (_MOTOR, _MOTOR_BASE))
    delta = tmp_path / "motor" / "install-delta" / "reachy_mini_ros2"
    delta.mkdir(parents=True)
    (delta / "x").write_text("x")
    done = robot.run("--delta", (tmp_path / "motor").as_posix())
    assert done.returncode == 0, done.stderr
    build = next(line for line in robot.calls() if line.startswith("docker build"))
    assert f"BASE={_MOTOR_BASE}" in build and "EMORI_V2_INTERFACES=none" not in build
    assert f"docker tag {_MOTOR} {_MOTOR_INTERFACES}" in robot.calls()


@pytest.mark.skipif(_bash is None, reason="needs bash")
def test_a_python_layer_never_removes_the_image_behind_the_interface_tag(tmp_path):
    robot = _OverlayRobot(tmp_path, (_MOTOR, _MOTOR_BASE, _MOTOR_INTERFACES))
    robot.same_image(_MOTOR, _MOTOR_INTERFACES)
    done = robot.run()
    assert done.returncode == 0, done.stderr
    assert not [line for line in robot.calls() if line.startswith("docker image rm")]


@pytest.mark.skipif(_bash is None, reason="needs bash")
def test_a_rollback_removes_the_interface_tag_after_the_restart(tmp_path):
    robot = _OverlayRobot(tmp_path, (_MOTOR, _MOTOR_BASE, _MOTOR_INTERFACES))
    stack = robot.root / "deploy/robot/ros2_native/v2-stack.sh"
    stack.write_text(f'#!/bin/bash\necho "v2-stack $*" >> "{robot.log.as_posix()}"\n', newline="\n")
    done = robot.run("--rollback")
    assert done.returncode == 0, done.stderr
    calls = robot.calls()
    assert (calls.index("v2-stack apply v2")
            < calls.index(f"docker image rm {_MOTOR_INTERFACES}"))


@pytest.mark.skipif(_bash is None, reason="needs bash")
def test_the_overlay_script_parses():
    assert subprocess.run([_bash, "-n", _OVERLAY.as_posix()], check=False).returncode == 0


@pytest.mark.skipif(_bash is None, reason="needs bash")
@pytest.mark.parametrize("args,code", [
    (["--delta"], 2), (["--rollback", "--delta", "x"], 2), (["--bogus"], 2),
])
def test_the_overlay_rejects_a_bad_command_line(args, code):
    assert subprocess.run([_bash, _OVERLAY.as_posix(), *args], capture_output=True,
                          check=False).returncode == code


@pytest.mark.skipif(_bash is None, reason="needs bash")
def test_the_overlay_refuses_a_folder_that_is_not_a_motor_layer(tmp_path):
    empty = subprocess.run([_bash, _OVERLAY.as_posix(), "--delta", tmp_path.as_posix()], capture_output=True,
                           text=True, check=False)
    assert empty.returncode == 3 and "install-delta" in empty.stderr
    (tmp_path / "install-delta" / "emori_hub").mkdir(parents=True)
    wrong = subprocess.run([_bash, _OVERLAY.as_posix(), "--delta", tmp_path.as_posix()], capture_output=True,
                           text=True, check=False)
    assert wrong.returncode == 3 and "reachy_mini_ros2" in wrong.stderr


def test_nothing_new_names_a_plan_identifier_or_a_date():
    banned = re.compile(r"\b(T\d\d|S-\d\d|SP-\d\d|SG-\d)\b|2026-\d\d-\d\d", re.IGNORECASE)
    for path in (_SCRIPT, _OVERLAY, _DELTA_FILE, Path(__file__)):
        assert banned.search(path.read_text(encoding="utf-8")) is None, path


def test_a_missing_docker_is_a_reported_failure_not_a_traceback(builder, tmp_path, capsys):
    code = builder.main(["--layers", "top", "--output", str(tmp_path / "p.tar")],
                        runner=lambda argv, capture=False: builder.run_command(
                            ["emori-no-such-command", *argv[1:]], capture=capture))
    assert code == 1 and "could not run" in capsys.readouterr().err
    assert not (tmp_path / "p.tar").exists()
