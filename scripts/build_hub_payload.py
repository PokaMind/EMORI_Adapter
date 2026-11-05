#!/usr/bin/env python3
"""Build the payload the robot turns into its hub image, or the motor's interface layer.

    python scripts/build_hub_payload.py --layers top  --output hub-payload.tar
    python scripts/build_hub_payload.py --layers base --output hub-payload.tar
    python scripts/build_hub_payload.py --layers motor --output motor-payload.tar
    python scripts/build_hub_payload.py --addon-wheels

The robot never receives a heavy layer and never builds a toolchain, so this runs on the
computer, inside the robot's own filesystem. That filesystem is an image imported once from
the robot (``docker export`` on the robot, ``docker import --platform linux/arm64`` here,
tagged ``emori/robot-buildenv:<id>``). Anything compiled in it matches the robot's ABI.
Every step is a throwaway ``docker run --rm`` container; no image is built, committed or
saved here, and the runner refuses any docker command outside the four it needs.

``--layers top`` holds the hub image's files (``Dockerfile.hub``, ``hub/``) with the colcon
install trees of ``emori_hub``, the graph's nodes it hosts and the two interface packages.
``base`` adds what the hub's base image installs (``Dockerfile.hub-base``, ``debs/``,
``wheels/``). ``motor`` holds only the interface layer for the motor image
(``motor/install-delta/``: ``reachy_mini_msgs``, ``reachy_mini_ros2``, ``emori_msgs``), for
``deploy/robot/ros2_native/motor-overlay.sh --delta``. ``--with-motor`` adds that layer to a
``top`` or ``base`` payload, compiled in the same run.

What goes in is HEAD, exported, and not the working tree: the image's label is the hash of
HEAD's files, and a label must not vouch for code no commit holds. Both the export and the
compile are done with line endings as the robot needs them, whatever this machine checks out.

``--addon-wheels`` downloads, for every ``addons/*/deploy/hub-constraints.txt``, the wheels
that file pins into that add-on's ``deploy/hub-wheels/``, for the hub's interpreter. It
needs neither Docker nor ``--layers``.

The apt closure is resolved against the image's own package index, and the compile runs one
job at a time: parallel compiler processes under arm64 emulation die of internal compiler
errors. That takes a quarter of an hour a package, so the compiled interface packages are kept
in ``build/hub-delta-cache/``, by their sources and the build environment, and a change to the
Python compiles nothing. Python files carry bytecode that checks its source by hash, because a
payload has no timestamps, the hub cannot write beside an image's files, and a later layer may
replace a source: bytecode that did not check would go on running the old one. A failed run
removes everything it wrote, so the output is whole or absent.

Exit: 0 built | 1 a step failed | 2 usage
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Callable, Iterable, Sequence

REPO = Path(__file__).resolve().parents[1]

BUILDENV_REPOSITORY = "emori/robot-buildenv"
ARCHITECTURE = "arm64"
DOCKER_PLATFORM = "linux/arm64"

# What `docker import` drops from the exported image. The ROS image these come from sets
# them, and the setup scripts and the toolchain read them.
BUILDENV_ENV = (
    "PATH=/opt/ros/jazzy/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG=C.UTF-8",
    "LC_ALL=C.UTF-8",
    "ROS_DISTRO=jazzy",
    "ROS_VERSION=2",
    "ROS_PYTHON_VERSION=3",
    "ROS_ROOT=/opt/ros/jazzy",
    "DEBIAN_FRONTEND=noninteractive",
)

# Where the image keeps its colcon workspace, and where this checkout's sources are mounted.
INSTALL_BASE = "/emori/ros2_ws/install"
SOURCE_MOUNT = "/src"
OUT_MOUNT = "/out"

# pip does not widen a manylinux_2_28 tag to the older ones, and some wheels (vosk, cffi,
# pydantic-core, webrtcvad-wheels) carry only an old one, so every tag the robot accepts is
# named.
PIP_PLATFORMS = (
    "manylinux2014_aarch64",
    "manylinux_2_17_aarch64",
    "manylinux_2_26_aarch64",
    "manylinux_2_27_aarch64",
    "manylinux_2_28_aarch64",
)
PIP_PYTHON_VERSION = "3.12"

# Pure Python pins PyPI has only as a source archive. Built into a wheel on this machine, which
# installs the same on the robot, whose image has no build backend for it.
SOURCE_ONLY_PINS = frozenset({"srt"})

# The colcon packages each image layer carries. The interface packages are in both: the hub
# image does not depend on the motor image having the same ones. The hub carries the graph's
# nodes too, so it never runs the older copies the motor image was built with.
INTERFACE_PACKAGES = ("reachy_mini_msgs", "emori_msgs")
HUB_PACKAGES = (*INTERFACE_PACKAGES, "emori_hub", "emori_adapter_ros", "emori_skill_native",
                "hri_context_analyzer", "hri_engagement_behaviors", "emori_skill_facade")
MOTOR_PACKAGES = (*INTERFACE_PACKAGES, "reachy_mini_ros2")
PYC_MODE = "checked-hash"
# What HEAD has to hold for a build. The add-ons are not in it, and have their own wheels.
EXPORTED_PATHS = ("deploy/hub", "deploy/ros2", "ros2_ws/src")
# Compiled interfaces are kept for this many sets of sources.
CACHE_KEEP = 4

# `docker` subcommands this script may run. None of them produces or stores an image.
ALLOWED_DOCKER = (("image", "inspect"), ("image", "ls"), ("run",), ("rm",))

TOP_FILES = (
    ("Dockerfile.hub", "deploy/hub/Dockerfile.hub", 0o644),
    ("hub/emori-hub-site.pth", "deploy/hub/emori-hub-site.pth", 0o644),
    ("hub/fastdds_peers.xml", "deploy/hub/fastdds_peers.xml", 0o644),
    ("hub/hub-entry.sh", "deploy/hub/hub-entry.sh", 0o755),
    ("hub/pw-record.sh", "deploy/hub/pw-record.sh", 0o755),
)
BASE_FILES = (("Dockerfile.hub-base", "deploy/hub/Dockerfile.hub-base", 0o644),)
MOTOR_FILES = (("Dockerfile.v2.robot.delta", "deploy/ros2/Dockerfile.v2.robot.delta", 0o644),)
CONSTRAINTS = "deploy/hub/constraints-hub.txt"
PACKAGE_LIST = "deploy/hub/hub-packages.txt"

LAYERS = ("top", "base", "motor")


class BuildError(Exception):
    """A step failed; the message says which, in a sentence."""


# --- the one runner ---------------------------------------------------------------------


Runner = Callable[..., "subprocess.CompletedProcess"]


def run_command(argv: Sequence[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    """Run one command. Every docker and pip invocation of this script goes through here, so
    a test replaces this one function and sees every argument vector.

    Output is shown as it arrives unless ``capture`` is set, because a compile takes a long
    time and a silent one looks hung.
    """

    try:
        return subprocess.run(list(argv), check=False, capture_output=capture, text=capture)
    except OSError as error:
        raise BuildError(f"could not run {argv[0]}: {error}") from error


def check_docker_argv(argv: Sequence[str]) -> None:
    """Refuse a docker command that could produce, store or move an image."""

    words = [word for word in argv[1:] if not word.startswith("-")]
    if not any(tuple(words[:len(allowed)]) == allowed for allowed in ALLOWED_DOCKER):
        raise BuildError(f"refusing docker {' '.join(words[:2])}: this script builds no image")
    if argv[1] == "run" and "--rm" not in argv:
        raise BuildError("refusing docker run without --rm: a container must not outlive its step")


# --- the pins ---------------------------------------------------------------------------


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path) -> list[tuple[str, str]]:
    """``(name, version)`` per ``name==version`` line; comments and blanks are skipped."""

    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;]+)", line)
        if match is None:
            raise BuildError(f"{path}: {line!r} is not a name==version pin")
        pins.append((normalise(match.group(1)), match.group(2)))
    return pins


def read_packages(path: Path) -> list[str]:
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.append(line)
    return names


def wheel_pins(directory: Path) -> set[tuple[str, str]]:
    found = set()
    for wheel in directory.glob("*.whl"):
        parts = wheel.name.split("-")
        if len(parts) >= 5:
            found.add((normalise(parts[0]), parts[1].replace("_", "-").lower()))
    return found


def pip_wheel_argv(name: str, version: str, destination: Path) -> list[str]:
    """Build one pure Python pin into a wheel, from its source archive."""

    return [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(destination),
            f"{name}=={version}"]


def pip_download_argv(constraints: Path, destination: Path) -> list[str]:
    """``pip download`` for the robot's interpreter, from the pins alone."""

    argv = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
            "--python-version", PIP_PYTHON_VERSION, "--implementation", "cp"]
    for platform in PIP_PLATFORMS:
        argv += ["--platform", platform]
    return argv + ["--dest", str(destination), "--requirement", str(constraints)]


