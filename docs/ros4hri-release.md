# Releasing the ROS 2 packages

This is the maintainer guide for releasing the standalone ROS 2 packages to the ROS index
with `bloom-release`: what is already in place, how to check it locally, and the commands
for the release itself.

The standalone packages are the six in `ros2_ws/src/open/`: `emori_msgs`,
`hri_context_analyzer`, `hri_engagement_behaviors`, `emori_skill_facade`,
`reachy_mini_msgs` and `reachy_mini_ros2`. None of them imports `emori_adapter` or
depends on a package under `ros2_ws/src/emori/`, so each can be built without the rest of
this repository. The five packages under `ros2_ws/src/emori/` (`emori_adapter_ros`,
`fiware_bridge`, `emori_adapter_bringup`, `emori_skill_native`, `emori_hub`) use the
`emori_adapter` core, a pip package with no rosdep key; they are built from this repository (the
`deploy/ros2/` images and `deploy/robot/ros2_native/`) and are not candidates for the
index. See [`LICENSING.md`](../LICENSING.md) for the licences and
[`ros2_ws/src/open/README.md`](../ros2_ws/src/open/README.md) for the packages
themselves.

## Which packages, on which distro

| Package | Distro | Notes |
|---|---|---|
| `emori_msgs` | Humble | Interfaces only. Release it first: the others depend on it. |
| `hri_context_analyzer` | Humble | Every dependency resolves through `rosdep`. |
| `hri_engagement_behaviors` | Humble | Every dependency resolves through `rosdep`; `pyhri` is deliberately not declared (see below). |
| `emori_skill_facade` | Humble | Every dependency resolves through `rosdep`. |
| `reachy_mini_msgs` | Jazzy | Interfaces only. |
| `reachy_mini_ros2` | Jazzy | Also needs Pollen's `reachy-mini` SDK and a few other Python packages from pip, which have no rosdep keys (listed in its `README.md`). A binary from the build farm installs without them, so its release notes must say how to install them. |

The checks in `tests/test_ros_publishing.py` (metadata, changelog, README) cover the four
Humble packages.

## What is already in place

| Item | Where |
|---|---|
| Apache-2.0 `LICENSE` in each package (the same text as the root `LICENSE`) | `ros2_ws/src/open/<pkg>/LICENSE` |
| `README.md` in each: purpose, topics/actions/parameters, how to build it alone | `ros2_ws/src/open/<pkg>/README.md` |
| `CHANGELOG.rst` with a `0.1.0 (2026-09-16)` entry in the format `catkin_generate_changelog` writes | `ros2_ws/src/open/<pkg>/CHANGELOG.rst` |
| `package.xml`: `Apache-2.0`, version `0.1.0`, a real `<description>`, `<maintainer>`, `<author>`, `<url type="repository">`, `<url type="bugtracker">` | each `package.xml` |
| A front door and contributor rules for the standalone packages | `ros2_ws/src/open/README.md`, `ros2_ws/src/open/CONTRIBUTING.md` |
| An image that builds and runs the four Humble packages with **no EMORI core installed** | `deploy/ros2/Dockerfile.standalone` + `deploy/ros2/standalone_smoke.sh` |
| A one-command check of that image | `scripts/ros_standalone_check.py` |
| CI jobs `ros2-standalone` (build + smoke the four packages) and `ros2-graph` (build the full image, bring the graph up on the mock source, assert the nodes and topics) | `.github/workflows/ci.yml` |
| Tests pinning all of the above | `tests/test_ros_publishing.py`, `tests/test_ros_standalone.py` |

### The repository URL

`<url type="repository">` and `<url type="bugtracker">` in every `package.xml` read

    https://github.com/pokamind/EMORI_Adapter
    https://github.com/pokamind/EMORI_Adapter/issues

`bloom-release` reads them and the `ros/rosdistro` index entry repeats them. If the
repository ever moves, change them in the `package.xml` files and in `REPOSITORY_URL` /
`BUGTRACKER_URL` in `tests/test_ros_publishing.py`, and nowhere else.

## Verifying locally

From the repository root:

```bash
# 1. the metadata, the licences and the standalone Dockerfile, as text
python -m pytest tests/test_ros_publishing.py

# 2. no package reaches back into the EMORI core
python -m pytest tests/test_ros_standalone.py

# 3. the expensive proof: build the image and start all three nodes in it
python scripts/ros_standalone_check.py
```

