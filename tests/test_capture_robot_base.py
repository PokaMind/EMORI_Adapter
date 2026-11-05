"""capture_robot_base: export-and-import only, never save, arch asserted."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "capture_robot_base",
    Path(__file__).resolve().parents[1] / "scripts" / "capture_robot_base.py",
)
cap = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cap)

IMAGE = "sha256:4d4b21d31b23" + "a" * 52


class FakeRunner:
    def __init__(self, arch="arm64", export_bytes=b"tar", fail_on=None, image=IMAGE):
        self.calls: list[list[str]] = []
        self.arch, self.export_bytes, self.fail_on, self.image = (
            arch, export_bytes, fail_on, image)

    def __call__(self, argv, stdout_to):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if self.fail_on and self.fail_on in joined:
            return 1, ""
        if "{{.Image}}" in joined:
            return 0, self.image + "\n"
        if "docker export" in joined:
            stdout_to.write_bytes(self.export_bytes)
            return 0, ""
        if "image inspect" in joined:
            return 0, self.arch + "\n"
        return 0, ""


def test_capture_exports_imports_and_tags_by_image_id(tmp_path):
    run = FakeRunner()
    tag = cap.capture(run, "pollen@robot", tmp_path)

    assert tag == "emori/robot-buildenv:4d4b21d31b23"
    export = next(c for c in run.calls if "docker export" in " ".join(c))
    assert export[:2] == ["ssh", "pollen@robot"]
    imp = next(c for c in run.calls if c[:2] == ["docker", "import"])
    assert imp[2:4] == ["--platform", "linux/arm64"]
    assert imp[-1] == tag
    assert (tmp_path / "base-4d4b21d31b23.tar").read_bytes() == b"tar"


def test_wrong_architecture_fails_and_keeps_the_tar(tmp_path):
    with pytest.raises(cap.CaptureError, match="amd64"):
        cap.capture(FakeRunner(arch="amd64"), "r", tmp_path)
    assert (tmp_path / "base-4d4b21d31b23.tar").exists()


def test_empty_export_fails_before_import(tmp_path):
    run = FakeRunner(export_bytes=b"")
    with pytest.raises(cap.CaptureError, match="wrote nothing"):
        cap.capture(run, "r", tmp_path)
    assert not any(c[:2] == ["docker", "import"] for c in run.calls)


def test_failed_export_stops(tmp_path):
    with pytest.raises(cap.CaptureError, match="docker export"):
        cap.capture(FakeRunner(fail_on="docker export"), "r", tmp_path)


def test_bad_image_id_is_refused(tmp_path):
    with pytest.raises(cap.CaptureError, match="unexpected image id"):
        cap.capture(FakeRunner(image="oops"), "r", tmp_path)


@pytest.mark.parametrize("args", [("save", "x"), ("image", "save", "x"),
                                  ("--debug", "save", "x")])
def test_docker_save_is_refused_by_construction(args):
    with pytest.raises(cap.CaptureError, match="save"):
        cap.docker_argv(*args)


def test_no_save_command_is_ever_issued(tmp_path):
    run = FakeRunner()
    cap.capture(run, "r", tmp_path)
    assert not any("save" in c for c in run.calls)


def test_only_docker_runs_on_the_robot():
    with pytest.raises(cap.CaptureError):
        cap.ssh_argv("r", ["rm", "-rf", "/"])


def test_main_reports_failure_and_success(tmp_path, capsys):
    assert cap.main(["--ssh", "r", "--out-dir", str(tmp_path)], run=FakeRunner()) == 0
    assert "imported emori/robot-buildenv:4d4b21d31b23" in capsys.readouterr().out
    assert cap.main(["--ssh", "r", "--out-dir", str(tmp_path)],
                    run=FakeRunner(arch="amd64")) == 1


def test_remove_tar_after_success(tmp_path):
    cap.main(["--ssh", "r", "--out-dir", str(tmp_path), "--remove-tar"], run=FakeRunner())
    assert not list(tmp_path.glob("base-*.tar"))