# --- the tar ----------------------------------------------------------------------------


def _safe(name: str) -> str:
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts or name.startswith("/"):
        raise BuildError(f"an unsafe path in a container's output: {name!r}")
    return "/".join(parts)


def _stamp(data: bytes) -> int:
    """A file's time in the payload, from its content.

    Docker's builder takes a file in a build context to be unchanged if its size and time are,
    and keeps its earlier copy without looking inside. With one time for every file, a change
    that kept a file's size would silently never reach the image, under a label saying it had.
    From the content, a change shows, and the same input still gives the same bytes.
    """

    return 1 + int.from_bytes(hashlib.sha256(data).digest()[:3], "big")


class PayloadWriter:
    """A tar written in a fixed order with no owners and no clock in it, so two builds of the
    same inputs are the same bytes."""

    def __init__(self, path: Path) -> None:
        self._tar = tarfile.open(path, "w", format=tarfile.PAX_FORMAT)
        self._names: set[str] = set()

    def _info(self, name: str, kind: bytes, mode: int, size: int = 0) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type, info.mode, info.size = kind, mode, size
        info.mtime = info.uid = info.gid = 0
        info.uname = info.gname = ""
        return info

    def _claim(self, name: str) -> None:
        if name in self._names:
            raise BuildError(f"{name} would be in the payload twice")
        self._names.add(name)

    def add_bytes(self, name: str, data: bytes, mode: int) -> None:
        name = _safe(name)
        if not name:
            raise BuildError("a payload file needs a name")
        self._claim(name)
        self._add_data(name, data, mode)

    def _add_data(self, name: str, data: bytes, mode: int) -> None:
        info = self._info(name, tarfile.REGTYPE, mode, len(data))
        info.mtime = _stamp(data)
        self._tar.addfile(info, io.BytesIO(data))

    def add_file(self, name: str, source: Path, mode: int) -> None:
        self.add_bytes(name, source.read_bytes(), mode)

    def add_tree(self, prefix: str, source: tarfile.TarFile,
                 keep: Callable[[str], bool]) -> int:
        """Copy the members of a container's tar whose path ``keep`` accepts, under
        ``prefix``. Returns how many files were copied."""

        copied = 0
        for member in sorted(source.getmembers(), key=lambda item: item.name):
            relative = _safe(member.name)
            if not relative or not keep(relative):
                continue
            if member.islnk():
                raise BuildError(f"{member.name} is a hard link: tar --hard-dereference")
            name = f"{prefix}/{relative}"
            self._claim(name)
            if member.isdir():
                self._tar.addfile(self._info(name, tarfile.DIRTYPE, member.mode & 0o7777))
            elif member.issym():
                info = self._info(name, tarfile.SYMTYPE, member.mode & 0o7777)
                info.linkname = member.linkname
                self._tar.addfile(info)
            elif member.isreg():
                self._add_data(name, source.extractfile(member).read(), member.mode & 0o7777)
                copied += 1
            else:
                raise BuildError(f"{member.name} is neither a file, a directory nor a link")
        return copied

    def close(self) -> None:
        self._tar.close()