(3) builds `emori/ros-standalone:amd64` from `deploy/ros2/Dockerfile.standalone`, runs
`deploy/ros2/standalone_smoke.sh` inside it, and exits non-zero unless
`/hri_context_analyzer`, `/hri_engagement_behaviors` and `/skill_facade` are all in
`ros2 node list` and `/skill/set_expression` and `/diagnostics` are both in
`ros2 topic list`. The facade runs with `EMORI_SKILL_TRANSPORT=log`, which accepts and
records every request without a host application, a shared key, a network or a robot —
the node's whole path executes and nothing can move.

## Preparing a source repository

`bloom-release` releases every package it finds in the source repository, and this one
also holds the `ros2_ws/src/emori/` packages, which the build farm cannot build. There are
two ways to handle that:

* **Release from this repository** and list the packages that must not be released, one
  name per line, in the release repository's ignore file for that distro (for example
  `humble.ignored`). A Humble release ignores the four `emori/` packages and the two
  Jazzy-only `reachy_mini_*` packages.
* **Export the packages to a separate source repository.** `git subtree split --prefix
  ros2_ws/src/open` takes the whole directory and cannot leave a package out; to export
  only some packages, use `git filter-repo` in a **fresh clone** of this repository (it
  rewrites the clone it runs in and refuses any other):

```bash
git clone https://github.com/pokamind/EMORI_Adapter emori_ros4hri && cd emori_ros4hri
git filter-repo \
    --path ros2_ws/src/open/emori_msgs/ \
    --path ros2_ws/src/open/hri_context_analyzer/ \
    --path ros2_ws/src/open/hri_engagement_behaviors/ \
    --path ros2_ws/src/open/emori_skill_facade/ \
    --path ros2_ws/src/open/README.md \
    --path ros2_ws/src/open/CONTRIBUTING.md \
    --path-rename ros2_ws/src/open/:
```

The selected packages land at the top level (`--path X/ --path-rename X/:` is
filter-repo's own spelling of `--subdirectory-filter X`, extended to several paths).
`git filter-repo` is a separate install. Before pushing an export, edit its `README.md`
and `CONTRIBUTING.md`: both describe all six packages and name paths in this repository
(`tests/`, `deploy/`, `LICENSING.md`) that the export does not carry. Packages that vendor
third-party code (`reachy_mini_ros2`) must keep their `THIRD_PARTY_NOTICES.md`.

## Tag and release

From a clean checkout of the source repository:

```bash
# Changelogs: the 0.1.0 entries are already written by hand; this is for later releases.
catkin_generate_changelog --all
catkin_prepare_release --version 0.1.0          # bumps package.xml, tags 0.1.0, pushes

# One bloom-release per package, against the humble distro.
bloom-release --rosdistro humble --track humble emori_msgs
bloom-release --rosdistro humble --track humble hri_context_analyzer
bloom-release --rosdistro humble --track humble hri_engagement_behaviors
bloom-release --rosdistro humble --track humble emori_skill_facade
```

`reachy_mini_msgs` and `reachy_mini_ros2` go out the same way on a `jazzy` track
(`bloom-release --rosdistro jazzy --track jazzy <pkg>`).

`bloom-release` asks for the release repository (create it as
`<org>/<pkg>-release`), opens a pull request against
[`ros/rosdistro`](https://github.com/ros/rosdistro) adding the packages to
`<distro>/distribution.yaml`, and the ROS build farm takes it from there.

Order matters: `emori_msgs` must be released and indexed **before** the packages that
depend on it, or their builds fail on the farm with an unresolvable `emori_msgs`
dependency. The same holds for `reachy_mini_msgs` before `reachy_mini_ros2`.

## What the index checks

* every `package.xml` is format 3 with a licence, a maintainer with a real address, and a
  description that is not the template;
* every dependency resolves through `rosdep` — which is why `pyhri` is **not** declared
  anywhere: apt has no `ros-humble-pyhri`, so declaring it would make `rosdep install`
  fail for everyone building `hri_engagement_behaviors`. The optional demo node imports
  it lazily and is the only thing that wants it;
* `CHANGELOG.rst` exists and its top entry matches the version in `package.xml`.

## After the index PR merges

`rosdep` learns the packages within a day, and

    sudo apt-get install ros-humble-hri-context-analyzer

becomes the way to install a package without cloning anything. Until then the supported
route for an outside consumer is the standalone image, or `colcon build
--packages-select` over a copy of the package directories — both documented in each
package's `README.md` and in `ros2_ws/src/open/README.md`.
