"""The ROS 2 packages are publishable, checked as text.

All eleven packages are Apache-2.0, like the rest of this repository (`LICENSING.md`). Four
of them -- `emori_msgs`, `hri_context_analyzer`, `hri_engagement_behaviors` and
`emori_skill_facade` -- are meant to be released on the ROS index on their own. The
packages under `ros2_ws/src/emori/` use the emori_adapter core and travel with it.

None of that can be verified by running the packages: the things that break a release are
a missing `LICENSE`, a `<url>` `bloom-release` needs and cannot find, a `CHANGELOG.rst`
whose top entry disagrees with `package.xml`, or a "standalone" image that quietly
installs the application it is supposed to prove it does not need. All of those are
text, and all of them are cheaper to catch here than three steps into a `bloom-release`
or -- worse -- after someone has the tarball.

The expensive half of the same claim is `scripts/ros_standalone_check.py`, which builds
the image and starts the nodes. This file is what runs on every push.
"""

from __future__ import annotations

import ast
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_WS = _ROOT / "ros2_ws" / "src"
# The directory split: `open/` holds standalone packages that never import the
# emori_adapter core and never depend on an `emori/` package; `emori/` holds the packages
# that use the core. colcon recurses into both, so the build does not care; a reader does.
_SRC = _WS / "open"
_CORE_HALF = _WS / "emori"
_DOCKERFILE = _ROOT / "deploy" / "ros2" / "Dockerfile.standalone"
_SMOKE = _ROOT / "deploy" / "ros2" / "standalone_smoke.sh"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_LICENSING = _ROOT / "LICENSING.md"
_ROOT_LICENSE = _ROOT / "LICENSE"
_RELEASE_DOC = _ROOT / "docs" / "ros4hri-release.md"

# The four, and only the four. `emori_adapter_ros` and `fiware_bridge` import the
# emori_adapter core by design and are deliberately absent from this list: a package that
# needs the core cannot be installed from the ROS index on its own.
PACKAGES = ("emori_msgs", "hri_context_analyzer", "hri_engagement_behaviors",
            "emori_skill_facade")
# Standalone robot-driver code anyone may use, not yet prepared for a ROS index release,
# so the bloom-readiness checks below stay on the four.
OPEN_NOT_YET_RELEASED = ("reachy_mini_msgs", "reachy_mini_ros2")
# The packages that use the core. `emori_skill_native` is v2's bridge: it imports the
# core's skill mapping on purpose. `emori_hub` implements the core's robot client.
USES_CORE = ("emori_adapter_ros", "fiware_bridge", "emori_adapter_bringup",
             "emori_skill_native", "emori_hub")
# Third-party code and data that keep their own licence, each listed in LICENSING.md.
THIRD_PARTY = ("ros2_ws/src/open/reachy_mini_ros2/THIRD_PARTY_NOTICES.md",
               "deploy/ros2/models/LICENSE.face_detection_yunet.txt",
               "src/emori_adapter/hostapp/static/jsqr.js")

# The canonical repository URL. `docs/ros4hri-release.md` names it too; change both
# together.
REPOSITORY_URL = "https://github.com/pokamind/EMORI_Adapter"
BUGTRACKER_URL = "https://github.com/pokamind/EMORI_Adapter/issues"
VERSION = "0.1.0"


def _package_xml(name: str) -> ElementTree.Element:
    return ElementTree.parse(_SRC / name / "package.xml").getroot()


def _text(root: ElementTree.Element, tag: str) -> str:
    node = root.find(tag)
    return " ".join((node.text or "").split()) if node is not None else ""