# --- the builder ------------------------------------------------------------------------


def _shell_words(words: Iterable[str]) -> str:
    return " ".join(f"'{word}'" for word in words)


def closure_script(packages: Sequence[str]) -> str:
    """The shell the closure container runs. ``--download-only`` fetches what installing the
    packages would add to this image, dependencies included, and nothing already in it."""

    return "\n".join([
        "set -eo pipefail",
        "apt-get update",
        "mkdir -p /tmp/debs/partial",
        "apt-get install -y --download-only --no-install-recommends"
        " -o Dir::Cache::archives=/tmp/debs -o APT::Sandbox::User=root"
        f" {_shell_words(packages)}",
        "rm -rf /tmp/debs/lock /tmp/debs/partial",
        "ls /tmp/debs/*.deb > /dev/null",
        f"tar -C /tmp/debs -cf {OUT_MOUNT}/debs.tar --owner=0 --group=0 .",
        _give_back(),
    ])


def _colcon_lines(packages: Sequence[str]) -> list[str]:
    selected = _shell_words(packages)
    trees = _shell_words(f"{INSTALL_BASE}/{name}" for name in packages)
    return [
        # The ROS setup scripts are not written for `set -e`, so it starts after them.
        "source /opt/ros/jazzy/setup.bash",
        f"source {INSTALL_BASE}/setup.bash",
        "set -e",
        "export MAKEFLAGS=-j1 CMAKE_BUILD_PARALLEL_LEVEL=1",
        "cd /tmp",
        f"colcon --log-base /tmp/log build --base-paths {SOURCE_MOUNT}"
        f" --build-base /tmp/build --install-base {INSTALL_BASE}"
        f" --packages-select {selected}"
        " --executor sequential --parallel-workers 1 --event-handlers console_direct+",
        f"python3 -m compileall -q -f --invalidation-mode {PYC_MODE} {trees}",
        f"cd {INSTALL_BASE}",
    ]


