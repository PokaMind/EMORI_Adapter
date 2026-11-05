# Contributing to the standalone EMORI ROS 2 packages

Contributions go through issues and pull requests on this repository. The root
[`CONTRIBUTING.md`](../../../CONTRIBUTING.md) covers setup, tests and the general rules;
the rules below apply to the packages in this directory on top of those.

1. **Never import `emori_adapter`**, not in a node, `setup.py` or launch file. Anything
   shared with the core is mirrored here as a small stdlib-only module (see
   `hri_context_analyzer/contracts.py`). `tests/test_ros_standalone.py` enforces it.
2. **Never depend on a package under `ros2_ws/src/emori/`.** The dependency may point the
   other way (`emori_skill_native` plugs into `emori_skill_facade` by entry point).
   `tests/test_ros_publishing.py` fails on a `*depend` naming one in any `package.xml` here.
3. **Messages are append-only**: add fields at the end; never reorder, rename or retype.
4. **No customer data, hostnames or credentials** in code, comments, READMEs or changelogs.
5. **Vendored code keeps its licence and copyright**: say "Vendored from <project>
   <version>" in the file's docstring, mark your changes, and keep the package's
   `THIRD_PARTY_NOTICES.md` and `LICENSING.md` ("Third-party code") current.

Before sending a change, from the repository root: `python -m pytest tests` and
`python -m ruff check src tests`, plus `python scripts/ros_standalone_check.py` if you
touched anything the standalone image builds. Update the package's `CHANGELOG.rst` for
anything a user of the package would notice. Contributions are licensed under Apache-2.0.
