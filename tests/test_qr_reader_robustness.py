"""The badge reader must work in a real (dim, slightly defocused) room.

Two halves, both verifiable OFF the robot:

* the kiosk decoder (``hostapp/pages.py``) — the new pass ladder is exercised FOR
  REAL against the vendored ``static/jsqr.js`` under node, on synthetic
  under-exposed + defocused frames built from the committed badge matrix;
* the camera bridge (``deploy/robot/camera-bridge.sh`` + ``.service``) — the
  GStreamer pipeline string is asserted by running the script's dry-run under sh.

Anything that needs the physical IMX708 (real decode rate, exposure, focus) is checked
on the robot, never a number invented here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QR_MATRIX = Path(__file__).parent / "fixtures" / "badge-anna-001.qr.txt"
BRIDGE_SH = ROOT / "deploy" / "robot" / "camera-bridge.sh"
BRIDGE_UNIT = ROOT / "deploy" / "robot" / "camera-bridge.service"
JSQR = ROOT / "src" / "emori_adapter" / "hostapp" / "static" / "jsqr.js"
W, H = 1280, 720

def _find_bash() -> str | None:
    """A bash that can actually RUN a script, not merely one that is named ``bash``.

    ``shutil.which("bash")`` on a Windows dev box resolves to the WSL relay stub in
    ``system32``, which dies with ``execvpe(/bin/bash) failed`` when no distro is
    installed - so a name lookup alone would turn "no POSIX shell here" into three
    permanent failures instead of three skips. Probe each candidate.
    """
    seen, cands = set(), []
    for c in (shutil.which("bash"),
              r"C:\Program Files\Git\bin\bash.exe",
              r"C:\Program Files\Git\usr\bin\bash.exe"):
        if c and c not in seen and Path(c).exists():
            seen.add(c)
            cands.append(c)
    for c in cands:
        try:
            if subprocess.run([c, "-c", "exit 0"], capture_output=True, timeout=30).returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return None


BASH = _find_bash()

node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
sh = pytest.mark.skipif(BASH is None, reason="no working POSIX shell")


# --------------------------------------------------------------------------- pages


def _page(monkeypatch, path: str) -> str:
    from starlette.testclient import TestClient

    from emori_adapter.hostapp import server
    from emori_adapter.robot.client import DryRunRobotClient

    monkeypatch.setattr(server, "DEMO", True)
    app = server._build_app(
        overrides={
            "robot_client": DryRunRobotClient(),
            "fiware_factory": None,
            "idle": False,
            "watchdog": False,
            "gesture": False,
            "tts": None,
            "stt": None,
        }
    )
    with TestClient(app) as client:
        return client.get(path).text


def test_kiosk_drops_the_single_dontinvert_pass(monkeypatch):
    page = _page(monkeypatch, "/")
    assert "dontInvert" not in page          # jsQR's own default is attemptBoth
    assert "attemptBoth" in page
    assert "qrLadder(" in page


def test_kiosk_asks_for_the_bridge_native_resolution(monkeypatch):
    page = _page(monkeypatch, "/")
    # Chromium defaults getUserMedia to 640x480; the bridge publishes 1280x720.
    assert "width:{ideal:1280}" in page
    assert "height:{ideal:720}" in page


def test_kiosk_shows_the_badge_box_and_the_light_reading(monkeypatch):
    page = _page(monkeypatch, "/")
    assert 'id=qrbox' in page or 'id="qrbox"' in page
    assert 'id=qrlight' in page or 'id="qrlight"' in page


def test_qr_affordance_strings_exist_in_both_languages():
    from emori_adapter.hostapp.pages import UI_STRINGS

    for lang in ("en", "ca"):
        for key in ("hold_badge", "too_dark", "light_label"):
            assert key in UI_STRINGS[lang], f"{lang}/{key}"
    assert set(UI_STRINGS["ca"]) == set(UI_STRINGS["en"])


def test_identify_page_uses_the_same_scanner(monkeypatch):
    page = _page(monkeypatch, "/identify")
    assert "qrLadder(" in page
    assert "dontInvert" not in page


# ------------------------------------------------------------------ real decoding


def _matrix() -> list[str]:
    return QR_MATRIX.read_text(encoding="utf-8").split()


def _frame(module_px: int, blur_r: int, gain: float, black: int = 4, noise: int = 2,
           offset: tuple[int, int] = (0, 0)) -> bytes:
    """A deterministic RGBA frame: badge, defocused (box blur), then under-exposed.

    ``offset`` shifts the badge off centre, in pixels. A badge held up to the robot is
    routinely NOT in the middle of the frame -- a presented code measured on a robot sat
    at y 0..336 while the centre crop only covers y 162..558 -- so "does the ladder
    still see it" has to be testable.
    """
    rows = _matrix()
    n, quiet = len(rows), 4
    side = (n + 2 * quiet) * module_px
    pad = blur_r + 1
    bw = side + 2 * pad
    patch = bytearray([128]) * (bw * bw)          # mid-grey wall behind the badge
    for r in range(side):
        my = r // module_px - quiet
        for cx in range(side):
            mx = cx // module_px - quiet
            on = 0 <= my < n and 0 <= mx < n and rows[my][mx] == "#"
            patch[(r + pad) * bw + cx + pad] = 0 if on else 255
    for _ in range(2 if blur_r else 0):           # 2x separable box blur == defocus
        for axis in (0, 1):
            src = bytes(patch)
            for a in range(bw):
                line = (src[a * bw:(a + 1) * bw] if axis == 0
                        else bytes(src[b * bw + a] for b in range(bw)))
                acc = sum(line[:blur_r])
                cnt = blur_r
                out = bytearray(bw)
                for i in range(bw):
                    if i + blur_r < bw:
                        acc += line[i + blur_r]
                        cnt += 1
                    if i - blur_r - 1 >= 0:
                        acc -= line[i - blur_r - 1]
                        cnt -= 1
                    out[i] = acc // cnt
                if axis == 0:
                    patch[a * bw:(a + 1) * bw] = out
                else:
                    for b in range(bw):
                        patch[b * bw + a] = out[b]
    seed = 12345

    def rnd() -> int:                              # tiny LCG: identical everywhere
        nonlocal seed
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        return (seed >> 16) % (2 * noise + 1) - noise if noise else 0

    def enc(v: int) -> int:
        return max(0, min(255, int(v * gain + black) + rnd()))

    bg = enc(128)
    buf = bytearray(bytes((bg, bg, bg, 255)) * (W * H))
    x0 = min(max(0, (W - bw) // 2 + offset[0]), W - bw)
    y0 = min(max(0, (H - bw) // 2 + offset[1]), H - bw)
    for r in range(bw):
        o = ((y0 + r) * W + x0) * 4
        for cx in range(bw):
            v = enc(patch[r * bw + cx])
            buf[o] = buf[o + 1] = buf[o + 2] = v
            o += 4
    return bytes(buf)


_HARNESS = """
globalThis.jsQR = require(process.argv[2]);
const mod = require(process.argv[3]);
const data = new Uint8ClampedArray(require('fs').readFileSync(process.argv[4]));
const w = +process.argv[5], h = +process.argv[6], out = [];
for (let p = 0; p < 4; p++) {
  const r = mod.qrLadder(data, w, h, p);
  out.push({pass: r.pass, data: r.data, ms: Math.round(r.ms), range: r.range});
}
const t = jsQR(data, w, h, {inversionAttempts: 'dontInvert'});
out.push({pass: 'today', data: t ? t.data : '', ms: 0, range: 0});
console.log(JSON.stringify(out));
"""


def _ladder(tmp_path: Path, frame: bytes) -> list[dict]:
    from emori_adapter.hostapp import pages

    mod = tmp_path / "qrscan.js"
    mod.write_text(pages._QR_SCAN_JS + "\nmodule.exports={qrLadder};\n", encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    raw = tmp_path / "frame.rgba"
    raw.write_bytes(frame)
    run = subprocess.run(
        [shutil.which("node"), harness.as_posix(), JSQR.as_posix(), mod.as_posix(),
         raw.as_posix(), str(W), str(H)],
        capture_output=True, text=True, timeout=300,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


@node
def test_ladder_decodes_a_dim_defocused_badge_that_todays_pass_misses(tmp_path):
    passes = _ladder(tmp_path, _frame(module_px=10, blur_r=3, gain=0.05))
    today = [p for p in passes if p["pass"] == "today"][0]
    assert today["data"] == "", "fixture no longer reproduces the deployed failure"
    decoded = [p["pass"] for p in passes if p["data"] == "EMORI:anna-001"]
    assert decoded, f"ladder failed on a dim defocused badge: {passes}"


@node
def test_ladder_decodes_a_small_dim_badge(tmp_path):
    passes = _ladder(tmp_path, _frame(module_px=6, blur_r=2, gain=0.10))
    assert [p for p in passes if p["pass"] == "today"][0]["data"] == ""
    assert [p["pass"] for p in passes if p["data"] == "EMORI:anna-001"]


@node
def test_good_light_spends_its_ticks_on_the_cheap_crop_pass(tmp_path):
    """In good light the ladder must scan, not grind through full-frame decodes.

    Measured on a robot from the scan telemetry: one FULL-frame jsQR call costs ~1.5 s on
    the CM4, and the loop manages 2 ticks/s total. The enhancement passes are rightly
    skipped when the crop is well lit -- but if they fell through to 'full', a lit frame
    would run crop,full,full,full: three ~1.5 s decodes per cheap one, leaving the centre
    crop (the pass that catches a badge held up to the camera) a turn only every ~2 s, and
    a badge would have to be held still and lucky. Keep ONE full pass per cycle
    for a badge outside the crop, and give the rest to the pass that pays.
    """
    passes = _ladder(tmp_path, _frame(module_px=10, blur_r=1, gain=1.0, black=0))
    assert passes[0]["pass"] == "crop"
    assert passes[0]["data"] == "EMORI:anna-001"
    names = [p["pass"] for p in passes[:4]]
    # No full-RESOLUTION whole-frame decode: that is the ~1.5 s pass that starved the loop.
    assert "full" not in names, f"lit frame still grinds a full-res decode: {names}"
    # Both coverage gaps get a look every cycle: full-res centre, and the whole field of
    # view at half res for a badge held outside the crop.
    assert names.count("crop") >= 2, f"cheap centre pass is starved: {names}"
    assert names.count("full/2") >= 2, f"off-centre badges are barely looked at: {names}"


@node
def test_a_badge_outside_the_centre_crop_still_decodes(tmp_path):
    """Held-up badges are not centred, and the crop only covers the middle 55%.

    On the robot the presented code sat entirely above the crop, so only the whole-frame
    pass could ever have seen it -- and that pass cost ~1.5 s, i.e. one look every ~2 s.
    Whole-frame coverage has to be cheap enough to run every cycle, which is what the
    half-resolution pass buys: a quarter of the pixels, the same field of view.
    """
    frame = _frame(module_px=10, blur_r=1, gain=1.0, black=0, offset=(-330, -230))
    passes = _ladder(tmp_path, frame)
    decoded = [p["pass"] for p in passes if p["data"] == "EMORI:anna-001"]
    assert decoded, f"off-centre badge invisible to every pass: {passes}"


@node
def test_dim_light_still_runs_the_enhancement_ladder(tmp_path):
    # The crop/stretch passes are what rescue a dim or defocused badge; favouring 'crop'
    # in good light must not cost us them when the frame actually needs repair.
    passes = _ladder(tmp_path, _frame(module_px=10, blur_r=3, gain=0.05))
    names = [p["pass"] for p in passes[:4]]
    assert "crop/2+levels" in names and "crop+stretch" in names, names


# ------------------------------------------------------------------ camera bridge


def test_bridge_unit_delegates_to_the_script():
    unit = BRIDGE_UNIT.read_text(encoding="utf-8")
    assert "ExecStart=/opt/emori/deploy/robot/camera-bridge.sh" in unit
    assert "EnvironmentFile=-/etc/default/emori-camera" in unit
    assert "framerate=30/1" not in unit          # the frozen pipeline is gone


def test_bridge_probes_gst_inspect_without_a_sigpipe_race():
    """Structural, because the failure does not reproduce off the robot.

    `cmd | grep -q` under `set -o pipefail` reports a property MISSING when it is present:
    grep exits at the first match, the ~100 KiB producer dies on SIGPIPE, pipefail
    promotes that to failure. On the robot this silently disabled autofocus. It cannot be
    caught by a fixture here -- Windows/git-bash does not deliver SIGPIPE the same way, so
    a padded fake listing still passes -- so the safe idiom is pinned directly instead.
    """
    script = BRIDGE_SH.read_text(encoding="utf-8")
    # Comments describe the trap at length; only executable lines are the claim.
    code = "\n".join(ln for ln in script.splitlines() if not ln.lstrip().startswith("#"))
    assert "| grep -q" not in code, "SIGPIPE-prone probe: pipefail will drop live props"
    assert 'PROPS="$(' in code, "probe gst-inspect once into a variable"
    assert "<<EOF" in code, "match via heredoc, which is not a pipeline"


def test_bridge_controls_are_overridable_and_release_checked():
    script = BRIDGE_SH.read_text(encoding="utf-8")
    # libcamerasrc property NAMES differ per libcamera release, so whatever the default
    # is, it stays gst-inspect-checked and overridable from /etc/default/emori-camera.
    assert "EMORI_CAM_SRC_CONTROLS:-" in script
    assert "gst-inspect-1.0" in script


def _fake_inspect(tmp_path, *props: str, bulk: int = 4000):
    """A gst-inspect-1.0 stand-in reporting exactly `props` (the robot's real AF names).

    `bulk` pads the listing past the 64 KiB pipe buffer, as the real gst-inspect does.
    That padding is the test, not decoration: with a SMALL listing the producer finishes
    writing before `grep -q` exits, so a `cmd | grep -q` probe under `set -o pipefail`
    passes -- and on the robot, where the listing is far larger, the same probe reports
    every property MISSING because the producer dies on SIGPIPE and pipefail promotes it
    to failure. A short fixture here silently blesses a bridge that turns focus off.
    """
    fake = tmp_path / "inspect.sh"
    body = "".join(f"  {p:<20}: {p}\n" for p in props)
    body += "".join(f"  filler-{i:<13}: padding\n" for i in range(bulk))
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}EOF\n", encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    return fake


@sh
def test_bridge_focuses_on_the_badge_by_default(tmp_path):
    """The IMX708 is an autofocus module and libcamera defaults AfMode to MANUAL.

    `gst-inspect-1.0 libcamerasrc` reports `Enum "AfMode" Default: 0, "manual"` -- "it
    will never perform any action nor move the lens of its own accord". A pipeline that
    passes no focus control leaves the lens at one fixed position, and a badge is only
    readable at whatever distance happens to be sharp, so scanning means hunting for an
    angle. Continuous AF over the FULL range (which includes the closest
    macro positions, unlike `normal`) is what makes a badge held at arm's length decode.
    """
    inspect = _fake_inspect(tmp_path, "af-mode", "af-range", "af-speed", "lens-position",
                            "ae-constraint-mode")
    pipeline, _ = _pipeline(EMORI_CAM_GST_INSPECT=inspect.as_posix())
    assert "af-mode=2" in pipeline           # continuous: rescans on its own
    assert "af-range=2" in pipeline          # full: includes the closest macro positions
    assert "af-speed=1" in pipeline          # fast: a kiosk cannot wait out a slow sweep
    # Highlight-priority AE: a badge is the brightest thing in frame and plain AE blows it
    # out in a dim room (black modules at luma 66, undecodable by any library).
    assert "ae-constraint-mode=1" in pipeline
    assert pipeline.startswith("libcamerasrc a")


@sh
def test_bridge_survives_a_libcamera_without_autofocus(tmp_path):
    # The AF default must never brick the camera on a build that lacks those names: an
    # unknown libcamerasrc property makes gst-launch exit 1, which systemd restart-loops.
    inspect = _fake_inspect(tmp_path, "brightness")
    pipeline, err = _pipeline(EMORI_CAM_GST_INSPECT=inspect.as_posix())
    assert "af-mode" not in pipeline
    assert pipeline.startswith("libcamerasrc ! video/x-raw")   # still a valid pipeline
    assert "no property 'af-mode'" in err


def _pipeline(**env) -> tuple[str, str]:
    import os

    e = dict(os.environ)
    e["EMORI_CAM_PRINT_PIPELINE"] = "1"
    e.update(env)
    run = subprocess.run(
        [BASH, BRIDGE_SH.as_posix()],
        env=e, capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip(), run.stderr


@sh
def test_bridge_defaults_buy_exposure_time_and_boost_contrast():
    pipeline, _ = _pipeline()
    assert "framerate=15/1" in pipeline          # 15 fps doubles the exposure ceiling
    assert "videobalance brightness=" in pipeline
    assert "contrast=" in pipeline
    assert "v4l2sink device=/dev/video40" in pipeline
    assert "width=1280,height=720" in pipeline


@sh
def test_bridge_balance_stage_can_be_removed():
    pipeline, _ = _pipeline(EMORI_CAM_BALANCE="0")
    assert "videobalance" not in pipeline
    assert "libcamerasrc ! video/x-raw" in pipeline


@sh
def test_bridge_drops_libcamerasrc_properties_this_build_lacks(tmp_path):
    fake = tmp_path / "inspect.sh"
    fake.write_text(
        "#!/bin/sh\ncat <<'EOF'\n  auto-focus-mode     : AF mode\n  lens-position       : lens\nEOF\n",
        encoding="utf-8", newline="\n",
    )
    fake.chmod(0o755)
    pipeline, err = _pipeline(
        EMORI_CAM_SRC_CONTROLS="auto-focus-mode=2 nope-prop=7",
        EMORI_CAM_GST_INSPECT=fake.as_posix(),
    )
    assert "libcamerasrc auto-focus-mode=2 !" in pipeline
    assert "nope-prop" not in pipeline
    assert "no property 'nope-prop'" in err