def compile_script(packages: Sequence[str]) -> str:
    """The shell the compile container runs for interface packages: the toolchain in, one job
    at a time, and the install trees of the named packages out."""

    return "\n".join([
        "set -o pipefail",
        "apt-get update || exit 1",
        "apt-get install -y --no-install-recommends build-essential || exit 1",
        *_colcon_lines(packages),
        f"tar -cf {OUT_MOUNT}/interfaces.tar --owner=0 --group=0 --hard-dereference"
        f" {_shell_words(packages)}",
        _give_back(),
    ])


def python_script(packages: Sequence[str]) -> str:
    """The same for Python packages, which need no toolchain, with the files at the top of the
    install tree too: the prefix scripts colcon just wrote, which a layer needs."""

    return "\n".join([
        "set -o pipefail",
        *_colcon_lines(packages),
        f"tar -cf {OUT_MOUNT}/python.tar --owner=0 --group=0 --hard-dereference"
        f" {_shell_words(packages)} $(find . -maxdepth 1 -type f -printf '%f\\n' | sort)",
        _give_back(),
    ])


def _give_back() -> str:
    # What the container wrote into the mounted folder belongs to whoever ran this script.
    return f'if [ -n "${{HOST_UID:-}}" ]; then chown -R "$HOST_UID:$HOST_GID" {OUT_MOUNT}; fi'