def _setup_licence(setup: Path) -> str | None:
    """The `license=` keyword of the `setup(...)` call, read with `ast`."""

    for node in ast.walk(ast.parse(setup.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "license" and isinstance(keyword.value, ast.Constant):
                    return keyword.value.value
    return None


# --- 1. the licence is present, not merely declared ----------------------------------


@pytest.mark.parametrize("package", PACKAGES)
def test_every_published_package_ships_the_apache_text(package):
    """`<license>Apache-2.0</license>` is a claim; the LICENSE file is the grant.

    The four `package.xml` files have said Apache-2.0 since the packages were created.
    Without the text next to them that is a promise nobody can act on: a recipient has
    no copy of the terms, and the ROS index will not take the package.
    """

    text = (_SRC / package / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text
    assert "Version 2.0" in text
    # Section 9 exists in the full text and in none of the one-paragraph summaries that
    # people paste in by mistake.
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in text
    assert "Accepting Warranty or Additional Liability" in text


def test_the_repository_licence_is_the_apache_text_the_packages_ship():
    """The root LICENSE is the same grant, byte for byte, as every package's copy."""

    assert _ROOT_LICENSE.is_file(), "the repository has no root LICENSE"
    assert _ROOT_LICENSE.read_bytes() == (_SRC / "emori_msgs" / "LICENSE").read_bytes()


@pytest.mark.parametrize("package", USES_CORE)
def test_the_packages_that_use_the_core_carry_the_apache_licence(package):
    """The `emori/` half is Apache-2.0 too, and says so everywhere a tool looks.

    `<license>` is what the ROS tooling, and anyone skimming the package, reads first;
    the LICENSE file is the grant itself, and setup.py is what a Python installer reads.
    """

    directory = _CORE_HALF / package
    assert directory.is_dir(), f"{package} is not under ros2_ws/src/emori/ - has it moved?"
    licence = directory / "LICENSE"
    assert licence.is_file(), f"{package} has no LICENSE"
    assert licence.read_bytes() == _ROOT_LICENSE.read_bytes(), (
        f"{package}/LICENSE is not a copy of the root LICENSE")
    root = ElementTree.parse(directory / "package.xml").getroot()
    assert _text(root, "license") == "Apache-2.0"
    setup = directory / "setup.py"
    if setup.exists():
        assert _setup_licence(setup) == "Apache-2.0", f"{package}/setup.py"


def test_the_directory_split_is_the_dependency_split():
    """Every package sits in the half its dependencies say, and nowhere else.

    A package dropped straight into `ros2_ws/src/`, or into the wrong half, is a package
    nobody classified. The two halves must list exactly the packages this file knows,
    so the next one has to be classified here before it can land.
    """

    def packages(directory: Path) -> set[str]:
        return {child.name for child in directory.iterdir()
                if (child / "package.xml").is_file()}

    assert packages(_SRC) == set(PACKAGES) | set(OPEN_NOT_YET_RELEASED)
    assert packages(_CORE_HALF) == set(USES_CORE)
    assert packages(_WS) == set(), "a ROS package sits outside open/ and emori/"


@pytest.mark.parametrize("package", PACKAGES + OPEN_NOT_YET_RELEASED)
def test_everything_in_the_open_half_carries_its_licence(package):
    text = (_SRC / package / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text and "Version 2.0" in text
    assert _text(_package_xml(package), "license") == "Apache-2.0"
    readme = (_SRC / package / "README.md").read_text(encoding="utf-8")
    assert "Apache-2.0" in readme


def test_the_licensing_is_written_down():
    """Every package, and every third-party file with a licence of its own, is listed."""

    text = _LICENSING.read_text(encoding="utf-8")
    for package in PACKAGES + OPEN_NOT_YET_RELEASED + USES_CORE:
        assert package in text
    assert "Apache-2.0" in text
    for path in THIRD_PARTY:
        assert path in text, f"LICENSING.md does not list {path}"
        assert (_ROOT / path).is_file(), f"{path} is listed but missing"


def test_vendored_code_in_the_open_half_carries_its_upstream_notice():
    """Code copied from somebody else keeps their copyright, in a place a recipient reads.

    `reachy_mini_ros2/gaze/` holds three files vendored from Pollen Robotics' SDK
    (Apache-2.0, Pollen's copyright). "Everything here is Apache-2.0, Copyright Pokamind"
    is then only true with an exception, and the exception has to be written down where
    the package travels - its own THIRD_PARTY_NOTICES.md - and in LICENSING.md. A file
    that says "Vendored from" is the trigger, so the next vendored file is held to it.
    """

    licensing = _LICENSING.read_text(encoding="utf-8")
    vendored = [path for path in sorted(_SRC.rglob("*.py"))
                if "__pycache__" not in path.parts
                and "Vendored from" in path.read_text(encoding="utf-8")]
    assert len(vendored) >= 3, "the vendored gaze files are gone - update LICENSING.md"
    for path in vendored:
        package = path.relative_to(_SRC).parts[0]
        notices = _SRC / package / "THIRD_PARTY_NOTICES.md"
        assert notices.is_file(), f"{package} vendors code but has no THIRD_PARTY_NOTICES.md"
        relative = path.relative_to(_SRC / package).as_posix()
        assert f"`{relative}`" in notices.read_text(encoding="utf-8"), (
            f"{package}/{relative} is vendored but not listed in its THIRD_PARTY_NOTICES.md")
        assert f"ros2_ws/src/open/{package}/THIRD_PARTY_NOTICES.md" in licensing


# --- 2. package.xml metadata a release tool can read ---------------------------------


@pytest.mark.parametrize("package", PACKAGES)
def test_the_manifest_carries_what_bloom_reads(package):
    root = _package_xml(package)
    assert _text(root, "name") == package
    assert _text(root, "version") == VERSION
    assert _text(root, "license") == "Apache-2.0"
    assert "@" in _text(root, "maintainer") or root.find("maintainer").get("email")
    assert _text(root, "author"), f"{package} has no <author>"

    urls = {node.get("type"): (node.text or "").strip() for node in root.findall("url")}
    assert urls.get("repository") == REPOSITORY_URL
    assert urls.get("bugtracker") == BUGTRACKER_URL


@pytest.mark.parametrize("package", PACKAGES)
def test_the_description_says_something_a_stranger_could_use(package):
    """The index rejects a template description, and so should we."""

    description = _text(_package_xml(package), "description")
    assert len(description) > 120, f"{package}: {description!r} is a placeholder"
    assert "TODO" not in description


@pytest.mark.parametrize("package", PACKAGES)
def test_nothing_published_names_the_core(package):
    """`emori_adapter` in a manifest or a setup.py is a dependency on the adapter core.

    `tests/test_ros_standalone.py` makes the same check over the Python sources with
    `ast`. This one covers the two files that test does not read, and they are exactly
    the files a package installer acts on.
    """

    for filename in ("package.xml", "setup.py"):
        path = _SRC / package / filename
        if not path.exists():  # emori_msgs is ament_cmake: no setup.py
            continue
        assert "emori_adapter" not in path.read_text(encoding="utf-8"), f"{package}/{filename}"


# Every element a package.xml (format 2 or 3) declares a dependency with.
_DEPEND_TAGS = ("depend", "build_depend", "build_export_depend", "buildtool_depend",
                "buildtool_export_depend", "exec_depend", "run_depend", "test_depend",
                "doc_depend")


def _manifest_dependencies(manifest: Path) -> set[str]:
    root = ElementTree.parse(manifest).getroot()
    return {" ".join((node.text or "").split())
            for tag in _DEPEND_TAGS for node in root.findall(tag)}


def _open_packages() -> list[str]:
    return sorted(d.name for d in _SRC.iterdir() if (d / "package.xml").is_file())


def test_no_open_manifest_depends_on_the_core_half():
    """`<exec_depend>fiware_bridge</exec_depend>` is a dependency on the core, too.

    The check above looks for the string `emori_adapter`, and only in the four. A
    dependency on any package under `emori/` - or the core - in ANY open manifest,
    `reachy_mini_*` included, is the same broken promise: rosdep cannot resolve it
    outside this repository, and the package cannot be installed alone.
    """

    core_half = {d.name for d in _CORE_HALF.iterdir() if (d / "package.xml").is_file()}
    assert set(USES_CORE) <= core_half
    forbidden = core_half | {"emori_adapter"}
    offenders = [f"{package}/package.xml: {dependency}"
                 for package in _open_packages()
                 for dependency in sorted(_manifest_dependencies(_SRC / package / "package.xml")
                                          & forbidden)]
    assert offenders == [], f"the open half depends on the emori/ half: {offenders}"


def test_the_manifest_dependency_reader_sees_every_kind_of_depend(tmp_path):
    """The check above is only worth its green if it reads every `*depend` element."""

    manifest = tmp_path / "package.xml"
    manifest.write_text(
        '<?xml version="1.0"?>\n<package format="3">\n  <name>x</name>\n'
        + "".join(f"  <{tag}> dep_{tag} </{tag}>\n" for tag in _DEPEND_TAGS)
        + "  <export><build_type>ament_python</build_type></export>\n</package>\n",
        encoding="utf-8")
    assert _manifest_dependencies(manifest) == {f"dep_{tag}" for tag in _DEPEND_TAGS}


# --- 3. README and CHANGELOG ---------------------------------------------------------


@pytest.mark.parametrize("package", PACKAGES)
def test_every_published_package_has_a_readme_that_says_how_to_build_it_alone(package):
    text = (_SRC / package / "README.md").read_text(encoding="utf-8")
    assert text.startswith(f"# {package}")
    assert "Apache-2.0" in text
    # "Build it alone" is the whole claim; a README that only explains the EMORI graph
    # leaves a reader with no way to use the package.
    assert "colcon build --packages-select" in text


@pytest.mark.parametrize("package", PACKAGES)
def test_the_changelog_top_entry_matches_the_manifest_version(package):
    """`bloom` reads both, and a disagreement fails the release, not the build."""

    text = (_SRC / package / "CHANGELOG.rst").read_text(encoding="utf-8")
    assert f"Changelog for package {package}" in text
    assert f"{VERSION} (2026-09-16)" in text


# --- 4. the standalone image proves what it claims -----------------------------------


def test_the_standalone_image_cannot_install_the_core():
    """One `pip` and this image stops being evidence of anything.

    The point of `Dockerfile.standalone` is an environment in which the EMORI
    application does not exist, so a package that secretly needs it cannot start. An
    installer in the image is an invitation to "just add the dependency", and the next
    person will.
    """

    text = _DOCKERFILE.read_text(encoding="utf-8")
    assert "pip" not in text, "the standalone image must not be able to install anything"
    assert "FROM ros:humble-ros-base" in text
    assert "ros-humble-hri-msgs" in text
    assert "colcon build" in text


def test_the_standalone_image_copies_only_the_four_packages():
    """`COPY ros2_ws ./ros2_ws` would drag the bridge in and void the whole check."""

    copied: list[str] = []
    for line in _DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("COPY "):
            copied.append(stripped.split()[1])

    from_workspace = sorted(path for path in copied if path.startswith("ros2_ws"))
    assert from_workspace == sorted(f"ros2_ws/src/open/{name}" for name in PACKAGES)

    # Nothing of the application, by any route.
    for forbidden in ("src", "./src", "pyproject.toml", "configs", "ros2_ws",
                      "ros2_ws/src", "ros2_ws/src/open", "ros2_ws/src/emori"):
        assert forbidden not in copied, f"the standalone image copies {forbidden!r}"


def test_the_smoke_asks_the_graph_rather_than_the_build():
    """A colcon build says nothing about whether a node can import what it needs."""

    text = _SMOKE.read_text(encoding="utf-8")
    for node in ("/hri_context_analyzer", "/hri_engagement_behaviors", "/skill_facade"):
        assert node in text
    assert "ros2 node list" in text
    # The two topics: one proves the behaviour lane and the facade agreed on the wire,
    # the other proves both ledgers are publishing.
    assert "ros2 topic list" in text
    assert "/skill/set_expression" in text
    assert "/diagnostics" in text
    # `log` is what makes the facade runnable with no hostapp, no key and no network.
    assert "EMORI_SKILL_TRANSPORT" in text and "log" in text


def test_the_release_doc_names_the_repository():
    """The release how-to names the same repository the manifests point at."""

    text = _RELEASE_DOC.read_text(encoding="utf-8")
    assert REPOSITORY_URL in text
    assert "bloom-release" in text
    assert "rosdistro" in text


# --- 5. CI runs both of them ---------------------------------------------------------


def _ci() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


@pytest.mark.parametrize("job", ["ros2-standalone", "ros2-graph"])
def test_ci_has_the_job(job):
    assert job in _ci()["jobs"], f"{job} is missing from .github/workflows/ci.yml"


def test_ci_still_runs_the_python_suite():
    """The two Docker jobs are additions; losing the unit suite to gain them is a loss."""

    test_job = _ci()["jobs"]["test"]
    runs = " ".join(str(step.get("run", "")) for step in test_job["steps"])
    assert "pytest tests" in runs
    assert "ruff check src tests" in runs


def test_the_standalone_job_builds_the_standalone_dockerfile():
    steps = " ".join(str(step.get("run", "")) for step in _ci()["jobs"]["ros2-standalone"]["steps"])
    assert "deploy/ros2/Dockerfile.standalone" in steps
    assert "linux/amd64" in steps


def test_the_graph_job_asserts_the_nodes_and_the_two_topics():
    steps = " ".join(str(step.get("run", "")) for step in _ci()["jobs"]["ros2-graph"]["steps"])
    assert "deploy/ros2/Dockerfile" in steps
    assert "EMORI_SOURCE=mock" in steps
    assert "EMORI_WITH_FIWARE=false" in steps
    assert "ros2 node list" in steps
    for node in ("/emori_adapter", "/hri_context_analyzer", "/hri_engagement_behaviors",
                 "/skill_facade"):
        assert node in steps
    assert "/skill/set_expression" in steps
    assert "/humans/persons/learner_01/engagement_status" in steps