class PayloadBuilder:
    def __init__(self, repo: Path = REPO, runner: Runner = run_command, *,
                 addons: Path | None = None, cache: Path | None = None) -> None:
        self.repo = repo
        self.runner = runner
        self.addons = addons if addons is not None else repo / "addons"
        self.cache = cache
        self._base_id = ""
        self._containers: list[str] = []
        self._count = 0

    # -- the commands ----

    def _say(self, text: str) -> None:
        print(f"build_hub_payload: {text}", flush=True)

    def _docker(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
        argv = ["docker", *args]
        check_docker_argv(argv)
        return self.runner(argv, capture=capture)

    def _pip(self, constraints: Path, destination: Path) -> None:
        pins = read_pins(constraints)
        with tempfile.TemporaryDirectory(prefix="emori-pins-") as folder:
            binary = Path(folder) / "binary.txt"
            binary.write_text("".join(f"{name}=={version}\n" for name, version in pins
                                      if name not in SOURCE_ONLY_PINS), encoding="utf-8")
            result = self.runner(pip_download_argv(binary, destination), capture=False)
        if result.returncode != 0:
            raise BuildError(f"pip download failed for {constraints.name} (exit"
                             f" {result.returncode}); nothing was kept")
        for name, version in pins:
            if name not in SOURCE_ONLY_PINS:
                continue
            result = self.runner(pip_wheel_argv(name, version, destination), capture=False)
            if result.returncode != 0:
                raise BuildError(f"could not build a wheel of {name}=={version} (exit"
                                 f" {result.returncode}); nothing was kept")

    # -- what HEAD holds ----

    def _export(self, work: Path) -> Path:
        """HEAD's files, with the line endings in the repository."""

        archive = work / "head.tar"
        result = self.runner(
            ["git", "-C", str(self.repo), "-c", "core.autocrlf=false", "-c", "core.eol=lf",
             "archive", "--format=tar", f"--output={archive}", "HEAD", *EXPORTED_PATHS],
            capture=False)
        if result.returncode != 0:
            raise BuildError(f"git archive failed (exit {result.returncode})")
        tree = work / "head"
        tree.mkdir()
        with tarfile.open(archive) as exported:
            exported.extractall(tree, filter="data")
        archive.unlink()
        return tree

    # -- the build environment ----

    def resolve_base(self, base: str | None) -> str:
        """The image to build in: the one named, or the only ``emori/robot-buildenv`` tag."""

        if base is None:
            listed = self._docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}",
                                  BUILDENV_REPOSITORY, capture=True)
            if listed.returncode != 0:
                raise BuildError("docker did not list images: is it running?")
            tags = sorted({line.strip() for line in listed.stdout.splitlines()
                           if line.strip() and not line.strip().endswith(":<none>")})
            if len(tags) != 1:
                raise BuildError(
                    f"found {len(tags)} {BUILDENV_REPOSITORY} tags ({', '.join(tags) or 'none'});"
                    " name the one to use with --base")
            base = tags[0]
        found = self._docker("image", "inspect", "--format", "{{.Architecture}}|{{.Id}}", base,
                             capture=True)
        if found.returncode != 0:
            raise BuildError(f"there is no image {base}")
        architecture, _, self._base_id = found.stdout.strip().partition("|")
        if architecture != ARCHITECTURE:
            raise BuildError(f"{base} is {architecture or 'of unknown architecture'},"
                             f" not {ARCHITECTURE}: import the export again with"
                             f" --platform {DOCKER_PLATFORM}")
        return base

    def _container(self, base: str, work: Path, script: str, *,
                   mounts: Sequence[tuple[Path, str, bool]] = ()) -> None:
        self._count += 1
        name = f"emori-payload-{os.getpid()}-{self._count}"
        argv = ["run", "--rm", "--name", name, "--platform", DOCKER_PLATFORM,
                "--entrypoint", "bash"]
        for entry in BUILDENV_ENV:
            argv += ["--env", entry]
        if hasattr(os, "getuid"):
            argv += ["--env", f"HOST_UID={os.getuid()}", "--env", f"HOST_GID={os.getgid()}"]
        for source, target, readonly in mounts:
            argv += ["--mount", f"type=bind,source={source},target={target}"
                     + (",readonly" if readonly else "")]
        argv += ["--mount", f"type=bind,source={work},target={OUT_MOUNT}", base, "-c", script]
        self._containers.append(name)
        result = self._docker(*argv)
        if result.returncode != 0:
            raise BuildError(f"the container step failed (exit {result.returncode}); see the"
                             " output above")
        self._containers.remove(name)

    def _forget_containers(self) -> None:
        for name in self._containers:
            try:
                self._docker("rm", "--force", name, capture=True)
            except (BuildError, OSError):
                pass
        self._containers.clear()

    # -- the pieces ----

    def closure(self, base: str, work: Path, source: Path) -> Path:
        packages = read_packages(source / PACKAGE_LIST)
        if not packages:
            raise BuildError(f"{PACKAGE_LIST} names no package")
        self._say(f"resolving the apt closure of {len(packages)} packages in {base}")
        self._container(base, work, closure_script(packages))
        return self._expect(work / "debs.tar")

    def interfaces(self, base: str, work: Path, source: Path,
                   packages: Sequence[str]) -> Path:
        """The compiled interface packages: from the cache, or compiled here and kept there."""

        cached = None
        if self.cache is not None:
            cached = self.cache / f"{_interfaces_key(self._base_id, source, packages)}.tar"
            if cached.is_file():
                self._say(f"the compiled {', '.join(packages)} are in {self.cache}")
                return cached
        self._say(f"compiling {', '.join(packages)} in {base}, one job at a time")
        self._container(base, work, compile_script(packages),
                        mounts=[(source / "ros2_ws" / "src", SOURCE_MOUNT, True)])
        built = self._expect(work / "interfaces.tar")
        if cached is None:
            return built
        cached.parent.mkdir(parents=True, exist_ok=True)
        partial = cached.with_name(cached.name + ".partial")
        shutil.copyfile(built, partial)
        os.replace(partial, cached)
        kept = sorted(cached.parent.glob("*.tar"), key=lambda path: path.stat().st_mtime)
        for old in kept[:-CACHE_KEEP]:
            old.unlink()
        return cached

    def python(self, base: str, work: Path, source: Path, packages: Sequence[str]) -> Path:
        self._say(f"installing {', '.join(packages)} in {base}")
        self._container(base, work, python_script(packages),
                        mounts=[(source / "ros2_ws" / "src", SOURCE_MOUNT, True)])
        return self._expect(work / "python.tar")

    def wheels(self, work: Path, source: Path) -> Path:
        constraints = source / CONSTRAINTS
        pins = read_pins(constraints)
        destination = work / "wheels"
        destination.mkdir()
        self._say(f"downloading {len(pins)} wheels for python {PIP_PYTHON_VERSION}"
                  f" ({len(PIP_PLATFORMS)} platform tags)")
        self._pip(constraints, destination)
        self.check_wheels(pins, destination)
        return destination

    @staticmethod
    def check_wheels(pins: Sequence[tuple[str, str]], directory: Path) -> None:
        have = wheel_pins(directory)
        missing = [f"{name}=={version}" for name, version in pins if (name, version) not in have]
        if missing:
            raise BuildError("no wheel was downloaded for " + ", ".join(missing))

    @staticmethod
    def _expect(path: Path) -> Path:
        if not path.is_file() or path.stat().st_size == 0:
            raise BuildError(f"the container left no {path.name}")
        return path

    # -- the payload ----

    def build(self, layers: str, output: Path, *, base: str | None = None,
              with_motor: bool = False) -> Path:
        if layers not in LAYERS:
            raise BuildError(f"--layers is one of {', '.join(LAYERS)}")
        if layers == "motor" and with_motor:
            raise BuildError("--with-motor adds the motor layer to top or base")
        motor = layers == "motor" or with_motor
        packages: list[str] = []
        if layers != "motor":
            packages += HUB_PACKAGES
        if motor:
            packages += [name for name in MOTOR_PACKAGES if name not in packages]

        output = output.resolve()
        partial = output.with_name(output.name + ".partial")
        work = Path(tempfile.mkdtemp(prefix="emori-payload-"))
        try:
            self._remove(partial)
            image = self.resolve_base(base)
            source = self._export(work)
            closure = (self.closure(image, _subdir(work, "closure"), source)
                       if layers == "base" else None)
            compiled = self.interfaces(image, _subdir(work, "interfaces"), source,
                                       [name for name in packages if name in INTERFACE_PACKAGES])
            python = self.python(image, _subdir(work, "python"), source,
                                 [name for name in packages if name not in INTERFACE_PACKAGES])
            wheel_dir = self.wheels(work, source) if layers == "base" else None
            output.parent.mkdir(parents=True, exist_ok=True)
            writer = PayloadWriter(partial)
            try:
                self._assemble(writer, source, layers, with_motor, compiled, python, closure,
                               wheel_dir)
            finally:
                writer.close()
            os.replace(partial, output)
        except BaseException:
            self._forget_containers()
            self._remove(partial)
            raise
        finally:
            shutil.rmtree(work, ignore_errors=True)
        self._say(f"wrote {output} ({output.stat().st_size // 1024} KB)")
        return output

    def _assemble(self, writer: PayloadWriter, source: Path, layers: str, with_motor: bool,
                  compiled: Path, python: Path, closure: Path | None,
                  wheel_dir: Path | None) -> None:
        files: list[tuple[str, str, int]] = []
        if layers != "motor":
            files += TOP_FILES
        if layers == "base":
            files += BASE_FILES
        if layers == "motor" or with_motor:
            files += MOTOR_FILES
        for name, path, mode in files:
            writer.add_file(name, source / path, mode)

        with tarfile.open(compiled) as interfaces, tarfile.open(python) as installed:
            if layers != "motor":
                self._delta(writer, interfaces, installed, "hub/install-delta", HUB_PACKAGES)
            if layers == "motor" or with_motor:
                self._delta(writer, interfaces, installed, "motor/install-delta", MOTOR_PACKAGES)

        if closure is not None:
            with tarfile.open(closure) as debs:
                copied = writer.add_tree("debs", debs, lambda name: name.endswith(".deb"))
            if copied == 0:
                raise BuildError("the apt closure holds no .deb")
        if wheel_dir is not None:
            for wheel in sorted(wheel_dir.glob("*.whl")):
                writer.add_file(f"wheels/{wheel.name}", wheel, 0o644)
            writer.add_file("wheels/constraints-hub.txt", source / CONSTRAINTS, 0o644)

    @staticmethod
    def _delta(writer: PayloadWriter, interfaces: tarfile.TarFile, installed: tarfile.TarFile,
               prefix: str, packages: Sequence[str]) -> None:
        """The named packages' install trees, plus the files at the top of the install tree
        (the regenerated prefix scripts), which every layer needs to find its packages."""

        def keep(name: str) -> bool:
            top = name.split("/", 1)[0]
            return top in packages or ("/" not in name and not _is_directory(installed, name))

        copied = writer.add_tree(prefix, interfaces, lambda name: name.split("/", 1)[0] in packages)
        if copied + writer.add_tree(prefix, installed, keep) == 0:
            raise BuildError(f"the compile left no files for {prefix}")

    @staticmethod
    def _remove(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    # -- add-on wheels ----

    def addon_wheels(self) -> int:
        """Download each add-on's pinned wheels into its own ``deploy/hub-wheels/``. Returns
        how many add-ons had pins. A failed download leaves that folder as it was."""

        done = 0
        for constraints in sorted(self.addons.glob("*/deploy/hub-constraints.txt")):
            deploy = constraints.parent
            pins = read_pins(constraints)
            final = deploy / "hub-wheels"
            partial = deploy / "hub-wheels.partial"
            shutil.rmtree(partial, ignore_errors=True)
            partial.mkdir()
            try:
                self._say(f"downloading {len(pins)} wheels for {deploy.parent.name}")
                self._pip(constraints, partial)
                self.check_wheels(pins, partial)
                shutil.rmtree(final, ignore_errors=True)
                os.replace(partial, final)
            except BaseException:
                shutil.rmtree(partial, ignore_errors=True)
                raise
            done += 1
        if done == 0:
            self._say("no add-on pins hub dependencies")
        return done


def _interfaces_key(base: str, source: Path, packages: Sequence[str]) -> str:
    """Names one compile: the build environment's image id, the script, and every file of the
    sources. Not its tag, because an environment imported again can keep the tag."""

    digest = hashlib.sha256(f"{base}\n{compile_script(packages)}\n".encode())
    for package in sorted(packages):
        root = source / "ros2_ws" / "src" / "open" / package
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:24]


def _subdir(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir()
    return path


def _is_directory(archive: tarfile.TarFile, name: str) -> bool:
    try:
        return archive.getmember(name).isdir()
    except KeyError:
        return False


def main(argv: list[str] | None = None, runner: Runner = run_command,
         repo: Path = REPO) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--layers", choices=LAYERS,
                        help="top: hub files and install trees | base: top plus the apt closure"
                             " and wheels | motor: the motor's interface layer only")
    parser.add_argument("--output", type=Path,
                        help="the tar to write (default: hub-payload-<layers>.tar)")
    parser.add_argument("--base", metavar="IMAGE",
                        help=f"the imported build environment (default: the only"
                             f" {BUILDENV_REPOSITORY} tag present)")
    parser.add_argument("--with-motor", action="store_true",
                        help="also build the motor's interface layer into a top or base payload")
    parser.add_argument("--addon-wheels", action="store_true",
                        help="download each add-on's pinned wheels into its deploy/hub-wheels/")
    args = parser.parse_args(argv)
    if args.layers is None and not args.addon_wheels:
        parser.error("name --layers, --addon-wheels, or both")
    if args.layers is None and (args.output or args.base or args.with_motor):
        parser.error("--output, --base and --with-motor go with --layers")
    if args.with_motor and args.layers == "motor":
        parser.error("--with-motor adds the motor layer to top or base")

    builder = PayloadBuilder(repo, runner, cache=repo / "build" / "hub-delta-cache")
    try:
        if args.addon_wheels:
            builder.addon_wheels()
        if args.layers is not None:
            output = args.output or Path(f"hub-payload-{args.layers}.tar")
            builder.build(args.layers, output, base=args.base, with_motor=args.with_motor)
    except BuildError as error:
        print(f"!! {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("!! interrupted; nothing was kept", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
