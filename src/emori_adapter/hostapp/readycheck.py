"""One-command readiness self-check for the EMORI robot appliance.

    emori-readycheck            # PASS/FAIL/SKIP/WARN matrix, exit 1 if any gate FAILs
    emori-readycheck --strict   # additionally exit 2 if any gate SKIPped
    emori-readycheck --json     # the same results, machine-readable

This replaces the hand-run curls that would otherwise decide whether the robot is
ready for a session (the checks in ``deploy/robot/install.sh`` and
``deploy/robot/robot-mode.sh``, plus the checks an installed add-on contributes).

Design contract, in order of importance:

* **A gate never fabricates a PASS.** A missing key, an unparsable body, or an
  absent dependency is SKIP-with-a-reason or FAIL - never PASS.
* **Gates are pure.** All I/O happens in :func:`collect` through an injectable
  :class:`SystemProbe`; :func:`evaluate` is a pure function of :class:`Evidence`,
  so every branch is unit-testable on a dev box with none of the hardware present.
* **Every FAIL carries one line of remediation** naming the responsible script or
  setting.  :class:`GateResult` refuses to be constructed without it.
* **Read-only**, with exactly one named exception: the ``session_chain`` gate performs
  a real partner SSO login, where Pokamind's SSO endpoint creates or updates a user
  record for the identity it is given.  That write is the price of proving the whole
  session chain instead of guessing at it, and it is contained to one identity that
  is never a person's: the robot's own drop-in learner ``robot-<robot_id>@emori.local``
  - the only account Pokamind lets a robot log in as whatever its scope, and the one
  every anonymous session already writes the same partner stamp to.  Those logins are
  signed with the robot's token, and one ``GET /api/robot/whoami`` asks what that
  robot may start: a read, authenticated exactly like every control-plane call
  (Pokamind records the assertion's nonce).  A robot without a token signs nothing
  and asks nothing (``pokamind_token`` FAILs ``pokamind_token_missing``).
  Nothing here starts, restarts, enables, or configures anything.
* **The robot SAYS what broke.**  Typical failures (frontend down, a replaced token,
  backend unreachable, wrong socket URL) are silent: the robot simply goes quiet.
  A FAILing run therefore speaks one short line
  through the robot's own speaker before it exits (``--no-speak`` opts out).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, replace
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, quote, urlsplit

if __name__ == "__main__":
    # Run with ``python -m``: an add-on importing this module by name gets this copy.
    sys.modules.setdefault("emori_adapter.hostapp.readycheck", sys.modules[__name__])

# Load .env before any environment read, exactly as server.py does, so the
# self-check sees the same deployment configuration the hostapp booted with. The import
# also runs the robot-local settings layer (site_settings.apply) on THIS process: a value
# inherited from the process that started readycheck (emori-admin's Health tab) and
# marked as derived is stripped and derived again, so every reader below -- and the
# site_config gate's KEY=source pairs -- see what the hostapp's next start would see.
import emori_adapter.config  # noqa: F401
from emori_adapter import site_settings as _site_settings

# The kiosk's OWN minting path, imported rather than re-implemented: a probe token
# signed by a second copy of the claim set could pass while every real session dies
# (and vice versa).  server.py imports exactly these two.
from emori_adapter.hostapp.jwt_sso import build_sso_url, mint_robot_sso_jwt
# THIS robot's own Pokamind credential and the one whoami client.
# Both are importable without the hostapp extras: parsing and the HTTP call are
# stdlib/httpx, and only signing reaches for cryptography (lazily, inside the call).
from emori_adapter import robot_token as _robot_token
from emori_adapter.hostapp import extensions as _extensions
from emori_adapter.hostapp.pokamind_whoami import WhoamiResult, fetch_whoami

# Same grammar the hostapp already enforces on its status codes
# (server.py ``_SAFE_STATUS_CODE_RE``); gate names and codes share it so a
# matrix line is always safe to log, paste, or forward.
SAFE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")

PASS = "pass"
FAIL = "fail"
SKIP = "skip"
# A proven inconsistency that does NOT stop a session from running, and so must
# not become a FAIL: a FAIL blocks go-live, speaks "I am not ready" at whoever is
# standing there, and exits non-zero, which is how an advisory finding ends up
# either ignored or suppressed.  SKIP would be a lie in the other direction -
# nothing here was unprovable.  WARN is reported, hinted, and costs no exit code.
WARN = "warn"
_STATUSES = (PASS, FAIL, SKIP, WARN)
_LABELS = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP", WARN: "WARN"}

# v4l2-ctl --list-formats-ext prints one "[n]: 'FOURCC'" line per advertised
# capture format.  An existing-but-unfed v4l2loopback still prints the
# "Type: Video Capture" header, so the header alone must NOT count as ready.
_CAPTURE_FORMAT_RE = re.compile(r"\[\d+\]:\s*'\w{4}'")

_DEFAULT_VIDEO_DEVICE = "/dev/video40"  # install.sh:53-58 (video10 is a bcm2835 codec node)
_TTS_PROBE_TEXT = "readycheck"

# The phone superadmin service (deploy/robot/emori-admin.service).
_DEFAULT_ADMIN_PORT = "8070"
# A typo'd EMORI_ADMIN_PORT must produce a WARN about the service, not a malformed URL
# that reads like one: anything that is not a plain port number falls back to the default.
_PORT_RE = re.compile(r"[0-9]{1,5}\Z")
_ADMIN_SERVICE_NAME = "emori-admin"  # /healthz `service` field
# emori-admin's own answer to "can you read Wi-Fi status and the speaker level, through
# the backend the phone would use right now?" (admin/backend_check.py). Gated on the
# shared EMORI_ADMIN_TOKEN, in the header the hostapp already uses towards emori-admin.
_ADMIN_DEEP_PATH = "/healthz/deep"
_ADMIN_TOKEN_HEADER = "X-EMORI-Admin-Token"
# Two backend calls in parallel, each bounded at 5 s in emori-admin, plus its :8000
# status probe (3 s): more than SystemProbe's 4 s default, so a slow daemon reads as
# "slow", not as "unreachable".
_ADMIN_DEEP_TIMEOUT_S = 15.0
# The answers that mean "this robot cannot do it at all" rather than "not right now":
# the route does not exist, or the tool the host backend runs is not installed. Same
# set as admin/backend_check.BACKEND_MISSING (a test holds the two together).
_ADMIN_BACKEND_MISSING = frozenset({"route_missing", "nmcli_missing", "alsa_missing"})
# sudo will not run the nmcli commands that change the Wi-Fi on a v2 robot: joins, the
# hotspot and the boot hotspot fallback all fail (admin/netman.PRIVILEGES_MISSING).
_ADMIN_PRIVILEGES_MISSING = frozenset({"sudo_denied", "sudo_missing"})
# What the code on disk is (deploy/robot/one-click-deploy.sh writes it before it restarts
# anything), in the install root. emori-admin reports the copy it read at start.
_DEPLOYED_MARKER = "DEPLOYED_COMMIT.txt"
_VOICE_TAP_BLOCKS_BELOW_MS = 5.0
# The v2 fence (deploy/robot/ros2_native/): while it exists Pollen's daemon cannot start,
# so :8000 belongs to the robot node even when nothing answers there -- and the v2
# graph is the only thing on this robot that can move it (gate_ros2 checks it).
_V2_FENCE_FLAG = "/opt/emori/V2_OWNS_THE_BUS"
# The hub flag (deploy/hub/emori-hub.service): while it exists the hub container, not the
# host application, runs the kiosk's gateway, and emori-hostapp.service is a skipped start.
_HUB_FLAG = "/opt/emori/HUB_IS_ROS"
_HUB_UNIT = "emori-hub"
_HOSTAPP_UNIT = "emori-hostapp"
_HUB_CONTAINER = "emori-hub"
# The directory the hub container mounts for the models' caches and the add-on's files; the
# container runs as uid 1000, so a test of it from inside is a test of that user.
_HUB_CACHE_DIR = "/home/pollen/.cache/emori"
# The hub's own ROS node (namespace /emori), the proof that the container reached the
# graph the motor container serves.
_HUB_NODE = "robot_link"
# What `/healthz` and the robot client call each transport.
_RUNTIME_ROS = "ros"
_RUNTIME_HTTP = "http"
_SINK_NODE_LINE = 'node.name = "emori_voice"'
# The v2 graph without Docker (deploy/robot/ros2_native/, Route B): install-native.sh
# installs this unit, and a fenced robot that has it runs the robot node natively.
_V2_NATIVE_UNIT = "/etc/systemd/system/reachy-mini-ros2.service"
# Installed by the first switch to v2 and kept by the way back, so it marks a robot that has been
# on v2 and may have the motor container left over.
_V2_FENCE_DROPIN = "/etc/systemd/system/reachy-mini-daemon.service.d/zz-v2-fence.conf"
# The camera bridge's own settings (camera-bridge.service EnvironmentFile=). Its
# EMORI_CAM_ROS_VIDEO_NR is the face look-at tee, so the gaze gate reads the device
# number from here unless EMORI_READYCHECK_ROS_VIDEO_DEVICE names one.
_CAMERA_DEFAULTS = "/etc/default/emori-camera"
_CAMERA_DEFAULTS_MAX_BYTES = 65536
_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")

# The containerised ROS 2 (ros4hri) graph, when an operator opted into it:
# deploy/ros2/docker-compose.robot.yml `container_name`, started by
# deploy/robot/emori-ros2.service (install.sh step 11).
_ROS2_CONTAINER = "emori-ros2"
# v2 (docs/ros2-v2-native-robot.md) runs a DIFFERENT container, with the robot itself
# inside it: no Pollen daemon, the node owns /dev/ttyAMA3 and answers the daemon's own
# dialect on :8000. A robot can be running either, never both, and this module has to be
# able to tell -- a v2 robot checked as v1 reports a container that does not exist and a
# perfectly healthy robot fails its own readiness gate.
_ROS2_V2_CONTAINER = "emori-ros2-v2"
# The node the v2 container adds, and the only one that can move the robot.
_ROS2_V2_NODE = "reachy_mini"
# What GET /api/daemon/status answers when the answer comes from the node's shim
# rather than from Pollen's daemon. Pollen has no `backend` field at all, which is what
# makes "which one is really on :8000" answerable.
_SHIM_BACKEND = "reachy_mini_ros2"
# The v2 facade reaches the robot node over ROS actions, not over the hostapp intake.
_ROS_NATIVE_TRANSPORT = "ros_native"
# The nodes `deploy/ros2/docker-compose.robot.yml`'s CMD (slice2.launch.py) starts
# unconditionally, by their launch `name=`.  `fiware_bridge` is NOT here: it is
# condition-gated on `with_fiware`, and the robot compose sets it false on purpose.
# `ros2 launch` keeps the survivors running when one node dies at import, so the
# container stays healthy while a piece of the graph publishes nothing at all.
_ROS2_NODES = ("emori_adapter", "hri_context_analyzer", "hri_engagement_behaviors",
               "skill_facade")
# One `bash -lc` because `ros2` needs both overlays sourced; `node list` and not
# `topic list` because it is cheaper and needs no `person_id` (which lives in the
# launch file, and this gate must not become a second source of truth for it).
# The distro is part of the flavour: v1 is Humble, v2 is Jazzy (reachy-mini 1.10.0
# needs Python >= 3.11). Globbing instead of naming one keeps this a single command
# for both, and a third distro later.
_ROS2_NODE_LIST = (
    "for setup in /opt/ros/*/setup.bash; do source \"$setup\"; done "
    "&& source /emori/ros2_ws/install/setup.bash "
    # --spin-time, because the default is ~1 s of DDS discovery and this graph is five
    # nodes on a CM4 that is also running a 50 Hz control loop: with the default,
    # back-to-back `ros2 node list` calls can return 3 nodes and then 5, failing a
    # healthy graph for "ros2_graph_incomplete". The probe already budgets 20 s.
    # --no-daemon, because without it `ros2 node list` starts a resident ros2 daemon in
    # the container that nothing here ever stops. The direct node finds the same graph,
    # and --spin-time applies to it.
    "&& ros2 node list --no-daemon --spin-time 5"
)
# Which lane may submit motion.  Mirrors hostapp/skill_api.py MOTION_OWNERS, including
# its rule that anything unrecognised reads as the shipped default rather than as
# permission - a typo must never be the reason a second writer reaches the robot.
_MOTION_OWNERS = ("hostapp", "ros", "none")
_DEFAULT_MOTION_OWNER = "hostapp"

# --- session chain -----------------------------------------------------------------
# Where Pokamind's SSO 302 lands: <app origin>/auth/partner-callback/<partner>?<params>.
# A Location that is NOT this path means the server redirected somewhere the kiosk
# cannot start a session from - a misconfigured app origin looks identical to a healthy
# login until the robot is already standing in front of someone.
_CALLBACK_PATH_PREFIX = "/auth/partner-callback/"
# The flag the screenless kiosk cannot live without: without it the app never
# auto-advances its device-check page, and a robot with no screen waits at a
# "Continue" button nobody can click.  The server appends it only when kiosk mode is
# enabled for the partner AND the token carried emori_kiosk, so its absence is a real,
# common, and otherwise silent misconfiguration.
_KIOSK_FLAG = ("emoriKiosk", "1")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# The SSO leg writes (see the module docstring), so it logs in as the robot's own
# drop-in learner (:func:`_token_probe_identity`), never as a person.
# What the robot's own account is called when no EMORI_ROBOT_NAME is set
# (identity.DEFAULT_ROBOT_NAME; repeated, not imported, to keep this module light).
_DEFAULT_ROBOT_NAME = "EMORI"
_PROBE_TOKEN_TTL_S = 120  # long enough for the two hops, short enough to be useless if seen

# One short line, spoken through the robot's own speaker, per language:
# (a broken link to Pokamind, anything else).  Two messages and not one because
# "I cannot reach Pokamind" on a robot whose motors are disabled is a lie that sends the
# operator to the wrong machine.  Short enough to be heard before anyone walks away.
SPOKEN_LINES: dict[str, tuple[str, str]] = {
    "en": ("I cannot reach Pokamind.", "I am not ready."),
    "ca": ("No puc connectar amb Pokamind.", "No estic a punt."),
}
# The gates that prove the robot can start a session at all; a failure in any of them is
# a link to Pokamind, so they share the first line above.
_LINK_GATES = frozenset({"pokamind", "pokamind_whoami", "session_chain", "badge_session",
                         "voice_menu", "voice_menu_in_scope"})
# Pokamind's per-robot SSO refusal codes. The SSO probe records the
# body's ``error`` VALUE only when it is one of these fixed, content-free codes; any
# other body stays reduced to its key's name, because an unknown rejection body may
# quote the token and the e-mail it refused.
_SSO_SCOPE_CODES = frozenset({
    "robot_unknown", "robot_revoked", "invalid_assertion", "module_not_in_scope",
    "pathway_not_in_scope", "course_not_in_scope", "language_not_in_scope",
    "identity_not_in_scope",
})
# Where the provisioning steps for a robot's own token are written down.
_TOKEN_DOC = "docs/deploy-a-new-robot.md"
# Where the robot-local settings layer is explained.
_SETTINGS_DOC = "docs/configuration.md#where-each-setting-comes-from"
# The one line a robot without a usable token is told (pokamind_token, demo_off, ...).
_TOKEN_FIX = ("install this robot's Pokamind token: admin page -> Pokamind -> Replace "
              "token, or Deploy-to-Robot")
_PLAYBACK_TIMEOUT_S = 10.0  # pw-play blocks while the sink is busy; never forever

# The course a session is claimed in when nothing names one (identity.DEFAULT_COURSE_ID;
# repeated, not imported, to keep this module light - a test holds the two equal).
_DEFAULT_COURSE_ID = "emori"

# --- the spoken menu's tiers (server.menu_source) ------------------------------------
_MENU_TIERS = frozenset({"env", "site", "prod", "auto", "dev", "builtin"})
# What the menu builder records in a menu it built from the Pokamind record
# (menu_builder.GENERATED_FROM / AUTHORED_DRAFT / AUTHORED_MIXED; a test holds them
# equal). A draft names an option after its module's screen title, because the record
# carries no spoken name for it.
_MENU_GENERATED_FROM = "pokamind_whoami"
_MENU_DRAFT_AUTHORED = frozenset({"title_draft", "mixed"})
# Module ids no Pokamind record carries: the tracked development menus (``dev-``,
# ``example-``) and the built-in default menu (``demo-module-``). A served menu holding
# one offers a dead option.
_PLACEHOLDER_ID_PREFIXES = ("example-", "demo-module-", "dev-")

# --- the robot-local settings layer (gate_site_config) -------------------------------
# The hostapp's own view of its site settings (admin_api, loopback + EMORI_ADMIN_TOKEN):
# readycheck reads ONLY restart_pending and restart_reasons from it.
_HOSTAPP_SITE_PATH = "/api/admin/site"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# The restart reasons emori-admin applies by itself once the robot has been idle long
# enough (admin/site_apply.AUTO_APPLY_REASONS minus its own dashboard_password); the
# others are operator-entered and wait for the Settings tab's Apply now.
_AUTO_APPLIED_REASONS = frozenset({"scope_changed", "addon_provisioned"})
_KEY_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
# A partner name as hints may quote it (the record's, or EMORI_PARTNER_NAME's).
_PARTNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
# whoami's ignored optional keys, as pokamind_whoami names them ("app_origin",
# "modules[3].menu_name"): names only, printable in a detail line.
_IGNORED_KEY_RE = re.compile(r"[A-Za-z0-9_.\[\]]{1,64}\Z")


@dataclass(frozen=True)
class GateResult:
    """One gate outcome.  A FAIL without remediation is a construction error."""

    name: str
    status: str
    code: str
    hint: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUSES:
            raise ValueError("gate_status_invalid")
        if SAFE_CODE_RE.fullmatch(self.name) is None:
            raise ValueError("gate_name_invalid")
        if SAFE_CODE_RE.fullmatch(self.code) is None:
            raise ValueError("gate_code_invalid")
        if self.status in (FAIL, WARN) and not self.hint:
            raise ValueError("gate_hint_required")


@dataclass(frozen=True)
class HttpResult:
    reached: bool = False
    status: int = 0
    payload: dict | None = None
    content_type: str = ""
    # The response's ``Date`` header and this machine's clock just before the request
    # went out and just after the answer arrived: what the ``clock`` gate measures the
    # robot's clock against (the server wrote Date somewhere between the two).
    date: str = ""
    sent_at: float = 0.0
    received_at: float = 0.0
    # Whether that Date can be Pokamind's clock NOW: a cache's ``Age`` header (the answer
    # was stored, its Date is that much older) and whether a redirect was followed (the
    # Date is another server's).
    age: str = ""
    redirected: bool = False


@dataclass(frozen=True)
class SsoResult:
    """The partner SSO answer, redirect NOT followed - the redirect IS the evidence.

    ``error_field`` is the NAME of the error key a 4xx body carried, never its text: a
    rejection quotes the token it refused and the e-mail it was minted for, and this
    record ends up in matrix lines, JSON output, and the journal.
    """

    reached: bool = False
    status: int = 0
    location: str = ""
    error_field: str = ""
    # Pokamind's own per-robot refusal code (``_SSO_SCOPE_CODES``), or "".
    error_code: str = ""


@dataclass(frozen=True)
class ClockProbe:
    """This robot's clock against Pokamind's: what a signed robot call depends on.

    ``ntp`` is ``timedatectl``'s ``NTPSynchronized`` ("yes" / "no"; "" = not known).
    ``skew_s`` is robot minus Pokamind, positive = the robot is AHEAD, measured from the
    ``Date`` header of the pokamind reachability probe; ``uncertainty_s`` bounds it
    (Date has one-second resolution, plus half the round trip). ``None`` = not measured.
    """

    ntp: str = ""
    skew_s: float | None = None
    uncertainty_s: float = 0.0
    # Why Pokamind's answer could not be measured against at all ("" = it could, or there
    # was none): it came out of a cache (Age > 0) or from behind a redirect, so its Date
    # is not Pokamind's clock now. The measurement is skipped, never guessed.
    unmeasurable: str = ""


@dataclass(frozen=True)
class IdentityProbe:
    """Which physical robot this is (``emori_adapter.robot_identity``), none of it secret.

    ``serial``/``wlan_mac`` are read from the hardware now; ``recorded_*`` from
    ``ROBOT_IDENTITY``, which install.sh writes. ``""`` = not known.
    """

    serial: str = ""
    wlan_mac: str = ""
    recorded_serial: str = ""
    recorded_robot_id: str = ""


@dataclass(frozen=True)
class TokenProbe:
    """What the token resolution found -- WITHOUT the token.

    Evidence is printed, logged and pasted into chats; only the masked form, the
    fingerprint and the robot_id (none of them secret) may live here. The token
    object itself stays local to :func:`collect`, which signs with it and drops it.
    """

    state: str = "none"  # configured | invalid | none
    source: str = "none"  # file | env | none
    path: str = ""
    error: str = ""
    robot_id: str = ""
    fingerprint: str = ""
    masked: str = ""
    insecure_permissions: bool = False


@dataclass(frozen=True)
class MenuProbe:
    """The ACTIVE voice menu, read through the hostapp's own loader."""

    loaded: bool = False
    reason: str = ""
    options: tuple[tuple[str, str], ...] = ()  # (spoken menu key, module_id), menu order
    source: str = ""  # the resolved yaml path ("" = built-in default menu)
    # --- provenance (defaulted: positional constructions elsewhere stand) ---
    lang: str = ""
    # The GENERATED production menu for this language (server.prod_menu_source),
    # "" when none is installed.  What ``source`` SHOULD be on a prod-pointed robot.
    prod_source: str = ""
    # sha256 of the file actually loaded ("" for the built-in default or an
    # unreadable file): what tells a shadowing file from an identical copy.
    digest: str = ""
    # sha256 of ``prod_source`` -- so an override that merely POINTS somewhere else
    # while carrying identical content is not reported as a divergence.
    prod_digest: str = ""
    # The env var that redirected this menu away from the normal search ("" = none).
    override_env: str = ""
    # The tier the hostapp serves it from (server.menu_source): env | site | prod | auto
    # | dev | builtin. "" = not known, which the gate judges exactly as it always did.
    tier: str = ""
    # A menu built from this robot's Pokamind record (tier auto): its ``authored``
    # label -- pokamind | title_draft | mixed. "" for every other file.
    authored: str = ""


@dataclass(frozen=True)
class SiteProbe:
    """The robot-local settings layer as THIS process applied it -- names and codes only.

    ``emori_adapter.config`` ran ``site_settings.apply`` when readycheck was imported, so
    ``sources`` is what the hostapp's next start would see, re-derived rather than
    inherited from whoever started readycheck. No value is ever held here: the matrix,
    the JSON and the journal may all carry this record.
    """

    probed: bool = False  # False = nothing was asked (the gate SKIPs, never PASSes)
    # (KEY, source) for every robot-local setting that has a value: source is env |
    # admin | calibration | calibration_fallback | pokamind | hardware | default.
    sources: tuple[tuple[str, str], ...] = ()
    # (KEY, slug) for every value saved on this robot that is refused, so not applied.
    invalid: tuple[tuple[str, str], ...] = ()
    auto: bool = True  # EMORI_SITE_AUTO is not 0: values are derived from Pokamind
    # This token's scope snapshot: ok | scope_missing | scope_unreadable |
    # scope_schema_unknown | scope_robot_mismatch | scope_fingerprint_mismatch;
    # "" = not asked (no usable token).
    scope_status: str = ""
    scope_fetched_at: float | None = None  # epoch seconds of the last verified sync
    scope_contract: int | None = None      # 1 or 2: which whoami contract answered
    scope_partner: str = ""                # the record's partner
    scope_ignored: tuple[str, ...] = ()    # whoami optional keys ignored as malformed
    scope_robot_id: str = ""               # whose snapshot it is, when not this robot's
    addon_provisioned: bool = False        # the snapshot's sticky add-on flag
    now: float = 0.0                       # the probe's clock, so `evaluate` stays pure
    # What the RUNNING hostapp reports (GET /api/admin/site): None = not asked or no
    # answer, which is never read as "nothing pending".
    restart_pending: bool | None = None
    restart_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    available: bool = False
    returncode: int = 0
    stdout: str = ""


@dataclass(frozen=True)
class DashboardData:
    """What the dashboard's own SQLite file says about the last finished session.

    Read-only, and deliberately COUNTS ONLY: no session id, no person code, no
    excerpt.  A readycheck matrix is pasted into chats and journals, and the one
    table in this system that touches a named person's conversation must not be
    quotable from it.  ``ok=False`` with a ``reason`` slug is how "could not look"
    is reported - it is never confused with "looked, and found nothing".
    """

    ok: bool = False
    reason: str = ""            # why the file could not be read (safe slug)
    path: str = ""              # the resolved DB path, as the writer would resolve it
    now: float = 0.0            # the probe's own clock, so `evaluate` stays pure
    last_heartbeat_ts: float = 0.0
    heartbeat_interval_s: float = 0.0
    finished_sessions: int = 0  # in the whole file: "has this robot ever finished one"
    # Has ANY session in the file ever recorded a cycle?  The only cycle question a
    # gate can prove: one cycle-less session is an ordinary outcome (an abandonment,
    # a covered lens, an audio-only conversation), a file where nothing has ever
    # produced one is a camera path that has never worked.
    any_cycles_ever: bool = False
    # The most recent COMPLETED session (ended_at IS NOT NULL AND status='completed'),
    # which is the row a manager refreshing the dashboard after a demo is looking at.
    # Deliberately not "the last finished row": an abandoned session -- a dead iframe,
    # a walk-away before the first cycle -- legitimately has no cycles and no scores,
    # and judging the dashboard by one would fail a robot that is being fed perfectly.
    last_completed: bool = False   # was there such a row at all
    last_finished_at: float = 0.0
    last_finished_status: str = ""
    last_finished_cycles: int = 0
    last_finished_scored: bool = False


# --- the :8000 routes this code needs (gate_daemon_routes) ----------------------------
# Generated from the same enumeration tests/test_daemon_contract.py checks, and shipped
# beside this module. CI proves the calls against the shim's SOURCE; this file is how the
# robot proves them against the shim (or Pollen daemon) it is actually RUNNING.
_ROUTE_MANIFEST = Path(__file__).with_name("daemon_routes.json")
_ROUTE_MANIFEST_COMMAND = "python tests/test_daemon_contract.py --write-manifest"
_ROUTE_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
_ROUTE_PARAM_RE = re.compile(r"\{[^{}]*\}")
_ROUTE_SAMPLE = "0"
_OPENAPI_PATH = "/openapi.json"
_ROUTES_NAMED = 6   # routes a hint names before "and N more"


@dataclass(frozen=True)
class RouteNeed:
    """One ``:8000`` route this code calls: an entry of :data:`_ROUTE_MANIFEST`."""

    method: str
    path: str            # the route's FastAPI template: "/api/motors/set_mode/{mode}"
    call: str            # a call the code makes on it; "{}" = a value known at run time
    v1: bool = True      # needed from :8000 when Pollen's daemon answers there
    v2: bool = True      # needed from :8000 when the v2 robot node's shim answers there
    fallback: str = ""   # "netman"/"mixer": emori-admin answers it itself on a 404

    @property
    def shape(self) -> str:
        """The template with every path parameter as ``{}``: openapi renders
        ``{dataset_name:path}`` as ``{dataset_name}``, and names are not the route."""

        return _ROUTE_PARAM_RE.sub("{}", self.path)

    @property
    def sample(self) -> str:
        """The code's own call, ready to ask about: each run-time value a harmless
        ``0`` (it fits a str, int, float and path converter alike)."""

        return self.call.split("?", 1)[0].replace("{}", _ROUTE_SAMPLE)

    @property
    def has_params(self) -> bool:
        return "{" in self.path


@dataclass(frozen=True)
class RouteProbe:
    """The routes this code needs from ``:8000``, and what the RUNNING ``:8000`` said.

    Asked without running any of them: ``GET /openapi.json`` (FastAPI's own route
    table; Pollen's daemon and the v2 shim both publish it), then ``OPTIONS`` for each
    needed route it does not list -- Starlette answers 405 for a path it has under
    another method and 404 for one it does not, before any handler is called.
    """

    needs: tuple[RouteNeed, ...] = ()
    # "" | "route_manifest_missing" | "route_manifest_invalid": the file did not load.
    manifest_error: str = ""
    reached: bool = False            # :8000 answered the openapi GET at all
    # (METHOD, shape) pairs from /openapi.json; None = it published no usable table.
    openapi: tuple[tuple[str, str], ...] | None = None
    # (sample path, HTTP status or 0 for no answer, its Allow header) per OPTIONS sent.
    options: tuple[tuple[str, int, str], ...] = ()


@dataclass(frozen=True)
class Evidence:
    """Everything the gates are allowed to look at.  Collected once, then pure."""

    healthz: HttpResult
    daemon: HttpResult
    motors: HttpResult
    tts: HttpResult
    stt: HttpResult
    pokamind: HttpResult
    camera: CommandResult
    gesture_enabled: bool
    gesture_mode: str
    # UNSET is the whole point of this one: it is physically coupled to
    # push_turn, so "not configured" is a real, reportable state (see
    # gate_gesture).  A bool would erase the difference from an explicit 0.
    gesture_invert: str
    reachy_base_url: str
    pokamind_base: str
    # The session chain: mint -> SSO 302 -> frontend origin.
    sso: SsoResult
    # The same chain walked as a REGISTERED BADGE: the anonymous probe above sends no
    # pathway/course claims at all, so it passes on a robot where every badge is dead.
    badge_sso: SsoResult
    badge_course_id: str
    badge_pathway: str
    frontend: HttpResult
    menu: MenuProbe
    hostapp_url: str
    lang: str
    # The phone superadmin service (emori-admin, :8070). Defaulted so a caller that
    # predates it still constructs an Evidence, and unreachable-by-default so the
    # gate can only ever WARN, never fabricate a PASS, on such a record.
    admin: HttpResult = HttpResult()
    # Its deep check (GET /healthz/deep): Wi-Fi status and a volume read through
    # the admin's own backend selection. Unreached by default (= never a PASS), and
    # only asked when /healthz answered and EMORI_ADMIN_TOKEN is set.
    admin_deep: HttpResult = HttpResult()
    admin_token_set: bool = False
    # DEPLOYED_COMMIT.txt as it is now ("" when absent: nothing to hold emori-admin to).
    deployed_commit: str = ""
    # The v2 fence flag is raised: :8000 is the robot node's, answering or not.
    v2_fence: bool = False
    v2_without_fence: bool = False
    # Every route this code calls on :8000, and whether the RUNNING :8000 has it
    # (gate_daemon_routes). Nothing asked by default, which can only ever SKIP.
    daemon_routes: RouteProbe = RouteProbe()
    # --- dashboard data liveness (gate_dashboard_data) -------------------------------
    # Would `/api/start` hand the kiosk the DEMO url (server.py: `if DEMO or
    # ROBOT_TOKEN is None`)?  A demo session runs the whole conversation and Pokamind never sends a
    # completion payload back, so every dashboard outcome column stays NULL - the
    # dashboard is blank for a reason that has nothing to do with the dashboard.
    # This is the answer derived from THIS process's environment, and it is only the
    # FALLBACK: `/healthz` carries what the running hostapp actually computed, and the
    # two differ exactly when .env was edited without a restart (see `_demo_branch`).
    start_demo_branch: bool = False
    dashboard_data: DashboardData = DashboardData()
    # --- the v2 face look-at camera tee (gate_gaze) -----------------------------------
    # The SECOND v4l2loopback node, the one the ROS robot node captures for face
    # look-at.  Empty means "this robot did not opt in", which is a v1 robot or one
    # installed before the tee existed -- not a fault.  EMORI_READYCHECK_ROS_VIDEO_DEVICE
    # when set, else the tee EMORI_CAM_ROS_VIDEO_NR arms in /etc/default/emori-camera
    # (the bridge's own file), so arming the tee is the only step a robot needs.
    gaze_video: str = ""
    camera_ros: CommandResult = CommandResult()      # v4l2-ctl on that device
    voice_delay_ms: str = ""                         # EMORI_VOICE_DELAY_MS, as written
    voice_nodes: CommandResult = CommandResult()     # pw-cli ls Node
    # --- THIS robot's own Pokamind token -------------------------------------------
    token: TokenProbe = TokenProbe()
    # EMORI_ROBOT_ID as configured, slugged exactly like identity.py ("" = unset, i.e.
    # derived from the token -- which is fine).
    env_robot_id: str = ""
    # GET /api/robot/whoami, signed with this robot's own token; None = not asked
    # (no token, or no valid one).
    whoami: WhoamiResult | None = None
    # The course claim an anonymous voice-menu session is minted with (server.py
    # voice_select: EMORI_COURSE_ID); the roster's own is ``badge_course_id``.
    course_id_env: str = ""
    # Is the roster's anonymous (drop-in) identity this robot's own
    # robot-<robot_id>@emori.local? A boolean and never the address: an explicit
    # ``anonymous:`` block may name a real mailbox. None = not probed.
    anonymous_is_robot: bool | None = None
    # --- the optional ROS 2 lane (gate_ros2) -----------------------------------------
    # ``EMORI_WITH_ROS2=1``: the operator opted into the containerised ros4hri graph
    # (install.sh step 11) -- or the v2 fence is up, which makes the v2 container the
    # robot's only motor path.  Everything below it is probed ONLY when that is true, so
    # a normal robot never shells out to docker and the gate costs it nothing.
    ros2_enabled: bool = False
    # What the HOST's .env says about which lane may move the robot.  docker compose
    # interpolates this same value into the container at ``up`` time, so a difference
    # between the two is an .env edited without recreating the container.
    ros_motion_owner: str = "hostapp"
    ros2_container: CommandResult = CommandResult()  # docker inspect .State.Running
    # `docker version`, asked only when BOTH inspects failed: "no such container" and
    # "docker would not answer" (the socket refused readycheck's user, dockerd is down)
    # exit the same way, and only the second leaves the container's state unknown.
    # Unasked (the default), a failed inspect means the container is absent.
    ros2_docker: CommandResult = CommandResult()
    # A fenced robot with reachy-mini-ros2.service: the v2 graph runs natively (Route B),
    # with no container to ask about. Its robot node is judged by the shim on :8000.
    ros2_native: bool = False
    # "" when nothing is running, else "v1" (daemon + observer graph) or "v2" (the
    # robot node IS the graph). Everything flavour-specific below hangs off this.
    ros2_flavor: str = ""
    # Both containers running at once: two graphs on one DDS domain, two engagement
    # lanes, and under v2 a second process reaching for the same serial bus.
    ros2_both_running: bool = False
    # EMORI_SKILL_TRANSPORT inside the container. `ros_native` means the facade calls
    # the robot node's actions directly and the hostapp intake is not in the path at
    # all -- so demanding /api/skill there would fail a robot for missing a route it has
    # no use for.
    ros2_transport: CommandResult = CommandResult()
    ros2_fiware: CommandResult = CommandResult()     # FIWARE_BRIDGE_ENABLED, in-container
    ros2_owner: CommandResult = CommandResult()      # EMORI_ROS_MOTION_OWNER, in-container
    # `ros2 node list` INSIDE the container: the only evidence here that the ROS graph
    # is running rather than the process that hosts it.  A node that dies on an import
    # takes its topics with it and leaves every other check green.
    ros2_nodes: CommandResult = CommandResult()
    # A GET on the POST-only ``/api/skill``: 405 means the route is mounted, 404 means
    # ``EMORI_SKILL_API`` is off.  A GET can never submit motion, so the probe that
    # proves the ROS lane has somewhere to POST stays read-only.
    skill_api: HttpResult = HttpResult()
    # --- the robot's clock (gate_clock) -----------------------------------------------
    # Unmeasured by default, so a record that predates the gate SKIPs, never PASSes.
    clock: ClockProbe = ClockProbe()
    # --- which physical robot (gate_identity) ------------------------------------------
    identity: IdentityProbe = IdentityProbe()
    # --- the Piper voices the robot speaks with (gate_tts) ----------------------------
    # The languages this robot speaks -- the kiosk's, and any an installed add-on speaks
    # (``ContributedGate.voice_languages``) -- that have no whole voice on
    # disk. A failed download no longer stops install.sh, so a missing voice is only
    # visible here: /api/tts still answers 200, in the first voice's accent.
    voices_missing: tuple[str, ...] = ()
    # --- the robot-local settings layer (gate_site_config) -----------------------------
    # Unprobed by default, so a record that predates the gate SKIPs, never PASSes.
    site: SiteProbe = SiteProbe()
    # EMORI_PARTNER_NAME as set EXPLICITLY ("" = unset: the protocol constant applies).
    partner_env: str = ""
    # EMORI_SMTP_HOST is set but there is no sender (results_email.sender_from is ""):
    # every results e-mail goes to the outbox instead.
    smtp_sender_missing: bool = False
    # --- installed add-ons (ContributedGate) --------------------------------------------
    # What each contributed gate's collector returned, by the gate's ``key``. Empty with
    # no add-on installed.
    addon: dict = field(default_factory=dict)
    # Non-empty when an add-on needs this robot's clock not AHEAD of Pokamind's at all:
    # the reason, as the add-on words it (see gate_clock).
    clock_strict_reason: str = ""
    # --- the hub (gate_hub_identity, gate_hub_audio, gate_ros2) --------------------------
    # The flag file is up: the hub container runs the gateway. Everything below it is asked
    # only then, so a robot on the host application never shells out for any of it.
    hub_mode: bool = False
    hub_container: CommandResult = CommandResult()   # docker inspect .State.Running
    hub_fiware: CommandResult = CommandResult()      # FIWARE_BRIDGE_ENABLED, in-container
    # Writable by the container's user (uid 1000): the directory the hub mounts for caches.
    hub_cache: CommandResult = CommandResult()
    # GET /api/robot/status: the hub's own view of the robot link, the motion owner and the
    # skill transport. `docker exec printenv` shows only the compose file's environment, not
    # what the unit's environment files gave the process, so these come from the process.
    hub_status: HttpResult = HttpResult()
    app_status: HttpResult = HttpResult()            # GET /api/status (voice tap, mic)
    hub_addon_status: HttpResult = HttpResult()      # GET /api/addon/status
    # Does THIS process find an installed add-on? None = not asked.
    host_has_addon: bool | None = None
    robot_transport: str = ""                        # EMORI_ROBOT_TRANSPORT as written


# ------------------------------------------------------------------ add-on contributions


@dataclass(frozen=True)
class GateContext:
    """What a contributed gate's collector may use besides the probe."""

    hostapp_url: str
    lang: str
    site: SiteProbe


@dataclass(frozen=True)
class ContributedGate:
    """One gate an installed add-on adds (``extensions.contributed("readiness_gates")``).

    ``run`` is a gate like the built-in ones: pure over :class:`Evidence`, naming its fix.
    ``after`` is the built-in (or earlier contributed) gate it follows, by name; unknown or
    empty appends it. ``collect(probe, ctx)`` gathers the evidence the gate judges, once per
    ``key``, into ``Evidence.addon[key]`` -- a gate that shares another's key leaves it None.
    ``voice_languages(evidence)`` names languages the add-on speaks (the tts gate then
    checks their voices); ``clock_strict(evidence)`` returns a reason when the add-on needs
    the clock not ahead of Pokamind's at all, else "".
    """

    name: str
    run: Callable[["Evidence"], "GateResult"]
    after: str = ""
    key: str = ""
    collect: Optional[Callable[[Any, GateContext], Any]] = None
    voice_languages: Optional[Callable[[Any], Iterable[str]]] = None
    clock_strict: Optional[Callable[[Any], str]] = None


def _contributed_gates() -> tuple:
    """The installed add-on's gates, or ``()``. A malformed entry is dropped."""

    found = _extensions.contributed("readiness_gates")
    if not isinstance(found, (list, tuple)):
        return ()
    return tuple(gate for gate in found
                 if isinstance(getattr(gate, "name", None), str) and callable(getattr(gate, "run", None)))


def _collect_contributed(probe, ctx: GateContext, gates) -> dict:
    """Run each key's collector once. A collector that raises leaves its key absent."""

    evidence: dict = {}
    for gate in gates:
        if gate.collect is None or gate.key in evidence:
            continue
        try:
            evidence[gate.key] = gate.collect(probe, ctx)
        except Exception:  # noqa: BLE001 - a probe never raises; an add-on might
            continue
    return evidence


def _contributed_voice_languages(gates, addon: dict) -> tuple[str, ...]:
    languages: list[str] = []
    for gate in gates:
        if gate.voice_languages is None or gate.key not in addon:
            continue
        try:
            languages.extend(str(code) for code in gate.voice_languages(addon[gate.key]))
        except Exception:  # noqa: BLE001
            continue
    return tuple(languages)


def _contributed_clock_reason(gates, addon: dict) -> str:
    for gate in gates:
        if gate.clock_strict is None or gate.key not in addon:
            continue
        try:
            reason = gate.clock_strict(addon[gate.key])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(reason, str) and reason:
            return reason
    return ""


# --------------------------------------------------------------------------- probe


class SystemProbe:
    """The only component that touches the network, the filesystem, or PortAudio.

    Every method fails soft and returns a record: a probe never raises, so one
    dead dependency can never abort the other eleven gates.
    """

    def __init__(self, *, timeout_s: float = 4.0) -> None:
        self._timeout_s = timeout_s

    def env(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    def http(self, url: str, *, bearer: str = "") -> HttpResult:
        import httpx  # core dependency (pyproject.toml)

        headers = {"Authorization": f"Bearer {bearer}"} if bearer else None
        sent_at = time.time()
        try:
            response = httpx.get(url, timeout=self._timeout_s, follow_redirects=True,
                                 headers=headers)
        except Exception:  # noqa: BLE001 - any transport fault is "not reached"
            return HttpResult(reached=False)
        received_at = time.time()
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001 - a WAV body is not JSON, and that is fine
            parsed = None
        return HttpResult(
            reached=True,
            status=response.status_code,
            payload=parsed if type(parsed) is dict else None,
            content_type=response.headers.get("content-type", ""),
            date=response.headers.get("date", ""),
            sent_at=sent_at,
            received_at=received_at,
            age=response.headers.get("age", ""),
            redirected=bool(getattr(response, "history", None)),
        )

    def deployed_commit(self) -> str:
        """``DEPLOYED_COMMIT.txt`` in the install root, first line (``""`` if none)."""

        root = _robot_token.install_root()
        try:
            text = (Path(root) if root is not None else Path.cwd()).joinpath(
                _DEPLOYED_MARKER).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return _marker_line(text)

    def path_exists(self, path: str) -> bool:
        try:
            return os.path.exists(path)
        except (OSError, ValueError):
            return False

    def read_text(self, path: str) -> str:
        """A small config file's text, ``""`` when it is missing, unreadable or too big."""

        try:
            with open(path, "rb") as handle:
                data = handle.read(_CAMERA_DEFAULTS_MAX_BYTES + 1)
        except (OSError, ValueError):
            return ""
        if len(data) > _CAMERA_DEFAULTS_MAX_BYTES:
            return ""
        return data.decode("utf-8", errors="replace")

    def admin_deep(self, url: str, token: str) -> HttpResult:
        """emori-admin's ``GET /healthz/deep``, with the shared admin token.

        The token goes in a header and nowhere else (never a URL, never a result).
        A read: emori-admin answers it with a Wi-Fi status and a volume read, never a
        rescan.
        """

        return self._get_with_admin_token(url, token, _ADMIN_DEEP_TIMEOUT_S)

    def hostapp_site(self, url: str, token: str) -> HttpResult:
        """The hostapp's ``GET /api/admin/site`` (loopback, the shared admin token).

        The answer carries the non-secret settings' values; :func:`collect` keeps only
        ``restart_pending`` and the restart reasons out of it.
        """

        return self._get_with_admin_token(url, token, self._timeout_s)

    @staticmethod
    def _get_with_admin_token(url: str, token: str, timeout_s: float) -> HttpResult:
        """A GET carrying the admin token in its header, and nowhere else."""

        import httpx

        try:
            response = httpx.get(url, timeout=timeout_s,
                                 headers={_ADMIN_TOKEN_HEADER: token})
        except Exception:  # noqa: BLE001 - any transport fault is "not reached"
            return HttpResult(reached=False)
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001
            parsed = None
        return HttpResult(reached=True, status=response.status_code,
                          payload=parsed if type(parsed) is dict else None,
                          content_type=response.headers.get("content-type", ""))

    def site_config(self, robot_id: str, fingerprint: str = "") -> SiteProbe:
        """The robot-local settings layer, as this process applied it (site_settings).

        ``robot_id``/``fingerprint`` are the token this robot runs with ("" = none): the
        scope snapshot on disk is judged against them exactly as the hostapp judges it.
        Read-only, total, and value-free: key names, layer names and codes.
        """

        now = time.time()
        try:
            sources = _site_settings.effective_sources()
        except Exception:  # noqa: BLE001 - a probe never raises
            sources = {}
        try:
            invalid = _site_settings.check_stored_settings()
        except Exception:  # noqa: BLE001
            invalid = {}
        status = ""
        fetched = contract = None
        partner = other = ""
        ignored: tuple[str, ...] = ()
        provisioned = False
        if robot_id:
            try:
                status = _site_settings.scope_snapshot_status(robot_id, fingerprint or None)
                data = (_site_settings.load_scope_snapshot(robot_id, fingerprint or None)
                        if status == "ok" else _site_settings.read_scope_file()[0])
            except Exception:  # noqa: BLE001 - an unreadable snapshot is reported as such
                status, data = "scope_unreadable", None
            data = data if isinstance(data, dict) else {}
            if status == "ok":
                stamp = data.get("fetched_at")
                fetched = float(stamp) if type(stamp) in (int, float) else None
                number = data.get("contract")
                contract = number if type(number) is int else None
                whoami = data.get("whoami") if isinstance(data.get("whoami"), dict) else {}
                value = whoami.get("partner")
                partner = value if isinstance(value, str) and _PARTNER_RE.fullmatch(
                    value) else ""
                names = whoami.get("ignored_keys")
                ignored = tuple(name for name in names if isinstance(name, str)
                                and _IGNORED_KEY_RE.fullmatch(name)) \
                    if isinstance(names, list) else ()
                provisioned = data.get("addon_provisioned") is True
            else:
                owner = data.get("robot_id")
                other = owner if isinstance(owner, str) and \
                    _robot_token.ROBOT_ID_RE.fullmatch(owner) else ""
        return SiteProbe(
            probed=True,
            sources=tuple(sorted(
                (key, source) for key, source in sources.items()
                if _KEY_NAME_RE.fullmatch(key) and source in _site_settings.SOURCES)),
            invalid=tuple(sorted(
                (key, slug) for key, slug in invalid.items()
                if _KEY_NAME_RE.fullmatch(key) and SAFE_CODE_RE.fullmatch(slug))),
            auto=_site_settings.auto_enabled(),
            scope_status=status if SAFE_CODE_RE.fullmatch(status or "") else "",
            scope_fetched_at=fetched, scope_contract=contract, scope_partner=partner,
            scope_ignored=ignored, scope_robot_id=other,
            addon_provisioned=provisioned, now=now,
        )

    def sso(self, url: str) -> SsoResult:
        """Partner SSO login WITHOUT following the redirect.

        The 302 and its Location are the two facts the whole chain turns on (does the
        backend accept our signature, and does it hand the kiosk a usable, kiosk-flagged
        frontend URL), and following it would burn the one-shot sign-in token the kiosk
        browser needs.
        """

        import httpx

        try:
            response = httpx.get(url, timeout=self._timeout_s, follow_redirects=False)
        except Exception:  # noqa: BLE001
            return SsoResult(reached=False)
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - a redirect has no body
            body = None
        field = ""
        code = ""
        if type(body) is dict:
            # The key's NAME only.  Its value quotes the rejected token / the e-mail --
            # except Pokamind's fixed per-robot scope codes, which carry neither.
            field = next((key for key in ("error", "message", "detail") if key in body), "")
            value = body.get("error")
            if type(value) is str and value in _SSO_SCOPE_CODES:
                code = value
        return SsoResult(
            reached=True,
            status=response.status_code,
            location=response.headers.get("location", ""),
            error_field=field,
            error_code=code,
        )

    def robot_token(self) -> "_robot_token.TokenLoad":
        """This robot's token, resolved exactly as the hostapp resolves it.

        Same file (``EMORI_POKAMIND_TOKEN_FILE``, a relative one resolved against the
        install root -- ``robot_token.install_root`` -- wherever readycheck is run
        from), same env fallback. This is what the NEXT hostapp start will use;
        ``/healthz`` says what the running one uses.
        """

        return _robot_token.load_token()

    def whoami(self, base_url: str, token) -> WhoamiResult:
        """The live ``GET /api/robot/whoami``, signed with this robot's own token."""

        return fetch_whoami(base_url, token, timeout_s=self._timeout_s)

    def roster_anonymous_is_robot(self, robot_id: str) -> bool | None:
        """Does a drop-in session log in as ``robot-<robot_id>@emori.local``?

        Pokamind admits exactly that ``@emori.local`` account for a robot signing with
        its own token, so an ``anonymous:`` roster block pinning any other one turns
        every drop-in session into ``identity_not_in_scope``. Answered through the
        hostapp's own roster loader; only the comparison leaves this method.
        """

        try:
            from emori_adapter.hostapp.server import _load_roster  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - optional extras absent: honest degradation
            return None
        try:
            email = _load_roster().anonymous_identity().email
        except Exception:  # noqa: BLE001 - an unreadable roster is reported as unprobed
            return None
        return email.strip().lower() == f"robot-{robot_id}@emori.local"

    def voice_menu(self, lang: str) -> MenuProbe:
        """The menu the robot will actually speak, via the hostapp's OWN loader.

        Imported rather than re-derived: ``server._menu_candidates`` (the overrides,
        then the menu saved on the admin page, a fork's ``configs/prod/`` menu, the menu
        built from the Pokamind record, the tracked development files, then the built-in
        default -- ``server.menu_source`` names the tier) is the DEFINITION of "the
        active menu", and a second copy of that order here would eventually validate
        module ids the robot never offers -- the exact shape of failure this gate exists
        to catch.  The import is lazy so a box without the hostapp's extras SKIPs
        instead of exploding.
        """

        try:
            from emori_adapter.hostapp.server import (  # noqa: PLC0415
                _load_menu,
                _menu_source,
                menu_override_env,
                prod_menu_source,
            )
        except Exception:  # noqa: BLE001 - optional extras absent: honest degradation
            return MenuProbe(loaded=False, reason="menu_loader_unavailable")
        try:
            from emori_adapter.hostapp.server import menu_source  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - a hostapp without tiers: judged as before
            menu_source = None
        try:
            menu = _load_menu(lang)
            options = tuple(
                (str(key), str(option.module_id)) for key, option in menu.options.items()
            )
        except Exception:  # noqa: BLE001 - unparsable/invalid YAML is a REAL failure
            return MenuProbe(loaded=False, reason="voice_menu_unreadable")
        source = _menu_source(lang) or ""
        # Provenance, all fail-soft: a robot whose configs/prod is missing must still
        # produce a MenuProbe (the gate then FAILs about the missing prod menu),
        # never an exception that takes the other seventeen gates with it.
        try:
            prod_source = prod_menu_source(lang) or ""
        except Exception:  # noqa: BLE001
            prod_source = ""
        try:
            override_env = menu_override_env(lang)
        except Exception:  # noqa: BLE001
            override_env = ""
        tier = ""
        if menu_source is not None:
            try:
                tier = str(menu_source(lang))
            except Exception:  # noqa: BLE001
                tier = ""
        tier = tier if tier in _MENU_TIERS else ""
        return MenuProbe(loaded=True, options=options, source=source,
                         lang=lang, prod_source=prod_source,
                         digest=file_digest(source), prod_digest=file_digest(prod_source),
                         override_env=override_env, tier=tier,
                         authored=menu_authored(source) if tier == "auto" else "")

    def roster_badge_claims(self) -> tuple[str, str]:
        """``(course_id, pathway)`` a REGISTERED badge would send, or ``("", "")``.

        Read through the hostapp's own roster loader, lazily, so a box without the
        extras degrades to "not probed" instead of exploding.  Claims only: no
        student_id, e-mail, name or badge value leaves the roster (the badge probe
        brings its own probe identity, exactly like the anonymous one).
        """

        try:
            from emori_adapter.hostapp.server import _load_roster  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - optional extras absent: honest degradation
            return "", ""
        try:
            course_id, pathway = _load_roster().badge_claim_sample()
        except Exception:  # noqa: BLE001 - an unreadable roster is reported as unprobed
            return "", ""
        return str(course_id or ""), str(pathway or "")

    def robot_identity(self) -> IdentityProbe:
        """The board's serial and Wi-Fi MAC, and what install.sh recorded (cwd-relative,
        like DEPLOYED_COMMIT.txt: readycheck.sh runs from the install root)."""

        from emori_adapter import robot_identity  # noqa: PLC0415 - stdlib only

        recorded = robot_identity.read_record(robot_identity.IDENTITY_FILE)
        return IdentityProbe(serial=robot_identity.hardware_serial(),
                             wlan_mac=robot_identity.wlan_mac(),
                             recorded_serial=recorded.get("serial", ""),
                             recorded_robot_id=recorded.get("robot_id", ""))

    def piper_voices_missing(self, languages) -> tuple[str, ...]:
        """The languages in ``languages`` without a whole Piper voice on disk.

        Resolved exactly as the hostapp's ``speech_api.build_tts`` resolves them
        (``PIPER_VOICE_<LANG>``, else ``voices/<lang>.onnx`` relative to the install root
        readycheck.sh runs from), plus the ``.onnx.json`` Piper loads beside the model.
        Something that is not a bare language tag names no voice: not this check's business.
        """

        try:
            from emori_adapter.hostapp.speech_api import (  # noqa: PLC0415 - stdlib only
                PIPER_VOICE_DEFAULT,
                piper_voice_env,
            )
        except Exception:  # noqa: BLE001 - a probe never raises
            return ()
        missing = []
        for lang in languages:
            env = piper_voice_env(lang)
            if env is None:
                continue
            path = self.env(env, "").strip() or PIPER_VOICE_DEFAULT.format(lang=lang)
            try:
                whole = os.path.getsize(path) > 0 and os.path.getsize(path + ".json") > 0
            except OSError:
                whole = False
            if not whole:
                missing.append(lang)
        return tuple(missing)

    def ntp_synchronized(self) -> str:
        """``timedatectl``'s NTPSynchronized: "yes", "no", or "" when it cannot say."""

        result = self.command(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        answer = result.stdout.strip().lower() if result.available else ""
        return answer if result.returncode == 0 and answer in ("yes", "no") else ""

    def speak(self, hostapp_url: str, lang: str, text: str) -> bool:
        """Say one line through the robot's speaker.  Best-effort; never raises.

        Synthesised by the hostapp's own Piper endpoint (the only voice on the robot)
        and played with ``pw-play``: the appliance runs PipeWire, and the daemon is
        started ``--no-media`` precisely so this sink is free.  Every failure mode here
        (no TTS engine, no pw-play, a busy sink) degrades to silence - the matrix is
        still printed and the exit code is still returned.
        """

        import httpx

        url = (f"{hostapp_url.rstrip('/')}/api/tts"
               f"?text={quote(text, safe='')}&lang={quote(lang, safe='')}")
        try:
            response = httpx.get(url, timeout=self._timeout_s, follow_redirects=True)
            if response.status_code != 200:
                return False
            if "audio/" not in response.headers.get("content-type", ""):
                return False
            audio = response.content
        except Exception:  # noqa: BLE001
            return False
        if not audio:
            return False

        path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="emori-readycheck-", suffix=".wav",
                                             delete=False) as handle:
                handle.write(audio)
                path = handle.name
            argv = ["pw-play"]
            target = self.env("EMORI_READYCHECK_PLAY_TARGET").strip()
            if target:
                argv.append(f"--target={target}")
            argv.append(path)
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, capture_output=True, text=True,
                timeout=_PLAYBACK_TIMEOUT_S, check=False,
            )
            return completed.returncode == 0
        except Exception:  # noqa: BLE001 - a missing binary must not hide the matrix
            return False
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    def command(
        self, argv: list[str], *, timeout_s: float | None = None
    ) -> CommandResult:
        # ``timeout_s`` exists for the one probe the shared 4 s budget cannot
        # hold: ``ros2 node list`` inside the emori-ros2 container takes ~4.4 s
        # WARM on the CM4 (the ros2 CLI daemon round trip), so under the default
        # budget the graph probe would always expire and the gate would report
        # SKIP ros2_graph_unreadable on every healthy robot.
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, capture_output=True, text=True,
                timeout=timeout_s if timeout_s is not None else self._timeout_s,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return CommandResult(available=False)
        return CommandResult(available=True, returncode=completed.returncode,
                             stdout=completed.stdout or "")

    def dashboard_data(self, path: str) -> DashboardData:
        """The dashboard's SQLite file, opened READ-ONLY: five small indexed reads.

        ``mode=ro`` is not decoration.  This runs while a session may be in
        progress, and a read-write open of a WAL database recovers the log and can
        checkpoint it - a self-check must never be able to touch the data it is
        reporting on.  ``ro`` also makes the ordinary failure honest: a path that
        does not exist cannot be created here and silently answered "0 sessions",
        which is the exact shape of "the dashboard shows zeros".

        The "last session" read is filtered to ``status='completed'``: an abandoned
        row (a dead iframe, a walk-away before the first cycle) is a NORMAL outcome
        with no cycles and no scores, and letting one of those be the sample would
        turn every ordinary abandonment into a red matrix.  Every query is bounded
        (four aggregates over indexed columns and one ``LIMIT 1``), and nothing
        identifying leaves this method - the session id the cycle count is looked up
        by stays local to this frame.
        """

        import sqlite3

        now = time.time()
        try:
            from emori_adapter.hostapp.metrics_store import (  # noqa: PLC0415
                heartbeat_interval_s,
            )
            interval = float(heartbeat_interval_s())
        except Exception:  # noqa: BLE001 - the gate can still judge the rest
            interval = 0.0
        if not path:
            return DashboardData(ok=False, reason="db_path_unset", now=now,
                                 heartbeat_interval_s=interval)
        resolved = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(resolved):
            return DashboardData(ok=False, reason="db_missing", path=resolved, now=now,
                                 heartbeat_interval_s=interval)
        uri = "file:" + quote(Path(resolved).as_posix(), safe="/:") + "?mode=ro"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1.0)
            beat = conn.execute("SELECT MAX(ts) AS ts FROM heartbeats").fetchone()
            finished = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE ended_at IS NOT NULL").fetchone()
            last = conn.execute(
                "SELECT session_id, ended_at, status, performance_scores FROM sessions "
                "WHERE ended_at IS NOT NULL AND status='completed' "
                "ORDER BY ended_at DESC LIMIT 1").fetchone()
            ever = conn.execute("SELECT EXISTS(SELECT 1 FROM cycles)").fetchone()
            cycles = 0
            if last is not None and last[0] is not None:
                row = conn.execute("SELECT COUNT(*) FROM cycles WHERE session_id=?",
                                   (last[0],)).fetchone()
                cycles = int(row[0]) if row and row[0] is not None else 0
        except sqlite3.Error as e:
            # The TYPE only: a sqlite message quotes the SQL, and this record is
            # printed in the matrix.  "no such table" and "unable to open" are
            # different faults an operator acts on differently, so they are kept
            # apart by the exception class, not by the server's own words.
            return DashboardData(ok=False, reason=_sqlite_reason(e), path=resolved,
                                 now=now, heartbeat_interval_s=interval)
        except Exception:  # noqa: BLE001 - a self-check never raises
            return DashboardData(ok=False, reason="db_unreadable", path=resolved,
                                 now=now, heartbeat_interval_s=interval)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        status = str(last[2] or "") if last is not None else ""
        scores = last[3] if last is not None else None
        return DashboardData(
            ok=True, path=resolved, now=now, heartbeat_interval_s=interval,
            last_heartbeat_ts=float(beat[0]) if beat and beat[0] is not None else 0.0,
            finished_sessions=int(finished[0]) if finished and finished[0] else 0,
            any_cycles_ever=bool(ever and ever[0]),
            last_completed=last is not None,
            last_finished_at=(float(last[1]) if last is not None and last[1] is not None
                              else 0.0),
            last_finished_status=status if SAFE_CODE_RE.fullmatch(status) else "",
            last_finished_cycles=cycles,
            last_finished_scored=bool(scores),
        )

    def daemon_routes(self, base_url: str, needs: tuple[RouteNeed, ...]) -> RouteProbe:
        """Which of ``needs`` the running ``:8000`` has (:func:`probe_routes`)."""

        import httpx

        try:
            # No redirects: a 307 is an answer about the path that was asked, not a
            # reason to ask somewhere else.
            with httpx.Client(timeout=self._timeout_s, follow_redirects=False) as client:
                return probe_routes(client, base_url, needs)
        except Exception:  # noqa: BLE001 - a probe never raises
            return RouteProbe(needs=tuple(needs))


def _sqlite_reason(e: BaseException) -> str:
    """A safe slug for a sqlite failure, DERIVED from it - never its text.

    A sqlite message quotes the statement, and this slug is printed, logged and
    forwarded; "no such table" (a foreign or empty file) and "unable to open" (a
    file the self-check may not read) send an operator to two different fixes.
    """

    text = str(e).lower()
    if "no such table" in text:
        return "db_no_schema"
    if "unable to open" in text or "readonly" in text or "permission" in text:
        return "db_unreadable"
    return "db_query_failed"


def _origin(url: str) -> str:
    """The scheme+host a browser would load, or "" if this is not an absolute http URL.

    A relative Location is not something the kiosk can navigate to from the robot's own
    origin, so it is treated exactly like a missing one.
    """

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _has_kiosk_flag(location: str) -> bool:
    name, value = _KIOSK_FLAG
    return value in parse_qs(urlsplit(location).query).get(name, [])


def _motion_owner(raw: str) -> str:
    """Normalise ``EMORI_ROS_MOTION_OWNER``, exactly as ``skill_api.motion_owner`` does.

    Same fail-closed rule, because the two answers are compared: a typo reads as the
    shipped default (``hostapp``) rather than as permission, so a misspelt owner can
    never be the reason a second lane starts writing to the robot.
    """

    value = (raw or "").strip().lower()
    return value if value in _MOTION_OWNERS else _DEFAULT_MOTION_OWNER


def _gateway_unit(ev: "Evidence") -> str:
    """The systemd unit that runs the kiosk's gateway on this robot.

    The remedies say to restart it, and restarting the unit the flag file skips is a start
    that exits 0 and changes nothing: the hub would keep the environment it started with.
    """

    return _HUB_UNIT if ev.hub_mode else _HOSTAPP_UNIT


def _gateway_label(ev: "Evidence") -> str:
    """The same unit as the phone's Services tab names it."""

    return "Hub" if ev.hub_mode else "Host app"


def _container_running(result: CommandResult) -> bool:
    """Did ``docker inspect -f {{.State.Running}}`` answer ``true``?

    An absent docker, an absent container and a stopped one are three different
    sentences to put in front of an operator, so this answers only the last question
    and the gate reads ``available`` / ``returncode`` for the other two.
    """

    return result.available and result.returncode == 0 and result.stdout.strip() == "true"


def file_digest(path: str) -> str:
    """``sha256:<hex>`` of ``path``, or ``""`` when it cannot be read.

    Content and not mtime: a deploy rewrites every timestamp, and the only question
    worth asking about a menu file is whether these are the bytes somebody proved.
    """

    if not path:
        return ""
    try:
        with open(path, "rb") as fh:
            return "sha256:" + hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def smtp_sender(smtp_from: str, smtp_user: str) -> str:
    """The results e-mail's From address, exactly as ``results_email.sender_from`` picks
    it: ``EMORI_SMTP_FROM``, else ``EMORI_SMTP_USER`` when that is an e-mail address,
    else ``""`` (a test holds the two equal). Never printed: only whether it is empty."""

    explicit = (smtp_from or "").strip()
    if explicit:
        return explicit
    user = (smtp_user or "").strip()
    return user if user and _site_settings.validate("EMORI_SMTP_FROM", user) is None else ""


def menu_authored(path: str) -> str:
    """Who named a menu's options, when the menu builder built it, else ``""``.

    ``pokamind`` (every spoken name came from the record), ``title_draft`` (every one
    was cut from a module title) or ``mixed``: the menu builder's top-level ``authored``
    key, believed only beside its ``generated_from`` mark. Bounded and total.
    """

    if not path:
        return ""
    try:
        import yaml  # noqa: PLC0415 - the hostapp's own menu loader needs it too

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh.read(1 << 16))
    except Exception:  # noqa: BLE001 - unreadable is "not a built menu", never a verdict
        return ""
    if not isinstance(data, dict) or data.get("generated_from") != _MENU_GENERATED_FROM:
        return ""
    authored = data.get("authored")
    return authored if isinstance(authored, str) and SAFE_CODE_RE.fullmatch(authored) else ""


def collect(probe) -> Evidence:
    """Run every probe once.  Defaults mirror the real readers, cited inline."""

    port = probe.env("HOSTAPP_PORT", "8080")  # server.py:1115
    hostapp = probe.env("EMORI_READYCHECK_HOSTAPP_URL", f"http://127.0.0.1:{port}").rstrip("/")
    reachy = probe.env("REACHY_BASE_URL", "http://127.0.0.1:8000").rstrip("/")  # server.py:96
    # server.py POKAMIND_BASE: an empty value means unset, and the shipped default is the
    # API origin (the SSO login lives there).
    pokamind = (probe.env("POKAMIND_BASE", "").strip()
                or _site_settings.DEFAULT_POKAMIND_BASE).rstrip("/")
    # server.py EMORI_LANG: the boot default, empty = unset = 'en'.
    boot_lang = probe.env("EMORI_LANG", "").strip() or "en"
    # The toggle persists the LIVE language in a state file that outranks EMORI_LANG;
    # a readycheck (its own CLI process) reading only the env would speak/verify the
    # OLD language after a toggle. Built like the hostapp's, with this robot's language
    # offer, so a persisted language outside its Pokamind scope is ignored here exactly
    # as it is at the hostapp's start. Best-effort: a broken state module falls back to env.
    try:
        from emori_adapter.hostapp.lang_state import LangState, allowed_languages
        lang = LangState(default=boot_lang, allowed=allowed_languages()).current()
    except Exception:  # noqa: BLE001 - readycheck must run even if lang_state cannot
        lang = boot_lang
    video = probe.env("EMORI_READYCHECK_VIDEO_DEVICE", _DEFAULT_VIDEO_DEVICE)
    # No default: the tee is opt-in (EMORI_CAM_ROS_VIDEO_NR in /etc/default/emori-camera),
    # and a default here would fail every v1 robot for not having a device it never wanted.
    # An explicit device wins; otherwise the bridge's own file says whether the tee is
    # armed and which node it feeds, so arming it there is the whole opt-in.
    gaze_video = (probe.env("EMORI_READYCHECK_ROS_VIDEO_DEVICE", "").strip()
                  or camera_tee_device(_probe_file_text(probe, _CAMERA_DEFAULTS)))
    # Read once, used twice: it arms the ROS lane below, and it says who owns :8000.
    v2_fence = _probe_exists(probe, _V2_FENCE_FLAG)
    # 127.0.0.1 and not the bind address: EMORI_ADMIN_HOST is 0.0.0.0 so the phone can
    # reach it over the AP, which is not an address anything can connect TO.
    admin_port = (probe.env("EMORI_ADMIN_PORT", _DEFAULT_ADMIN_PORT).strip()
                  or _DEFAULT_ADMIN_PORT)
    if _PORT_RE.fullmatch(admin_port) is None:
        admin_port = _DEFAULT_ADMIN_PORT
    admin_base = f"http://127.0.0.1:{admin_port}"
    admin = probe.http(f"{admin_base}/healthz")
    # Held in this frame only, like the robot token below. And handed only to the
    # service that said it IS emori-admin: EMORI_ADMIN_PORT can point at anything (the
    # hostapp, a stray dev server), and whatever answers there would otherwise be sent
    # the hostapp's own credential towards emori-admin.
    admin_token = probe.env("EMORI_ADMIN_TOKEN", "").strip()
    admin_deep = (_probe_admin_deep(probe, admin_base + _ADMIN_DEEP_PATH, admin_token)
                  if admin_token and _is_emori_admin(admin) else HttpResult())
    healthz = probe.http(f"{hostapp}/healthz")

    # --- this robot's own Pokamind token ---------------------------------------------
    # Held in this frame only: it signs the probe logins and the whoami call below,
    # and Evidence keeps nothing of it but what is safe to print (TokenProbe).
    token_load = _probe_token(probe)
    token = token_load.token
    token_probe = _token_probe(token_load)
    env_robot_id = _robot_token.robot_slug(probe.env("EMORI_ROBOT_ID", ""))

    # --- the robot-local settings layer (names, layers and codes; never a value) -------
    site = _probe_site(probe, token_probe)
    pending, reasons = _probe_site_restart(probe, hostapp, healthz, admin_token)
    if site.probed:
        site = replace(site, restart_pending=pending, restart_reasons=reasons)

    # --- the session chain, walked in the kiosk's own order -------------------------
    # server.py DEMO verbatim (demo_default): always demo without a token, else
    # HOSTAPP_DEMO=1 forces it. In the demo branch /api/start returns the demo url and
    # no session ever reports an outcome back.
    start_demo_branch = token is None or probe.env("HOSTAPP_DEMO", "0") == "1"
    # Resolved exactly as MetricsStore does (env, then its own default), and against
    # THIS process's cwd - which is the point: a relative default read from a
    # different directory than the hostapp's is a second, empty dashboard.db, and the
    # gate catches it by finding no fresh heartbeat in the file it just resolved.
    try:
        from emori_adapter.hostapp.metrics_store import DEFAULT_DB_PATH  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - keep the documented default rather than crash
        DEFAULT_DB_PATH = "artifacts/dashboard.db"
    dash_db = probe.env("EMORI_DASH_DB", "") or DEFAULT_DB_PATH
    sso = SsoResult(reached=False)
    badge_sso = SsoResult(reached=False)
    badge_course_id, badge_pathway = probe.roster_badge_claims()
    frontend = HttpResult(reached=False)
    # server.py COURSE_ID: the EFFECTIVE course of this process (explicit, saved on the
    # admin page, or derived from this robot's Pokamind record), else the code default.
    course_id_env = probe.env("EMORI_COURSE_ID", "").strip() or _DEFAULT_COURSE_ID

    def _mint(course_id: str, pathway: str | None = None) -> str | None:
        """The probe login, signed with this robot's token, exactly as the KIOSK signs.

        ``None`` when signing is impossible on this box (the token's crypto extras are
        missing) -- reported as an unreached login rather than a made-up rejection.
        """

        claims = dict(course_id=course_id, pathway=pathway, ttl_s=_PROBE_TOKEN_TTL_S,
                      extra={"emori_lang": lang, "emori_kiosk": True},
                      **_token_probe_identity(token.robot_id,
                                              probe.env("EMORI_ROBOT_NAME", "")))
        try:
            return mint_robot_sso_jwt(token, **claims)
        except Exception:  # noqa: BLE001 - PyJWT/cryptography absent or broken
            return None

    if token is not None:
        # Without a token there is nothing to sign with: pokamind_token FAILs and the
        # chain gates SKIP. No module_id/pathway: the probe must not depend on any single module
        # existing (the voice_menu gate proves those) nor reserve a topic.
        probe_token = _mint(course_id_env)
        if probe_token is not None:
            sso = probe.sso(build_sso_url(
                pokamind, probe.env("EMORI_PARTNER_NAME", "emori-robot"), probe_token))
        origin = _origin(sso.location)
        if origin:
            # The app origin the SERVER names, not one this check assumes: a wrong
            # app origin is exactly the kind of drift that leaves a blank iframe.
            frontend = probe.http(f"{origin}/")
        if badge_pathway:
            # The badge leg, minted with the roster's OWN course + pathway claims and
            # this check's probe identity.  Run only when the roster actually pins a
            # pathway: without one a badge sends the same claims the probe above already
            # walked, and a second identical login would buy nothing.
            badge_token = _mint(badge_course_id or course_id_env, badge_pathway)
            if badge_token is not None:
                badge_sso = probe.sso(build_sso_url(
                    pokamind, probe.env("EMORI_PARTNER_NAME", "emori-robot"), badge_token))

    # --- what Pokamind says this robot may do -----------------------------------------
    # Asked with the robot's own credential: the automatic menu-scope check.
    whoami = _probe_whoami(probe, pokamind, token) if token is not None else None
    anonymous_is_robot = None
    asker = getattr(probe, "roster_anonymous_is_robot", None)
    if token is not None and callable(asker):
        try:
            anonymous_is_robot = asker(token.robot_id)
        except Exception:  # noqa: BLE001 - a probe never raises
            anonymous_is_robot = None

    # --- the optional ROS 2 lane, probed only where it was armed ---------------------
    # Nothing here runs on a normal robot: `EMORI_WITH_ROS2` is the same opt-in that
    # install.sh step 11 reads, and without it this costs one env read.  The container
    # is asked about ITSELF (docker exec printenv) rather than trusted from .env,
    # because compose interpolates .env once, at `up` - an operator who edits the file
    # and restarts nothing has a graph running the old answer.
    #
    # The v2 fence arms it too, and an explicit EMORI_WITH_ROS2=0 does not disarm it:
    # with the fence up Pollen's daemon cannot start, so the v2 container is the only
    # thing that moves this robot. "No ROS graph here" is then false by construction,
    # and `ros2_lane_off` would be a PASS for the one container whose death leaves the
    # robot without motors. Lowering the fence (README "Rolling back to v1") is what
    # takes a robot off v2, and the gate with it.
    # The hub flag arms it as well: the hub only runs against the v2 motor node.
    hub_mode = _probe_exists(probe, _HUB_FLAG)
    ros2_enabled = probe.env("EMORI_WITH_ROS2", "0").strip() == "1" or v2_fence or hub_mode
    ros2_native = v2_fence and _probe_exists(probe, _V2_NATIVE_UNIT)
    ros_motion_owner = _motion_owner(probe.env("EMORI_ROS_MOTION_OWNER", ""))
    ros2_container = CommandResult()
    ros2_docker = CommandResult()
    ros2_fiware = CommandResult()
    ros2_owner = CommandResult()
    ros2_nodes = CommandResult()
    ros2_transport = CommandResult()
    ros2_flavor = ""
    ros2_both_running = False
    skill_api = HttpResult()
    # Asked whatever the settings say: going to v1 turns the lane off, and a motor container still
    # running then is what the lane's gate would never see. A robot that was never on v2 is not
    # asked, and may have no Docker to ask.
    v2_without_fence = (not v2_fence and _probe_exists(probe, _V2_FENCE_DROPIN)
                        and _container_running(probe.command(
                            ["docker", "inspect", "-f", "{{.State.Running}}", _ROS2_V2_CONTAINER])))
    if ros2_enabled:
        # Ask about BOTH containers before judging either. v2 is asked about first only
        # because it is the one that owns the robot when it is up; the answer that
        # matters most is when both are running, which is the failure neither container
        # can see from the inside.
        v2_container = probe.command(
            ["docker", "inspect", "-f", "{{.State.Running}}", _ROS2_V2_CONTAINER])
        v1_container = probe.command(
            ["docker", "inspect", "-f", "{{.State.Running}}", _ROS2_CONTAINER])
        ros2_both_running = (_container_running(v1_container)
                             and _container_running(v2_container))
        if _container_running(v2_container):
            ros2_container, ros2_flavor = v2_container, "v2"
        elif _container_running(v1_container):
            ros2_container, ros2_flavor = v1_container, "v1"
        else:
            # Neither is up. Report the v1 container unless only the v2 one exists at
            # all, or the fence says this is a v2 robot, so "absent" names the container
            # this robot was actually meant to run.
            ros2_container = (v2_container if v2_fence or (
                v1_container.returncode != 0 and v2_container.returncode == 0)
                else v1_container)
            ros2_flavor = "v2" if ros2_container is v2_container else ""
            # Both inspects failed: ask whether docker answers at all, or "absent" would
            # be said of a container this user simply cannot see.
            if (v1_container.available and v2_container.available
                    and v1_container.returncode != 0 and v2_container.returncode != 0):
                ros2_docker = probe.command(
                    ["docker", "version", "--format", "{{.Server.Version}}"])
        container = _ROS2_V2_CONTAINER if ros2_flavor == "v2" else _ROS2_CONTAINER
        # A v1 graph on a fenced robot is FAILed as such, with nothing more to ask it.
        if _container_running(ros2_container) and not (v2_fence and ros2_flavor == "v1"):
            # `printenv NAME` and not `env`: the container also holds EMORI_SKILL_KEY,
            # and a readycheck record is pasted into chats and journals.  Only the two
            # values a gate judges ever enter this process.
            ros2_fiware = probe.command(
                ["docker", "exec", container, "printenv", "FIWARE_BRIDGE_ENABLED"])
            ros2_owner = probe.command(
                ["docker", "exec", container, "printenv", "EMORI_ROS_MOTION_OWNER"])
            ros2_transport = probe.command(
                ["docker", "exec", container, "printenv", "EMORI_SKILL_TRANSPORT"])
            # ...and whether the GRAPH is alive, which nothing above can tell: `ros2
            # launch` outlives any single node it started.  Own budget: see
            # ``Probe.command`` - the CLI daemon round trip alone outlives the
            # shared 4 s on the CM4.
            ros2_nodes = probe.command(
                ["docker", "exec", container, "bash", "-lc", _ROS2_NODE_LIST],
                timeout_s=20.0)
        skill_api = probe.http(f"{hostapp}/api/skill")

    app_status = probe.http(f"{hostapp}/api/status")

    # --- the hub container, asked about only where the flag is up -----------------------
    hub_container = CommandResult()
    hub_fiware = CommandResult()
    hub_cache = CommandResult()
    hub_status = HttpResult()
    hub_addon_status = HttpResult()
    host_has_addon: bool | None = None
    if hub_mode:
        hub_container = probe.command(
            ["docker", "inspect", "-f", "{{.State.Running}}", _HUB_CONTAINER])
        if _container_running(hub_container):
            hub_fiware = probe.command(
                ["docker", "exec", _HUB_CONTAINER, "printenv", "FIWARE_BRIDGE_ENABLED"])
            hub_cache = probe.command(
                ["docker", "exec", _HUB_CONTAINER, "test", "-w", _HUB_CACHE_DIR])
        hub_status = probe.http(f"{hostapp}/api/robot/status")
        hub_addon_status = probe.http(f"{hostapp}/api/addon/status")
        try:
            host_has_addon = _extensions.extension_class() is not None
        except Exception:  # noqa: BLE001 - a probe never raises
            host_has_addon = None

    menu = probe.voice_menu(lang)

    # The reachability probe doubles as the clock reference: its Date header is
    # Pokamind's own time, the clock every signed robot call is judged by.
    pokamind_reach = probe.http(f"{pokamind}/")
    clock = _probe_clock(probe, pokamind_reach)
    identity = _probe_identity(probe)
    contributed = _contributed_gates()
    addon = _collect_contributed(
        probe, GateContext(hostapp_url=hostapp, lang=lang, site=site), contributed)
    voices_missing = _probe_voices(probe, lang, _contributed_voice_languages(contributed, addon))

    return Evidence(
        healthz=healthz,
        daemon=probe.http(f"{reachy}/api/daemon/status"),
        motors=probe.http(f"{reachy}/api/motors/status"),
        tts=probe.http(f"{hostapp}/api/tts?text={_TTS_PROBE_TEXT}&lang={lang}"),
        stt=probe.http(f"{hostapp}/api/stt/health"),
        pokamind=pokamind_reach,
        camera=probe.command(["v4l2-ctl", "-d", video, "--list-formats-ext"]),
        gaze_video=gaze_video,
        # Not probed at all when the robot did not opt in: the gate SKIPs on the empty
        # string and a v1 robot never shells out for a device it does not have.
        camera_ros=(probe.command(["v4l2-ctl", "-d", gaze_video, "--list-formats-ext"])
                    if gaze_video else CommandResult()),
        gesture_enabled=probe.env("EMORI_GESTURE", "1") != "0",  # server.py:525
        gesture_mode=probe.env("EMORI_GESTURE_MODE", "back_drive").strip().lower(),  # driver.py:999
        gesture_invert=probe.env("EMORI_GESTURE_INVERT", "").strip(),  # driver.py _oriented
        voice_delay_ms=probe.env("EMORI_VOICE_DELAY_MS", "").strip(),
        voice_nodes=(probe.command(["pw-cli", "ls", "Node"])
                     if probe.env("EMORI_VOICE_DELAY_MS", "").strip() not in ("", "0")
                     else CommandResult()),
        reachy_base_url=reachy,
        pokamind_base=pokamind,
        sso=sso,
        badge_sso=badge_sso,
        badge_course_id=badge_course_id,
        badge_pathway=badge_pathway,
        frontend=frontend,
        menu=menu,
        token=token_probe,
        env_robot_id=env_robot_id,
        whoami=whoami,
        course_id_env=course_id_env,
        anonymous_is_robot=anonymous_is_robot,
        hostapp_url=hostapp,
        lang=lang,
        admin=admin,
        admin_deep=admin_deep,
        admin_token_set=bool(admin_token),
        deployed_commit=_probe_text(probe, "deployed_commit"),
        v2_fence=v2_fence,
        v2_without_fence=v2_without_fence,
        ros2_docker=ros2_docker,
        ros2_native=ros2_native,
        daemon_routes=_probe_daemon_routes(probe, reachy),
        start_demo_branch=start_demo_branch,
        dashboard_data=probe.dashboard_data(dash_db),
        ros2_enabled=ros2_enabled,
        ros_motion_owner=ros_motion_owner,
        ros2_container=ros2_container,
        ros2_fiware=ros2_fiware,
        ros2_owner=ros2_owner,
        ros2_nodes=ros2_nodes,
        ros2_transport=ros2_transport,
        ros2_flavor=ros2_flavor,
        ros2_both_running=ros2_both_running,
        skill_api=skill_api,
        clock=clock,
        identity=identity,
        voices_missing=voices_missing,
        site=site,
        partner_env=probe.env("EMORI_PARTNER_NAME", "").strip(),
        smtp_sender_missing=(bool(probe.env("EMORI_SMTP_HOST", "").strip())
                             and not smtp_sender(probe.env("EMORI_SMTP_FROM", ""),
                                                 probe.env("EMORI_SMTP_USER", ""))),
        addon=addon,
        clock_strict_reason=_contributed_clock_reason(contributed, addon),
        hub_mode=hub_mode,
        hub_container=hub_container,
        hub_fiware=hub_fiware,
        hub_cache=hub_cache,
        hub_status=hub_status,
        app_status=app_status,
        hub_addon_status=hub_addon_status,
        host_has_addon=host_has_addon,
        robot_transport=probe.env("EMORI_ROBOT_TRANSPORT", "").strip().lower(),
    )


def measure_skew(date_header: str, sent_at: float,
                 received_at: float) -> tuple[float, float] | None:
    """``(skew_s, uncertainty_s)`` of this clock against a server's ``Date`` header.

    ``skew_s`` is local minus server, positive = this machine is AHEAD. The server wrote
    ``Date`` at some instant between ``sent_at`` and ``received_at`` on this clock, and
    truncated its own time to the second, so its true time then was in ``[D, D + 1)``:
    the answer is the middle of the possible range and ``uncertainty_s`` is half its
    width. ``None`` for a missing or unparsable header or an impossible interval.
    """

    if not date_header or not sent_at or received_at < sent_at:
        return None
    try:
        server = email.utils.parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return None
    if server is None or server.tzinfo is None:
        return None
    seconds = server.timestamp()
    skew = (sent_at + received_at) / 2.0 - (seconds + 0.5)
    return skew, (received_at - sent_at) / 2.0 + 0.5


def _probe_identity(probe) -> IdentityProbe:
    asker = getattr(probe, "robot_identity", None)
    if not callable(asker):
        return IdentityProbe()
    try:
        result = asker()
    except Exception:  # noqa: BLE001 - a probe never raises; a fake might
        return IdentityProbe()
    return result if isinstance(result, IdentityProbe) else IdentityProbe()


def _probe_voices(probe, lang: str, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The languages this robot speaks that have no whole Piper voice. Never raises."""

    asker = getattr(probe, "piper_voices_missing", None)
    if not callable(asker):
        return ()
    try:
        from emori_adapter.hostapp.lang_state import SUPPORTED_LANGS as kiosk  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - the kiosk languages server.py ships with
        kiosk = ("ca", "en")
    wanted = {lang, *kiosk}
    wanted |= set(extra)
    try:
        missing = asker(tuple(sorted(wanted)))
    except Exception:  # noqa: BLE001 - a fake may; the real one never does
        return ()
    return tuple(m for m in missing if isinstance(m, str) and SAFE_CODE_RE.fullmatch(m))


def _date_not_now(answer: HttpResult) -> str:
    """Why ``answer``'s Date header is not its server's clock now, or "" when it is.

    A cache in front of Pokamind (a CDN, a site proxy) serves a stored answer with its
    ORIGINAL Date and an ``Age`` header of how long it was kept: measured blindly, a
    perfectly synchronized robot reads as ``Age`` seconds ahead -- a FAIL. After a
    followed redirect the Date is another server's (a captive portal, a www host).
    """

    age = (answer.age or "").strip()
    if age:
        try:
            seconds = float(age)
        except ValueError:
            return "carried an unreadable Age header (a cache answered)"
        if seconds > 0:
            return f"came from a cache (Age: {seconds:.0f} s)"
    if answer.redirected:
        return "came through a redirect (another server's Date)"
    return ""


def _probe_clock(probe, pokamind: HttpResult) -> ClockProbe:
    """NTP state + the skew against Pokamind's Date header. A probe never raises."""

    asker = getattr(probe, "ntp_synchronized", None)
    ntp = ""
    if callable(asker):
        try:
            answer = asker()
        except Exception:  # noqa: BLE001 - a fake may; the real one never does
            answer = ""
        ntp = answer if answer in ("yes", "no") else ""
    unmeasurable = _date_not_now(pokamind) if pokamind.reached else ""
    if unmeasurable:
        return ClockProbe(ntp=ntp, unmeasurable=unmeasurable)
    measured = (measure_skew(pokamind.date, pokamind.sent_at, pokamind.received_at)
                if pokamind.reached else None)
    if measured is None:
        return ClockProbe(ntp=ntp)
    return ClockProbe(ntp=ntp, skew_s=measured[0], uncertainty_s=measured[1])


def _token_probe_identity(robot_id: str, robot_name: str) -> dict[str, str]:
    """Who a token-signed probe logs in as: this robot's own drop-in learner.

    Pokamind's per-robot SSO admits ``robot-<robot_id>@emori.local`` for the robot
    whose key signed the login, whatever its scope; every other address needs its
    domain in the robot's ``identity_domains`` - empty for a fresh robot -
    and an account already in that org. So this is the one identity a correctly
    provisioned robot can always be probed as, and exactly the account
    ``identity.robot_identity()`` derives for its anonymous sessions (same student_id,
    same name, so a first-ever probe creates the account the kiosk would have).
    """

    return {"student_id": f"robot-{robot_id}",
            "email": f"robot-{robot_id}@emori.local",
            "name": (robot_name or "").strip() or _DEFAULT_ROBOT_NAME}


def _probe_token(probe) -> "_robot_token.TokenLoad":
    """The probe's token resolution, degraded to "none" -- a probe never raises."""

    reader = getattr(probe, "robot_token", None)
    if not callable(reader):
        return _robot_token.TokenLoad()
    try:
        load = reader()
    except Exception:  # noqa: BLE001 - an unreadable token is reported, never fatal
        return _robot_token.TokenLoad(source="file", error="token_file_unreadable")
    return load if isinstance(load, _robot_token.TokenLoad) else _robot_token.TokenLoad()


def _token_probe(load: "_robot_token.TokenLoad") -> TokenProbe:
    token = load.token
    fingerprint = ""
    if token is not None:
        try:
            fingerprint = token.fingerprint
        except Exception:  # noqa: BLE001 - cryptography absent: the rest still stands
            fingerprint = ""
    return TokenProbe(
        state=load.state, source=load.source, path=load.path, error=load.error,
        robot_id=token.robot_id if token is not None else "",
        fingerprint=fingerprint, masked=token.masked if token is not None else "",
        insecure_permissions=load.insecure_permissions,
    )


def _marker_line(text: str) -> str:
    """The deploy marker as both sides read it (``admin/system.deployed_marker``)."""

    lines = (text or "")[:4096].strip().splitlines()
    return lines[0].strip()[:200] if lines else ""


def _probe_text(probe, name: str) -> str:
    asker = getattr(probe, name, None)
    if not callable(asker):
        return ""
    try:
        value = asker()
    except Exception:  # noqa: BLE001 - a probe never raises; a fake might
        return ""
    return value if isinstance(value, str) else ""


def _probe_exists(probe, path: str) -> bool:
    asker = getattr(probe, "path_exists", None)
    if not callable(asker):
        return False
    try:
        return asker(path) is True
    except Exception:  # noqa: BLE001
        return False


def _probe_file_text(probe, path: str) -> str:
    asker = getattr(probe, "read_text", None)
    if not callable(asker):
        return ""
    try:
        value = asker(path)
    except Exception:  # noqa: BLE001 - a probe never raises; a fake might
        return ""
    return value if isinstance(value, str) else ""


_CAM_ROS_NR_RE = re.compile(r"[0-9]{1,3}")


def camera_tee_device(text: str) -> str:
    """The ROS tee device ``/etc/default/emori-camera`` arms, or ``""`` for none.

    Read as systemd reads that ``EnvironmentFile``: comment lines skipped, one pair of
    surrounding quotes dropped, the LAST ``EMORI_CAM_ROS_VIDEO_NR=`` wins. Empty is
    camera-bridge.sh's own "no tee" (the documented rollback), and anything that is not
    a device number names no device the bridge would feed either.
    """

    number = ""
    for line in re.split(r"\r\n|\r|\n", text or ""):
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "EMORI_CAM_ROS_VIDEO_NR":
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
            number = value
    return f"/dev/video{number}" if _CAM_ROS_NR_RE.fullmatch(number) else ""


def _is_emori_admin(healthz: HttpResult) -> bool:
    """``/healthz`` answered 200 and named itself emori-admin (the same test gate_admin
    applies before it trusts anything else from that port)."""

    return (healthz.reached and healthz.status == 200
            and _payload(healthz).get("service") == _ADMIN_SERVICE_NAME)


def _probe_admin_deep(probe, url: str, token: str) -> HttpResult:
    asker = getattr(probe, "admin_deep", None)
    if not callable(asker):
        return HttpResult()
    try:
        result = asker(url, token)
    except Exception:  # noqa: BLE001 - a probe never raises; a fake might
        return HttpResult()
    return result if isinstance(result, HttpResult) else HttpResult()


def _probe_whoami(probe, base_url: str, token) -> WhoamiResult | None:
    asker = getattr(probe, "whoami", None)
    if not callable(asker):
        return None
    try:
        result = asker(base_url, token)
    except Exception:  # noqa: BLE001 - fetch_whoami never raises; a fake might
        return WhoamiResult(code="whoami_unreachable")
    return result if isinstance(result, WhoamiResult) else None


def _probe_site(probe, token: TokenProbe) -> SiteProbe:
    """The settings layer's record, judged against the token in force. Never raises."""

    asker = getattr(probe, "site_config", None)
    if not callable(asker):
        return SiteProbe()
    usable = token.state == "configured"
    try:
        result = asker(token.robot_id if usable else "", token.fingerprint if usable else "")
    except Exception:  # noqa: BLE001 - a probe never raises; a fake might
        return SiteProbe()
    return result if isinstance(result, SiteProbe) else SiteProbe()


def _is_loopback_url(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower() in _LOOPBACK_HOSTS
    except ValueError:
        return False


def _probe_site_restart(probe, hostapp: str, healthz: HttpResult,
                        admin_token: str) -> tuple[bool | None, tuple[str, ...]]:
    """``(restart_pending, reasons)`` as the RUNNING hostapp reports them, or ``(None, ())``.

    Asked only of a hostapp on this machine that answered its own /healthz, and only
    with the admin token in a header: the route sits behind the hostapp's loopback +
    admin-token guard, and EMORI_READYCHECK_HOSTAPP_URL may point anywhere. Everything
    else in the answer (the non-secret settings' values) is dropped here.
    """

    if not admin_token or not _is_loopback_url(hostapp):
        return None, ()
    if not (healthz.reached and healthz.status == 200
            and _payload(healthz).get("status") == "ok"):
        return None, ()
    asker = getattr(probe, "hostapp_site", None)
    if not callable(asker):
        return None, ()
    try:
        answer = asker(hostapp + _HOSTAPP_SITE_PATH, admin_token)
    except Exception:  # noqa: BLE001 - a probe never raises; a fake might
        return None, ()
    if not isinstance(answer, HttpResult) or not answer.reached or answer.status != 200:
        return None, ()
    payload = _payload(answer)
    pending = payload.get("restart_pending")
    reasons = payload.get("restart_reasons")
    return (pending if type(pending) is bool else None,
            tuple(dict.fromkeys(reason for reason in reasons
                                if isinstance(reason, str) and SAFE_CODE_RE.fullmatch(reason)))
            if isinstance(reasons, list) else ())


# --------------------------------------------------------------------------- gates


def _payload(result: HttpResult) -> dict:
    return result.payload if type(result.payload) is dict else {}


def _has_token(ev: Evidence) -> bool:
    return ev.token.state == "configured"


def _sso_credential(ev: Evidence) -> str:
    """Which credential the probe logins were signed with: ``robot_token`` or ``none``."""

    return "robot_token" if _has_token(ev) else "none"


_SSO_CODE_MEANING = {
    "robot_unknown": "Pokamind has no robot with this token's robot_id",
    "robot_revoked": "this robot's token has been REVOKED in Pokamind",
    "invalid_assertion": "the signature, audience or lifetime was refused - check the "
                         "robot clock (NTP) and that the token was not rotated",
    "module_not_in_scope": "a module id outside this robot's scope was requested",
    "pathway_not_in_scope": "a pathway outside this robot's scope was requested",
    "course_not_in_scope": "the course_id claim is not this robot's course",
    "language_not_in_scope": "the kiosk language is not one this robot may use",
    "identity_not_in_scope": "the learner identity is not one this robot may log in as",
}


def _sso_rejection_hint(ev: Evidence, sso: SsoResult, *, badge: bool) -> str:
    seen = f" (body carried an '{sso.error_field}' field)" if sso.error_field else ""
    said = (f" - Pokamind said {sso.error_code}: {_SSO_CODE_MEANING[sso.error_code]}"
            if sso.error_code in _SSO_CODE_MEANING else "")
    lead = (f"SSO answered {sso.status}{seen}{said} for a token carrying the roster's "
            "course_id + pathway while the claim-less one was accepted"
            if badge else f"the SSO endpoint answered {sso.status}{seen}{said}")
    roster = (" - the claims this login added came from configs/roster.yaml (its "
              "course_id and pathway)" if badge else "")
    return (f"{lead}: this robot's own Pokamind token ({ev.token.masked}) signed it - "
            "run the pokamind_whoami gate (or the admin Pokamind tab's Test connection) "
            f"to see this robot's scope, and see {_TOKEN_DOC} for replacing a revoked or "
            f"rotated token{roster}")


def gate_hostapp(ev: Evidence) -> GateResult:
    if not ev.healthz.reached:
        return GateResult(
            "hostapp", FAIL, "hostapp_unreachable",
            f"start it: `sudo systemctl restart {_gateway_unit(ev)}` (deploy/robot/install.sh step 6)",
        )
    if ev.healthz.status != 200 or _payload(ev.healthz).get("status") != "ok":
        return GateResult(
            "hostapp", FAIL, "hostapp_unhealthy",
            f"/healthz answered but not status=ok: `journalctl -u {_gateway_unit(ev)} -n 100`",
        )
    return GateResult("hostapp", PASS, "ok")


def gate_demo_off(ev: Evidence) -> GateResult:
    if not ev.healthz.reached:
        return GateResult("demo_off", SKIP, "hostapp_unreachable")
    if _payload(ev.healthz).get("demo") is not False:
        if not _has_token(ev):
            return GateResult("demo_off", FAIL, "hostapp_in_demo_mode",
                              f"{_TOKEN_FIX} ({_TOKEN_DOC})")
        return GateResult(
            "demo_off", FAIL, "hostapp_in_demo_mode",
            "set HOSTAPP_DEMO=0 (or leave it empty) in /opt/emori/.env, then RESTART "
            f"{_gateway_unit(ev)} - it binds its token and HOSTAPP_DEMO only at process start, so "
            "a stale demo process cannot be re-pointed",
        )
    return GateResult("demo_off", PASS, "live")


def gate_robot_link(ev: Evidence) -> GateResult:
    if not ev.healthz.reached:
        return GateResult("robot_link", SKIP, "hostapp_unreachable")
    if _payload(ev.healthz).get("robot") is not True:
        return GateResult(
            "robot_link", FAIL, "hostapp_robot_link_down",
            "the hostapp's driver cannot reach REACHY_BASE_URL; fix the daemon gate, then "
            f"restart {_gateway_unit(ev)} so it re-reads REACHY_BASE_URL",
        )
    return GateResult("robot_link", PASS, "linked")


def _daemon_start_hint(ev: Evidence) -> str:
    """How to bring back what serves ``:8000`` -- which is not Pollen's daemon on v2.

    On a v2 robot the robot node owns ``:8000`` and Pollen's daemon is fenced off: the
    daemon's ``/api/daemon/start`` does not exist there, and starting the daemon unit
    would open the serial bus the node holds (deploy/robot/ros2_native/README.md).
    """

    if _port_8000_owner(ev) == "node":
        if ev.ros2_flavor == "v2":
            restart = ("`docker restart emori-ros2-v2`, then `docker logs emori-ros2-v2 | "
                       "grep 'REST shim'`")
        else:
            restart = ("`sudo systemctl restart reachy-mini-ros2` (native route; "
                       "`journalctl -u reachy-mini-ros2 -n 200`) or `docker restart "
                       "emori-ros2-v2` (Docker route)")
        return ("on this robot :8000 is the v2 robot node, not Pollen's daemon: restart "
                f"the node - {restart}; deploy/robot/ros2_native/README.md, 'When it will "
                "not start'. Do not start Pollen's daemon here: it would open the serial "
                "bus the node holds")
    return (f"POST {ev.reachy_base_url}/api/daemon/start?wake_up=true "
            "(v1.9.0 makes wake_up a REQUIRED query param), or "
            "`sudo systemctl restart reachy-mini-daemon`")


def gate_daemon(ev: Evidence) -> GateResult:
    start_hint = _daemon_start_hint(ev)
    if not ev.daemon.reached:
        return GateResult("daemon", FAIL, "daemon_unreachable", start_hint)
    if _payload(ev.daemon).get("state") != "running":
        return GateResult("daemon", FAIL, "daemon_not_running", start_hint)
    if _payload(ev.daemon).get("no_media") is not True:
        return GateResult(
            "daemon", FAIL, "daemon_owns_media",
            "run `deploy/robot/robot-mode.sh emori`: the daemon must come up --no-media or it "
            "holds the camera/mic away from the kiosk",
        )
    return GateResult("daemon", PASS, "running")


def gate_motors(ev: Evidence) -> GateResult:
    if not ev.daemon.reached:
        return GateResult("motors", SKIP, "daemon_unreachable")
    if not ev.motors.reached or ev.motors.status != 200:
        return GateResult(
            "motors", FAIL, "motors_status_unreachable",
            f"GET {ev.reachy_base_url}/api/motors/status did not answer 200; see the daemon gate",
        )
    if _payload(ev.motors).get("mode") != "enabled":
        return GateResult(
            "motors", FAIL, "motors_not_enabled",
            f"POST {ev.reachy_base_url}/api/motors/set_mode/enabled - motors boot 'disabled' "
            "after a power cycle and every goto returns 200 while the head stays limp",
        )
    return GateResult("motors", PASS, "enabled")


# --- daemon_routes: version skew between this code and what answers :8000 ------------
_SERVED, _MISSING, _UNSURE = "served", "missing", "unsure"


def load_route_manifest(path: Path | None = None) -> tuple[tuple[RouteNeed, ...], str]:
    """The routes this code needs from ``:8000``, as ``(needs, error)``.

    ``error`` is ``route_manifest_missing`` (no file: a broken install) or
    ``route_manifest_invalid`` (not the generator's shape), and ``needs`` is then
    empty: half a manifest would check half the routes and call that a PASS.
    """

    target = _ROUTE_MANIFEST if path is None else Path(path)
    try:
        raw = target.read_bytes()
    except OSError:
        return (), "route_manifest_missing"
    try:
        needs = tuple(_route_need(entry)
                      for entry in json.loads(raw.decode("utf-8"))["routes"])
    except (ValueError, KeyError, TypeError):   # UnicodeDecodeError is a ValueError
        return (), "route_manifest_invalid"
    return (needs, "") if needs else ((), "route_manifest_invalid")


def _route_need(entry: dict) -> RouteNeed:
    method, path, call = entry["method"], entry["path"], entry["call"]
    needed_on, fallback = entry["needed_on"], entry.get("fallback", "")
    if not (isinstance(method, str) and method in _ROUTE_METHODS
            and isinstance(path, str) and path.startswith("/")
            and isinstance(call, str) and call.startswith("/")
            and isinstance(needed_on, list) and needed_on
            and all(on in ("v1", "v2") for on in needed_on)
            and isinstance(fallback, str)):
        raise ValueError("route_manifest_invalid")
    return RouteNeed(method, path, call, v1="v1" in needed_on, v2="v2" in needed_on,
                     fallback=fallback)


def probe_routes(client, base_url: str, needs: tuple[RouteNeed, ...]) -> RouteProbe:
    """Ask the ``:8000`` at ``base_url`` which of ``needs`` it serves, running none.

    ``client`` is an httpx-style client. One ``GET /openapi.json``; then one ``OPTIONS``
    per needed route that table does not list (all of them when there is no table, none
    on a robot that has them all) -- which also catches a route hidden from the schema.
    An ``OPTIONS`` with no answer at all stops the rest: a wedged server costs one
    timeout, not one per route.
    """

    needs = tuple(needs)
    try:
        table = _openapi_routes(client.get(base_url + _OPENAPI_PATH))
    except Exception:  # noqa: BLE001 - any transport fault is "not reached"
        return RouteProbe(needs=needs)
    answers: list[tuple[str, int, str]] = []
    silent = False
    unlisted = (need.sample for need in needs
                if table is None or (need.method, need.shape) not in table)
    for sample in dict.fromkeys(unlisted):
        if not silent:
            try:
                answer = client.request("OPTIONS", base_url + sample)
                answers.append((sample, answer.status_code, answer.headers.get("allow", "")))
                continue
            except Exception:  # noqa: BLE001 - no answer is an answer: unsure
                silent = True
        answers.append((sample, 0, ""))
    return RouteProbe(needs=needs, reached=True,
                      openapi=tuple(sorted(table)) if table is not None else None,
                      options=tuple(answers))


def _openapi_routes(response) -> set[tuple[str, str]] | None:
    """``(METHOD, shape)`` of every operation an ``/openapi.json`` answer lists, or None."""

    if getattr(response, "status_code", 0) != 200:
        return None
    try:
        document = response.json()
    except Exception:  # noqa: BLE001 - not JSON: no table
        return None
    paths = document.get("paths") if isinstance(document, dict) else None
    if not isinstance(paths, dict):
        return None
    return {(method.upper(), _ROUTE_PARAM_RE.sub("{}", path))
            for path, operations in paths.items()
            if isinstance(path, str) and isinstance(operations, dict)
            for method in operations
            if isinstance(method, str) and method.upper() in _ROUTE_METHODS}


def _probe_daemon_routes(probe, base_url: str) -> RouteProbe:
    needs, error = load_route_manifest()
    if error:
        return RouteProbe(manifest_error=error)
    asker = getattr(probe, "daemon_routes", None)
    if not callable(asker):
        return RouteProbe(needs=needs)
    try:
        result = asker(base_url, needs)
    except Exception:  # noqa: BLE001 - a probe never raises; a fake might
        return RouteProbe(needs=needs)
    return result if isinstance(result, RouteProbe) else RouteProbe(needs=needs)


def needed_routes(needs, owner: str) -> tuple[RouteNeed, ...]:
    """The routes ``:8000`` must have, for whoever answers there (:func:`_port_8000_owner`):
    the v2 node, Pollen's daemon, or -- when it does not say -- only what both need."""

    if owner == "node":
        return tuple(need for need in needs if need.v2)
    if owner == "daemon":
        return tuple(need for need in needs if need.v1)
    return tuple(need for need in needs if need.v1 and need.v2)


def _route_verdict(routes: RouteProbe, need: RouteNeed) -> str:
    listed = routes.openapi is not None
    if listed and (need.method, need.shape) in routes.openapi:
        return _SERVED
    answer = next((a for a in routes.options if a[0] == need.sample), None)
    if answer is None or answer[1] == 0:
        return _UNSURE
    _, status, allow = answer
    if status == 405:
        # The path is routed. Starlette's Allow names the methods of the FIRST route on
        # it only, so a method missing there is proof only when the table agrees.
        allowed = {method.strip().upper() for method in allow.split(",") if method.strip()}
        if not allowed or need.method in allowed:
            return _SERVED
        return _MISSING if listed else _UNSURE
    if status == 404:
        # A converter may refuse the sample (a uuid parameter): then only the table says.
        return _MISSING if listed or not need.has_params else _UNSURE
    if 200 <= status < 300:
        return _SERVED
    return _UNSURE


def _route_list(needs) -> str:
    named = ", ".join(f"{need.method} {need.path}" for need in needs[:_ROUTES_NAMED])
    more = len(needs) - _ROUTES_NAMED
    return named + (f" and {more} more" if more > 0 else "")


def _route_missing_hint(ev: Evidence, owner: str, missing) -> str:
    routes = _route_list(missing)
    if owner == "node":
        if ev.ros2_flavor == "v2":
            ship = ("rebuild and ship the robot image `emori/ros2-jazzy-v2:robot` and "
                    "recreate emori-ros2-v2 (deploy/robot/ros2_native/README.md, Route A)")
        else:
            ship = ("Docker route: rebuild and ship `emori/ros2-jazzy-v2:robot` and recreate "
                    "emori-ros2-v2 (deploy/robot/ros2_native/README.md, Route A); native "
                    "route: re-run deploy/robot/ros2_native/install-native.sh (Route B), then "
                    "`sudo systemctl restart reachy-mini-ros2`")
        return (f"the v2 robot node's REST shim on :8000 does not serve {routes}: the node "
                "on this robot is older than the code deployed beside it, and every such "
                f"call answers 404. Ship the node again: {ship}; then re-run readycheck")
    if owner == "daemon":
        return (f"Pollen's reachy-mini daemon on :8000 does not serve {routes}: it is not "
                "the version this code calls, and every such call answers 404. Update "
                "reachy-mini in the daemon's venv (/venvs/mini_daemon on ReachyMiniOS) to a "
                "version that serves them, then `sudo systemctl restart reachy-mini-daemon`")
    return (f"whatever answers on :8000 does not serve {routes}, and it did not say whether "
            "it is Pollen's daemon or the v2 robot node: see the daemon gate first")


def gate_daemon_routes(ev: Evidence) -> GateResult:
    """Every route this code calls on ``:8000`` exists on the ``:8000`` that is RUNNING.

    CI (``tests/test_daemon_contract.py``) proves each call against the v2 shim's
    source. A robot runs the shim it was last SHIPPED (the ``emori/ros2-jazzy-v2:robot``
    image, the native pixi env) or the Pollen daemon its OS came with, and either can be
    older than the code: the call then answers 404 and nothing on the robot notices. The
    list is ``daemon_routes.json`` (generated from that same enumeration); on a v2 robot
    the Wi-Fi/volume routes emori-admin answers itself are not asked for, and on v1 the
    v2 node's own ``/api/emori/*`` are not.
    """

    routes = ev.daemon_routes
    if not ev.daemon.reached:
        return GateResult("daemon_routes", SKIP, "daemon_unreachable")
    if routes.manifest_error:
        return GateResult(
            "daemon_routes", FAIL, routes.manifest_error,
            f"{_ROUTE_MANIFEST.name} (beside readycheck.py) did not load, so nothing can "
            "say which routes this code needs: deploy again (it ships with the code); in a "
            f"checkout, regenerate it with `{_ROUTE_MANIFEST_COMMAND}`",
        )
    if not routes.needs:
        return GateResult("daemon_routes", SKIP, "routes_not_checked")
    if not routes.reached:
        return GateResult("daemon_routes", SKIP, "routes_not_probed")
    owner = _port_8000_owner(ev)
    missing: list[RouteNeed] = []
    healed: list[RouteNeed] = []
    unsure: list[RouteNeed] = []
    for need in needed_routes(routes.needs, owner):
        verdict = _route_verdict(routes, need)
        if verdict == _MISSING:
            (healed if owner == "daemon" and need.fallback else missing).append(need)
        elif verdict == _UNSURE:
            unsure.append(need)
    if missing:
        return GateResult("daemon_routes", FAIL, "daemon_route_missing",
                          _route_missing_hint(ev, owner, missing))
    if healed:
        backends = "/".join(sorted({need.fallback for need in healed}))
        return GateResult(
            "daemon_routes", WARN, "daemon_route_fallback",
            f"Pollen's reachy-mini daemon on :8000 does not serve {_route_list(healed)}; "
            f"emori-admin answers them itself ({backends}), so Wi-Fi and volume still work, "
            "but the daemon is not the version this code calls: update reachy-mini "
            "(/venvs/mini_daemon) and `sudo systemctl restart reachy-mini-daemon`",
        )
    if unsure:
        return GateResult(
            "daemon_routes", WARN, "daemon_routes_unverified",
            f"could not tell whether :8000 serves {_route_list(unsure)}: it publishes no "
            "/openapi.json, and an OPTIONS on the route answered neither 405 (served) nor "
            "a conclusive 404 (or nothing at all). Ask it by hand: `curl -si -X OPTIONS "
            "<REACHY_BASE_URL><path>` -- 405 means served, 404 missing",
        )
    return GateResult("daemon_routes", PASS,
                      "routes_served_v2" if owner == "node" else "routes_served")


def gate_camera_bridge(ev: Evidence) -> GateResult:
    if not ev.camera.available:
        return GateResult("camera_bridge", SKIP, "v4l2_ctl_unavailable")
    if ev.camera.returncode != 0:
        return GateResult(
            "camera_bridge", FAIL, "camera_node_missing",
            # BOTH nodes, matching install.sh step 5 verbatim. An operator who pastes a
            # one-node line here reloads the module without /dev/video41, and the next
            # `docker start emori-ros2-v2` fails container creation on a missing device.
            "`sudo modprobe v4l2loopback video_nr=40,41 exclusive_caps=1,1 "
            "card_label=ReachyCam,ReachyCamROS max_buffers=2` "
            "(deploy/robot/install.sh step 5)",
        )
    if _CAPTURE_FORMAT_RE.search(ev.camera.stdout) is None:
        return GateResult(
            "camera_bridge", FAIL, "camera_capture_not_advertised",
            "the loopback exists but advertises no capture format: "
            "`sudo systemctl restart emori-camera-bridge` (no producer is feeding it)",
        )
    return GateResult("camera_bridge", PASS, "capture_ready")


def gate_gaze(ev: Evidence) -> GateResult:
    """The v2 face look-at camera path, as far as a read-only host check can see it.

    Deliberately NOT a check that gaze works: this gate never shells into the container
    and never asks the node anything, because readycheck is documented read-only and
    cheap and runs at boot on a box at loadavg 4.7.  What it can prove from the host is
    the half that actually breaks -- that the second loopback exists and that something
    is feeding it -- and those are two different failures with two different fixes:

    * the node is missing entirely: the module was loaded with only video40, or a live
      `ros-loopback.sh` was never run.  The container will not even start, because
      ``devices:`` naming a missing node fails container CREATION;
    * the node exists but advertises no capture format: it was created, but
      EMORI_CAM_ROS_VIDEO_NR is unset in /etc/default/emori-camera so the bridge tees
      nothing into it.  The container starts, the node opens nothing, and the face
      topic is silently empty -- the failure with no symptom.

    Unconfigured is PASS ``gaze_lane_off``, exactly as the optional ROS lane is
    ``ros2_lane_off`` and for exactly the same reason: face look-at is a feature an
    operator ARMS (``EMORI_CAM_ROS_VIDEO_NR`` in /etc/default/emori-camera, which this
    check reads too; ``EMORI_READYCHECK_ROS_VIDEO_DEVICE`` only overrides the device),
    not a dependency that could not be probed.  Every v1 robot and every v2 robot that has
    not armed look-at is unconfigured, so a SKIP would make ``readycheck.sh --strict``
    exit 2 on a perfectly healthy robot -- which is the one thing --strict must never
    do, or nobody will trust it when it fires.
    """

    if not ev.gaze_video:
        return GateResult("gaze", PASS, "gaze_lane_off")
    if not ev.camera_ros.available:
        return GateResult("gaze", SKIP, "v4l2_ctl_unavailable")
    if ev.camera_ros.returncode != 0:
        return GateResult(
            "gaze", FAIL, "ros_camera_node_missing",
            f"`sudo /opt/emori/deploy/robot/ros-loopback.sh {ev.gaze_video.rsplit('video', 1)[-1]}` "
            "(and check camera-bridge.service's second ExecStartPre)",
        )
    if _CAPTURE_FORMAT_RE.search(ev.camera_ros.stdout) is None:
        return GateResult(
            "gaze", FAIL, "ros_camera_not_fed",
            f"the ROS loopback exists but nothing is feeding it: "
            f"EMORI_CAM_ROS_VIDEO_NR={ev.gaze_video.rsplit('video', 1)[-1]} in "
            "/etc/default/emori-camera, then `sudo systemctl restart emori-camera-bridge` "
            "(and `sudo systemctl restart emori-kiosk` right after, or the kiosk camera "
            "stays black)",
        )
    return GateResult("gaze", PASS, "ros_camera_ready")


def gate_tts(ev: Evidence) -> GateResult:
    if not ev.healthz.reached:
        return GateResult("tts", SKIP, "hostapp_unreachable")
    voices_hint = (
        "run `bash scripts/get_voice_models.sh` (or set PIPER_VOICE_CA/PIPER_VOICE_EN) and "
        f"restart {_gateway_unit(ev)}; the engine is built once at startup"
    )
    files = ", ".join(f"voices/{lang}.onnx" for lang in ev.voices_missing)
    missing = (f" (no Piper voice for {', '.join(ev.voices_missing)}: {files})"
               if ev.voices_missing else "")
    if not ev.tts.reached:
        return GateResult("tts", FAIL, "tts_probe_failed", voices_hint + missing)
    if ev.tts.status == 409:
        # The session owns the speaker and the route refuses every other voice until it ends, so
        # the engine was not asked. A deploy during a session runs this and the session goes on.
        return GateResult("tts", SKIP, "tts_session_active")
    if ev.tts.status == 503:
        return GateResult("tts", FAIL, "tts_unavailable", voices_hint + missing)
    if ev.tts.status != 200:
        return GateResult("tts", FAIL, "tts_error", voices_hint + missing)
    if "audio/" not in ev.tts.content_type:
        return GateResult(
            "tts", FAIL, "tts_not_audio",
            "/api/tts returned 200 without an audio body; check the Piper voice file itself",
        )
    if ev.voices_missing:
        # /api/tts answered: PiperTts reads a language it has no voice for with its FIRST
        # voice, so the robot still speaks -- in the wrong accent, which nobody on site
        # reports as a fault. install.sh warns and goes on when a download fails.
        return GateResult(
            "tts", WARN, "voice_missing",
            f"this robot speaks {', '.join(ev.voices_missing)} but has no whole Piper voice "
            f"for it ({files} + .onnx.json): the first installed voice reads that text. Once "
            "the robot is online: deploy again, or `cd /opt/emori && bash "
            "scripts/get_voice_models.sh` (a voice of another language as "
            f"`<lang>=<piper-voices path>`), then restart {_gateway_unit(ev)}",
        )
    return GateResult("tts", PASS, "warm")


def gate_stt(ev: Evidence) -> GateResult:
    if not ev.healthz.reached:
        return GateResult("stt", SKIP, "hostapp_unreachable")
    if not ev.stt.reached or ev.stt.status != 200:
        return GateResult(
            "stt", FAIL, "stt_probe_failed",
            "/api/stt/health did not answer 200; see the hostapp gate",
        )
    if _payload(ev.stt).get("available") is not True:
        return GateResult(
            "stt", FAIL, "stt_unavailable",
            "install the [stt] extra and the Vosk model (`bash scripts/get_voice_models.sh`, "
            f"VOSK_MODEL_CA), then restart {_gateway_unit(ev)}",
        )
    return GateResult("stt", PASS, "available")


def gate_pokamind(ev: Evidence) -> GateResult:
    # Any answer below 5xx proves the transport works; 401/404 are authorisation
    # and routing concerns, not reachability, and must not fail this gate.
    if not ev.pokamind.reached:
        return GateResult(
            "pokamind", FAIL, "pokamind_unreachable",
            f"no HTTP answer at {ev.pokamind_base}: start the reverse tunnel "
            "or fix POKAMIND_BASE",
        )
    if ev.pokamind.status >= 500:
        return GateResult(
            "pokamind", FAIL, "pokamind_server_error",
            "the link carries HTTP but Pokamind answered 5xx: the server behind "
            "POKAMIND_BASE is failing - retry, and check that POKAMIND_BASE is right",
        )
    return GateResult("pokamind", PASS, "reachable")


# How far this robot's clock may be from Pokamind's before Pokamind refuses what the
# robot signs. The server accepts an assertion whose iat is at most 2 s in its future
# and whose exp (iat + 45) is at most 2 s in its past -- every control-plane call,
# whoami included. The learner login is looser: iat up to 60 s ahead, exp (iat + 600)
# not yet past.
_CLOCK_AHEAD_LIMIT_S = 2.0
_CLOCK_BEHIND_LIMIT_S = 47.0
_SSO_AHEAD_LIMIT_S = 60.0
_SSO_BEHIND_LIMIT_S = 600.0
_NTP_FIX = ("`sudo timedatectl set-ntp true`, then `timedatectl timesync-status` - and "
            "check that this network lets NTP (UDP 123) out. A CM4 has no RTC battery: "
            "after every power-off its clock is wrong until NTP syncs")


def _skew_words(clock: ClockProbe) -> str:
    skew = clock.skew_s or 0.0
    side = "ahead of" if skew >= 0 else "behind"
    return f"{abs(skew):.1f} s {side} Pokamind's (+/-{clock.uncertainty_s:.1f} s)"


def gate_clock(ev: Evidence) -> GateResult:
    """This robot's clock, as Pokamind judges what the robot signs.

    Every control-plane call (whoami, an add-on's) carries an assertion Pokamind
    refuses when the robot's clock is more than 2 s ahead of its own (or 47 s behind),
    and a learner login one it refuses past 60 s ahead or 600 s behind. The skew is
    measured against the Date header of the pokamind probe, so it is only ever as good
    as one second plus half a round trip: a FAIL needs the WHOLE uncertainty range to be
    outside the window, never just the middle of it. An answer served from a cache (Age >
    0) or reached through a redirect is not measured at all (``clock_unmeasurable``).
    """

    clock = ev.clock
    if clock.skew_s is not None:
        low = clock.skew_s - clock.uncertainty_s
        high = clock.skew_s + clock.uncertainty_s
        sso = (low > _SSO_AHEAD_LIMIT_S) or (high < -_SSO_BEHIND_LIMIT_S)
        also = " - learner logins too" if sso else ""
        if low > _CLOCK_AHEAD_LIMIT_S:
            return GateResult(
                "clock", FAIL, "clock_ahead_of_pokamind",
                f"this robot's clock is {_skew_words(clock)}: Pokamind refuses every call "
                f"it signs (whoami, control calls{also}) once the clock is more than "
                f"{_CLOCK_AHEAD_LIMIT_S:.0f} s ahead - {_NTP_FIX}",
            )
        if ev.clock_strict_reason and low > 0:
            # An installed add-on signs a timestamp Pokamind refuses when it is in its
            # future -- no forward tolerance at all.
            return GateResult(
                "clock", FAIL, "clock_ahead_of_pokamind",
                f"this robot's clock is {_skew_words(clock)}: {ev.clock_strict_reason} - "
                f"{_NTP_FIX}",
            )
        if high < -_CLOCK_BEHIND_LIMIT_S:
            return GateResult(
                "clock", FAIL, "clock_behind_pokamind",
                f"this robot's clock is {_skew_words(clock)}: Pokamind refuses every call "
                f"it signs (whoami, control calls{also}) once the clock is more than "
                f"{_CLOCK_BEHIND_LIMIT_S:.0f} s behind - {_NTP_FIX}",
            )
        inside = -_CLOCK_AHEAD_LIMIT_S <= low and high <= _CLOCK_AHEAD_LIMIT_S
        if not inside and (clock.ntp != "yes" or high < -_CLOCK_AHEAD_LIMIT_S):
            # Not provably refused, not provably fine: a skew this size is one drift (or
            # one slow round trip) away from every signed call failing.
            return GateResult(
                "clock", WARN, "clock_skew",
                f"this robot's clock is {_skew_words(clock)}; Pokamind refuses signed calls "
                f"beyond {_CLOCK_AHEAD_LIMIT_S:.0f} s ahead or {_CLOCK_BEHIND_LIMIT_S:.0f} s "
                f"behind (NTP synchronized: {clock.ntp or 'unknown'}) - {_NTP_FIX}",
            )
    if clock.unmeasurable and clock.ntp != "yes":
        # Nothing vouches for the clock: no usable reading, and NTP does not say yes.
        # (NTP's yes is still a PASS below -- the Date was only ever the cross-check.)
        why = (f"Pokamind's answer {clock.unmeasurable}, so its Date header is not "
               "Pokamind's clock now and the skew was not measured")
        if clock.ntp == "no":
            return GateResult(
                "clock", WARN, "clock_unmeasurable",
                f"NTP has not synchronized this robot's clock, and {why}: the robot may "
                "be outside the 2 s Pokamind allows for signed calls - "
                f"{_NTP_FIX}",
            )
        return GateResult("clock", SKIP, "clock_unmeasurable",
                          f"{why}; NTP's state is unknown (no timedatectl)")
    if clock.ntp == "no":
        measured = (f" (now {_skew_words(clock)}, inside the limits)"
                    if clock.skew_s is not None else "")
        return GateResult(
            "clock", WARN, "ntp_unsynchronized",
            f"NTP has not synchronized this robot's clock{measured}: the next power-off or "
            "drift takes it outside the 2 s Pokamind allows for signed calls (a clock far "
            "enough off also breaks HTTPS, which reads as pokamind_unreachable) - "
            f"{_NTP_FIX}",
        )
    if clock.ntp == "yes":
        return GateResult("clock", PASS, "clock_ntp_synchronized")
    if clock.skew_s is not None:
        return GateResult("clock", PASS, "clock_within_tolerance")
    return GateResult("clock", SKIP, "clock_unprobed")


def gate_identity(ev: Evidence) -> GateResult:
    """WHICH robot this is: the board install.sh recorded, against the board it runs on.

    Every fresh Reachy Mini is hostname ``reachy-mini``; the CM4's serial is what tells
    them apart. A card moved into another robot body still holds the first robot's
    token, so this body logs in as that robot -- with its org, scope and history. Not
    a FAIL: the robot works, it may simply have been swapped on purpose.
    """

    ident = ev.identity
    if not ident.serial:
        return GateResult("identity", SKIP, "serial_unavailable")
    if not ident.recorded_serial:
        return GateResult("identity", SKIP, "identity_unrecorded")
    if ident.recorded_serial != ident.serial:
        signs_as = ev.token.robot_id or ident.recorded_robot_id or "?"
        return GateResult(
            "identity", WARN, "identity_card_moved",
            f"this install was recorded on board {ident.recorded_serial}, but it runs on "
            f"board {ident.serial}: the SD card is in another robot body, which logs in as "
            f"robot '{signs_as}'. Intended (a repaired or swapped body)? Deploy again: "
            "install.sh records the new board. If not, put the card back and give this "
            f"body its own token ({_TOKEN_DOC})",
        )
    return GateResult("identity", PASS, "identity_recorded")


def identity_line(ev: Evidence) -> str:
    """One line naming this robot, printed above the matrix. Nothing in it is secret."""

    ident = ev.identity
    parts = [f"serial {ident.serial or 'unknown'}"]
    if ident.wlan_mac:
        parts.append(f"Wi-Fi {ident.wlan_mac}")
    if ev.token.robot_id:
        parts.append(f"token for '{ev.token.robot_id}'")
    if ev.env_robot_id and ev.env_robot_id != ev.token.robot_id:
        parts.append(f"EMORI_ROBOT_ID={ev.env_robot_id}")
    return "robot: " + ", ".join(parts)


_TOKEN_ERROR_HINTS = {
    "token_empty": "the token file is empty",
    "token_malformed": "the value is not a pkr1.<robot_id>.<seed> token",
    "token_version_unsupported": "the value is a token of a version this robot cannot read",
    "robot_id_invalid": "the robot_id part is not a lowercase slug of 3-63 characters",
    "seed_invalid": "the key part is not 43 characters of base64url (one character lost "
                    "or added in a copy-paste looks exactly like this)",
    "token_file_unreadable": "the token file exists but this user cannot read it",
    "token_file_too_large": "the token file holds far more than one token",
}


def gate_pokamind_token(ev: Evidence) -> GateResult:
    """THIS robot's own Pokamind credential: present, well formed, and the one in force.

    Local facts only (the token file / env, ``EMORI_ROBOT_ID``, ``/healthz``); whether
    Pokamind accepts the token is ``pokamind_whoami``'s question. The token is the
    robot's ONLY Pokamind credential: without a usable one the kiosk serves demo
    sessions only, so a missing or unparsable token is a FAIL.
    """

    token = ev.token
    if token.state == "invalid":
        where = token.path if token.source == "file" else "POKAMIND_ROBOT_TOKEN"
        return GateResult(
            "pokamind_token", FAIL, "pokamind_token_invalid",
            f"{where}: {_TOKEN_ERROR_HINTS.get(token.error, 'the token is unusable')} "
            f"({token.error or 'invalid'}). The hostapp ignores it and serves demo "
            f"sessions only: {_TOKEN_FIX} ({_TOKEN_DOC})",
        )
    if token.state != "configured":
        return GateResult("pokamind_token", FAIL, "pokamind_token_missing",
                          f"{_TOKEN_FIX} ({_TOKEN_DOC})")
    if ev.env_robot_id and ev.env_robot_id != token.robot_id:
        return GateResult(
            "pokamind_token", FAIL, "robot_id_mismatch",
            f"EMORI_ROBOT_ID in .env is '{ev.env_robot_id}' but this robot's Pokamind token "
            f"is for '{token.robot_id}'. The token wins for every Pokamind call, so the "
            "robot logs in as the token's robot - if that is not this robot, the wrong "
            "token is installed. Fix whichever is wrong: replace the token, or SET "
            f"EMORI_ROBOT_ID={token.robot_id} (do not delete the line: with "
            "EMORI_DASH_SALT empty the dashboard's person codes derive from it - pin "
            f"that salt first, {_TOKEN_DOC} 'Upgrading a robot'), then restart {_gateway_unit(ev)}",
        )
    if ev.anonymous_is_robot is False:
        # An @emori.local pin cannot get here: under a token the hostapp replaces it with
        # the robot's own account (identity._anonymous_record). What is left is a shared
        # account in a staff domain, which Pokamind admits only when that domain is in
        # the robot's identity_domains and the account is a learner of its org.
        return GateResult(
            "pokamind_token", WARN, "anonymous_identity_not_this_robot",
            "configs/roster.yaml has an `anonymous:` block that makes drop-in sessions log "
            f"in as a staff-domain account instead of robot-{token.robot_id}@emori.local. "
            "Pokamind admits it only if that domain is in this robot's identity_domains and "
            "the account belongs to its org (else identity_not_in_scope). Delete the block "
            f"to use the robot's own account, then restart {_gateway_unit(ev)}",
        )
    running = _payload(ev.healthz) if ev.healthz.reached else {}
    in_force = running.get("pokamind_credential")
    if type(in_force) is str and in_force != "robot_token":
        return GateResult(
            "pokamind_token", WARN, "token_not_in_force",
            f"a token for '{token.robot_id}' is installed ({token.source}) but the RUNNING "
            f"hostapp still uses '{in_force}': it binds its credential at start - "
            f"`sudo systemctl restart {_gateway_unit(ev)}` (or Services > {_gateway_label(ev)} > Restart)",
        )
    # Token -> token (a rotation, or Replace token on a robot that already had one): the
    # kind is "robot_token" both times, so only the key says which one is signing. The
    # gates below prove the token ON DISK, so without this a robot whose restart did not
    # happen reads all green while Pokamind refuses every session it mints with the old
    # key. An older hostapp that does not report the field is not judged.
    running_key = running.get("pokamind_key_fingerprint")
    if (in_force == "robot_token" and type(running_key) is str and token.fingerprint
            and running_key != token.fingerprint):
        return GateResult(
            "pokamind_token", FAIL, "token_not_in_force",
            f"the token installed for '{token.robot_id}' ({token.source}, key "
            f"{token.fingerprint}) is not the one the RUNNING hostapp signs with (key "
            f"{running_key}): it binds its credential at start, and after a rotate Pokamind "
            f"refuses the old key - `sudo systemctl restart {_gateway_unit(ev)}` (or Services > "
            f"{_gateway_label(ev)} > Restart)",
        )
    if token.insecure_permissions:
        return GateResult(
            "pokamind_token", WARN, "token_file_permissions",
            f"{token.path} is readable by other users on this robot: "
            f"`chmod 600 {token.path} && chmod 700 {token.path.rsplit('/', 1)[0] or '.'}` "
            "(deploy/robot/install.sh does this on every full deploy)",
        )
    return GateResult("pokamind_token", PASS, "robot_token")


_WHOAMI_HINTS = {
    "robot_revoked": "this robot's token has been REVOKED in Pokamind: ask Pokamind to "
                     "reactivate it, or to issue a new one (rotate) and replace it",
    "robot_unknown": "Pokamind has no robot with this token's robot_id: the token was "
                     "never issued on this server (wrong POKAMIND_BASE?) or was deleted",
    "whoami_auth_failed": "Pokamind refused this robot's signature: the token was rotated "
                          "(an older copy is installed), the robot clock is off (NTP), "
                          "POKAMIND_BASE points at a different server than issued it, or "
                          "Pokamind's record of this robot's credential does not match "
                          "the token: ask Pokamind to check this robot's credential",
    "whoami_not_supported": "the server at POKAMIND_BASE predates per-robot tokens (no "
                            "/api/robot/whoami): point POKAMIND_BASE at a current Pokamind "
                            "server",
    "whoami_unreachable": "no answer from Pokamind itself: no connection, or something "
                          "between the robot and POKAMIND_BASE (a captive portal, a site "
                          "proxy, a firewall) answered 401/403 without Pokamind's JSON. "
                          "That says nothing about the token: fix the network path (see "
                          "the pokamind gate) and check again",
    "whoami_timeout": "POKAMIND_BASE did not answer in time: see the pokamind gate",
    "whoami_rate_limited": "Pokamind rate-limited the check: re-run in a minute",
    "whoami_unavailable": "the Pokamind control plane is unavailable (503): retry; if it "
                          "persists, report it to Pokamind",
    "whoami_server_error": "Pokamind answered 5xx: retry; if it persists, report it to "
                           "Pokamind",
    "whoami_rejected": "Pokamind rejected the request (4xx)",
    "invalid_whoami_response": "Pokamind answered something that is not the whoami "
                               "contract: the robot and the server disagree on the "
                               "version - update the robot, or report it to Pokamind",
    "whoami_robot_id_mismatch": "Pokamind answered about a DIFFERENT robot than the one "
                                "that signed: report this to Pokamind",
    "whoami_key_mismatch": "Pokamind's record holds a different key fingerprint than this "
                           "token: the token was rotated - install the current one",
    "whoami_base_url_invalid": "POKAMIND_BASE must be an https origin (plain http only for "
                               "localhost, the dev tunnel)",
    "whoami_signing_unavailable": "this robot cannot sign with its token: install the "
                                  "hostapp extra (PyJWT + cryptography) - re-run install.sh",
}


def _whoami_ok(ev: Evidence):
    """The parsed whoami when the live call succeeded, else None."""

    result = ev.whoami
    if result is None or not result.ok or result.whoami is None:
        return None
    return result.whoami


def gate_pokamind_whoami(ev: Evidence) -> GateResult:
    """Pokamind's own answer about THIS robot: active, owned by an org, in scope.

    Live, with the robot's own token (``GET /api/robot/whoami``). What it settles that
    nothing else on the robot can: the token
    is accepted and not revoked, the robot belongs to an org, the kiosk language and
    the course the sessions are minted with are inside the robot's scope (outside it
    every session is refused with a 403 before the learner sees anything), and every
    module in scope is actually startable.
    """

    if not _has_token(ev):
        # Nothing to sign with: pokamind_token owns that FAIL, one line per fault.
        return GateResult("pokamind_whoami", SKIP,
                          "pokamind_token_invalid" if ev.token.state == "invalid"
                          else "no_robot_token")
    if not ev.pokamind.reached:
        return GateResult("pokamind_whoami", SKIP, "pokamind_unreachable")
    result = ev.whoami
    if result is None:
        return GateResult("pokamind_whoami", SKIP, "whoami_not_probed")
    whoami = _whoami_ok(ev)
    if whoami is None:
        code = result.code if SAFE_CODE_RE.fullmatch(result.code or "") else "whoami_failed"
        status = f" (HTTP {result.http_status})" if result.http_status else ""
        return GateResult(
            "pokamind_whoami", FAIL, code,
            f"GET {ev.pokamind_base}/api/robot/whoami as '{ev.token.robot_id}'{status}: "
            + _WHOAMI_HINTS.get(code, "the check failed") + f" - {_TOKEN_DOC}",
        )
    if whoami.status != "active":
        return GateResult(
            "pokamind_whoami", FAIL, "robot_not_active",
            f"Pokamind reports '{whoami.robot_id}' as {whoami.status}: ask Pokamind to "
            "reactivate it or to issue a new token",
        )
    if not whoami.org:
        return GateResult(
            "pokamind_whoami", FAIL, "whoami_org_missing",
            f"'{whoami.robot_id}' belongs to no Pokamind org, so no learner can be scoped "
            "to it: ask Pokamind to assign the robot to an org",
        )
    kiosk = ev.lang.split("-")[0].split("_")[0].lower()
    if whoami.languages and kiosk not in whoami.languages:
        return GateResult(
            "pokamind_whoami", FAIL, "kiosk_language_not_in_scope",
            f"the kiosk speaks '{kiosk}' but '{whoami.robot_id}' may only run "
            f"{'/'.join(whoami.languages)} sessions: every login is refused "
            "(language_not_in_scope). Switch the kiosk language (admin Language tab) or "
            "add the language to the robot's scope in Pokamind",
        )
    if whoami.course_id:
        # The EFFECTIVE course of this process: explicit, saved on the admin page, or
        # derived from this robot's Pokamind record -- never a guess at the .env line.
        wrong, fixes = [], []
        if ev.course_id_env and ev.course_id_env != whoami.course_id:
            origin, fix = _course_origin(ev, whoami.course_id)
            wrong.append(f"EMORI_COURSE_ID (voice menu) is '{ev.course_id_env}'{origin}")
            fixes.append(fix)
        # A roster without a course of its own follows EMORI_COURSE_ID: named only when
        # it pins a different one itself.
        if (ev.badge_course_id and ev.badge_course_id != whoami.course_id
                and ev.badge_course_id != ev.course_id_env):
            wrong.append("configs/roster.yaml course_id (badges, drop-ins) is "
                         f"'{ev.badge_course_id}'")
            fixes.append("delete the roster's own `course_id:` line from "
                         "configs/roster.yaml (it then follows the robot's course)")
        if wrong:
            fix = "; ".join(fixes)
            return GateResult(
                "pokamind_whoami", FAIL, "course_id_not_in_scope",
                f"'{whoami.robot_id}' is scoped to course '{whoami.course_id}' but "
                f"{'; '.join(wrong)}: those sessions are refused (course_not_in_scope). "
                f"{fix[:1].upper()}{fix[1:]}; then the admin page's Settings -> Apply now "
                "(the host app restarts when idle)",
            )
    problems = whoami.problems()
    if problems:
        listing = ", ".join(f"{m.module_id} ({m.problem or 'unavailable'})" for m in problems)
        return GateResult(
            "pokamind_whoami", WARN, "whoami_module_problem",
            f"modules in '{whoami.robot_id}''s scope that cannot be started: {listing}. "
            "voice_menu_in_scope says whether the spoken menu offers any of them; fix the "
            "module (publish it / grant the partner / language) or drop it from the scope",
        )
    return GateResult("pokamind_whoami", PASS, "whoami_active")


def _course_origin(ev: Evidence, course: str) -> tuple[str, str]:
    """``(where EMORI_COURSE_ID came from, how to fix it)``, for the course hint.

    Read off the settings layer's own record (site_config), so the remedy is the one
    that applies: an admin-saved or .env value is cleared on the Settings tab, a value
    derived from an older sync is refreshed by syncing.
    """

    source = dict(ev.site.sources).get("EMORI_COURSE_ID")
    setting = f"set EMORI_COURSE_ID to '{course}' on the admin page's Settings tab"
    if source == _site_settings.SOURCE_ADMIN:
        # Clearing an admin-saved value hands the course back to .env / the token.
        return (" (saved on the admin page)",
                "on the admin page's Settings tab clear EMORI_COURSE_ID so the course "
                f"follows this robot's token, or set it to '{course}'")
    if source == _site_settings.SOURCE_ENV:
        # An admin-saved value outranks .env; only emptying the .env line lets the
        # token decide.
        return (" (set explicitly, e.g. in /opt/emori/.env)",
                f"{setting}, or empty the EMORI_COURSE_ID= line in /opt/emori/.env so the "
                "course follows this robot's token")
    if source == _site_settings.SOURCE_POKAMIND:
        return (" (derived from this robot's last Pokamind sync, older than its record)",
                "admin page Pokamind tab -> Sync now")
    if ev.site.probed:
        return (" (nothing sets it: the code default)",
                "let this robot sync its Pokamind scope (see the site_config gate) so the "
                f"course is derived, or {setting}")
    return "", setting


def _menu_scope_verdict(ev: Evidence) -> GateResult | None:
    """Every offered module id inside the robot's scope AND available, or why not.

    None when this cannot be judged (no token, no whoami, no menu).
    """

    whoami = _whoami_ok(ev)
    if whoami is None or not ev.menu.loaded or not ev.menu.options:
        return None
    outside = [(key, module_id) for key, module_id in ev.menu.options
               if whoami.module(module_id) is None]
    if outside:
        return GateResult(
            "voice_menu_in_scope", FAIL, "voice_menu_module_not_in_scope",
            f"the spoken menu offers modules outside '{whoami.robot_id}''s scope "
            f"({_menu_listing(outside)}): "
            "Pokamind refuses those sessions (module_not_in_scope). Ask Pokamind to add "
            f"them to the robot's scope, or fix the menu: {_menu_fix(ev)}",
        )
    dead = [(key, module_id, whoami.module(module_id)) for key, module_id in ev.menu.options]
    dead = [(key, module_id, m) for key, module_id, m in dead
            if m is not None and (not m.available or m.problem is not None)]
    if dead:
        listing = ", ".join(f"say '{key}' -> module {module_id} ({m.problem or 'unavailable'})"
                            for key, module_id, m in dead)
        return GateResult(
            "voice_menu_in_scope", FAIL, "voice_menu_module_unavailable",
            f"the spoken menu offers modules Pokamind cannot start ({listing}): a visitor "
            "who picks one gets a robot that never starts. Publish/grant the module, or "
            f"drop it from the menu: {_menu_fix(ev)}",
        )
    return GateResult("voice_menu_in_scope", PASS, "menu_in_scope")


def gate_voice_menu_in_scope(ev: Evidence) -> GateResult:
    """Every option the spoken menu offers is in this robot's scope and startable.

    The automatic menu check: whether the menu was built from this robot's Pokamind
    record, saved on the admin page or set by an override, its module ids are checked
    against what Pokamind says THIS robot may start NOW, with the robot's own
    credential -- a built menu is only as fresh as the last sync. A robot without a
    token SKIPs here: pokamind_token owns that FAIL.
    """

    if not _has_token(ev):
        return GateResult("voice_menu_in_scope", SKIP, "no_robot_token")
    if _whoami_ok(ev) is None:
        # pokamind_whoami owns that failure; one line per fault.
        return GateResult("voice_menu_in_scope", SKIP, "whoami_unavailable")
    if not ev.menu.loaded or not ev.menu.options:
        return GateResult("voice_menu_in_scope", SKIP, "voice_menu_unavailable")
    verdict = _menu_scope_verdict(ev)
    if verdict is not None and verdict.status == PASS:
        return _modules_not_offered(ev) or verdict
    return verdict or GateResult("voice_menu_in_scope", SKIP, "voice_menu_unavailable")


def _modules_not_offered(ev: Evidence) -> GateResult | None:
    """A WARN when the menu built from the record leaves some of its modules out.

    Only for that menu (a saved or overridden menu is somebody's choice): modules past
    the sixth of the kiosk language (the number words stop at six), and modules the
    record gives no language (they fit no menu). Counts only, never an id.
    """

    whoami = _whoami_ok(ev)
    if whoami is None or ev.menu.tier != "auto":
        return None
    try:
        from emori_adapter.hostapp import menu_builder  # noqa: PLC0415

        drafts = menu_builder.build_drafts(whoami.to_dict(), languages=(ev.lang,))
    except Exception:  # noqa: BLE001 - an advisory never breaks the run
        return None
    skipped = (drafts.get(ev.lang) or {}).get("skipped") or []
    over = sum(1 for row in skipped if row.get("reason") == menu_builder.SKIP_OVER_LIMIT)
    no_language = sum(1 for row in skipped
                      if row.get("reason") == menu_builder.SKIP_NO_LANGUAGE)
    if not over and not no_language:
        return None
    parts = []
    if over:
        parts.append(f"{over} module(s) past the sixth in '{ev.lang}' (a visitor chooses "
                     "by saying one to six)")
    if no_language:
        parts.append(f"{no_language} module(s) with no language in the record")
    return GateResult(
        "voice_menu_in_scope", WARN, "voice_menu_modules_not_offered",
        "the spoken menu built from this robot's Pokamind record leaves out "
        + " and ".join(parts) + ": ask Pokamind to trim or fix the record, or save a "
        "menu of this robot's own on the admin page's Menu tab")


_SITE_FILES = (f"{_site_settings.SETTINGS_FILE}, {_site_settings.SITE_SECRETS_FILE} or "
               f"{_site_settings.ADMIN_SECRETS_FILE}")


def _site_findings(ev: Evidence) -> list[tuple[str, str, str]]:
    """Every site_config finding as ``(status, code, hint)``, worst first."""

    site = ev.site
    sources = dict(site.sources)
    found: list[tuple[str, str, str]] = []

    # POKAMIND_BASE itself: every Pokamind call (whoami, SSO, any add-on's) refuses a
    # base that is not an https origin. Never quoted: a malformed one may carry userinfo.
    problem = _site_settings.validate("POKAMIND_BASE", ev.pokamind_base)
    if problem:
        source = sources.get("POKAMIND_BASE")
        where = {"admin": "saved on the admin page",
                 "env": "set in /opt/emori/.env"}.get(source or "", "as configured")
        found.append((FAIL, "pokamind_base_invalid",
                      f"POKAMIND_BASE ({where}) is not an https origin ({problem}; plain "
                      "http only for localhost, 127.0.0.1 or ::1, and no user:password@, "
                      "query or fragment), so every Pokamind call refuses it: fix it on the "
                      "admin page (Settings -> Server) or in /opt/emori/.env, or empty it to "
                      f"use the shipped default {_site_settings.DEFAULT_POKAMIND_BASE}"))

    # A value saved on this robot that its own check refuses is NOT in force: .env's
    # value (or the code default) is used instead. Named, never quoted.
    if site.invalid:
        listing = ", ".join(f"{key} ({slug})" for key, slug in site.invalid)
        found.append((FAIL, "site_settings_invalid",
                      f"saved on this robot but refused, so NOT in force: {listing}. Save "
                      "it again on the admin page (Settings), or delete the line from "
                      f"{_SITE_FILES} under /opt/emori (key_wrong_file: the key is in a file "
                      f"that is not its own) - {_SETTINGS_DOC}"))

    # The Pokamind scope this robot derives its settings from -- only where it is used:
    # a token is in force and EMORI_SITE_AUTO is on.
    status = site.scope_status
    if _has_token(ev) and site.auto and status:
        robot = ev.token.robot_id
        if status in ("scope_robot_mismatch", "scope_fingerprint_mismatch"):
            if status == "scope_fingerprint_mismatch":
                whose = "another token of this robot (an older key)"
            elif site.scope_robot_id and site.scope_robot_id != robot:
                whose = f"robot '{site.scope_robot_id}'"
            else:
                whose = "another robot"
            found.append((WARN, "scope_robot_mismatch",
                          f"the Pokamind scope on disk belongs to {whose} ({status}), not "
                          f"to the token in force for '{robot}': it is ignored, so nothing "
                          "is derived from Pokamind (course, pathways, name, language, the "
                          "spoken menu). Admin page Pokamind tab -> Sync now: the first "
                          "successful sync replaces it"))
        elif status != "ok":
            found.append((WARN, "scope_not_synced",
                          f"this robot has its own token ('{robot}') but no usable Pokamind "
                          f"scope on disk ({status}), so nothing is derived from its "
                          "Pokamind record (course, pathways, name, language, the spoken "
                          "menu): admin page Pokamind tab -> Sync now, and check that the "
                          "robot reaches POKAMIND_BASE (the host app syncs 10 s after it "
                          "starts, then every 6 h)"))
    # The partner is a protocol constant: only an EXPLICIT EMORI_PARTNER_NAME can drift
    # from the record (Pokamind's live answer, else the last verified one).
    record = _whoami_ok(ev)
    partner = record.partner if record is not None else site.scope_partner
    if ev.partner_env and partner and ev.partner_env != partner:
        named = (f"'{ev.partner_env}'" if _PARTNER_RE.fullmatch(ev.partner_env)
                 else "another value")
        theirs = f"'{partner}'" if _PARTNER_RE.fullmatch(partner) else "another one"
        found.append((WARN, "partner_mismatch",
                      f"EMORI_PARTNER_NAME is set to {named} but this robot's Pokamind "
                      f"record says partner {theirs}: Pokamind refuses a login under "
                      "any other partner. Delete the EMORI_PARTNER_NAME line from "
                      "/opt/emori/.env (the partner is a protocol constant, not a setting), "
                      f"then restart {_gateway_unit(ev)}"))
    if (_has_token(ev) and site.auto and status == "ok"
            and site.scope_fetched_at is not None and site.now
            and site.now - site.scope_fetched_at > _site_settings.SCOPE_STALE_S):
        days = int((site.now - site.scope_fetched_at) // 86400)
        found.append((WARN, "scope_stale",
                      f"the last verified Pokamind scope is {days} days old (the limit is "
                      f"{_site_settings.SCOPE_STALE_S // 86400} days): the robot keeps working "
                      "from it, but nothing changed in Pokamind since has reached it. "
                      "Admin page Pokamind tab -> Sync now; see the pokamind_whoami gate"))
    if ev.smtp_sender_missing:
        # results_email has no built-in sender: with a host but no From, nothing is sent.
        found.append((WARN, "smtp_from_unset",
                      "EMORI_SMTP_HOST is set but there is no sender address "
                      "(EMORI_SMTP_FROM, or an EMORI_SMTP_USER that is an e-mail address): "
                      "no results e-mail is sent, each report waits in artifacts/outbox/. "
                      "Set the sender on the admin page (Settings -> Results e-mail), then "
                      "Send test e-mail"))
    if site.restart_pending is True:
        reasons = site.restart_reasons or ("unspecified",)
        manual = [reason for reason in reasons if reason not in _AUTO_APPLIED_REASONS]
        when = ("the admin page's Settings -> Apply now puts them in force (saved by an "
                "operator, so they wait for it)" if manual else
                "emori-admin restarts the host app by itself after 10 idle minutes, or "
                "Settings -> Apply now")
        found.append((WARN, "site_restart_pending",
                      f"the running host app has changes waiting for its next restart "
                      f"({', '.join(reasons)}): {when} - never during a session"))
    return found


def _site_detail(ev: Evidence) -> str:
    """``KEY=source`` for every robot-local setting with a value, and the scope's state."""

    site = ev.site
    pairs = " ".join(f"{key}={source}" for key, source in site.sources)
    if not site.auto:
        scope = "Pokamind derivation off (EMORI_SITE_AUTO=0)"
    elif not _has_token(ev):
        scope = "no robot token: nothing is derived from Pokamind"
    else:
        scope = f"Pokamind scope {site.scope_status or 'not checked'}"
        if site.scope_status == "ok" and site.scope_contract is not None:
            scope += f", contract {site.scope_contract}"
        if site.scope_ignored:
            scope += f", ignored as malformed: {', '.join(site.scope_ignored)}"
    return f"{pairs or 'no robot-local setting has a value, so code defaults apply'} ({scope})"


def gate_site_config(ev: Evidence) -> GateResult:
    """The robot-local settings layer: every value usable, and where each one came from.

    A robot from the public tree configures itself: admin-saved values, then ``.env``,
    then this robot's Pokamind record (the scope snapshot), then its hardware profile,
    then the shipped defaults (``site_settings``). Read in THIS process -- readycheck
    re-ran the layer when it started, so a value inherited from whoever started it is
    never mistaken for a ``.env`` value. FAIL when a saved value is refused or the API
    origin is unusable; WARN when the derivation is stale, missing, foreign, or waiting
    for a restart; PASS lists ``KEY=source`` pairs (names, never values).
    """

    if not ev.site.probed:
        return GateResult("site_config", SKIP, "site_config_not_probed")
    found = _site_findings(ev)
    if found:
        worst = next((f for f in found if f[0] == FAIL), found[0])
        others = [code for _status, code, _hint in found if code != worst[1]]
        also = f" (also: {', '.join(dict.fromkeys(others))})" if others else ""
        return GateResult("site_config", worst[0], worst[1], worst[2] + also)
    return GateResult("site_config", PASS, "site_config_ok", _site_detail(ev))


def gate_session_chain(ev: Evidence) -> GateResult:
    """Walk the link the kiosk walks: mint -> SSO 302 -> the frontend it names.

    A break anywhere in this chain leaves the same symptom: a robot that says
    nothing.  So each link fails under its OWN code - the matrix line
    alone must be enough to know which machine to go and fix.
    """

    if _sso_credential(ev) == "none":
        # No token = a demo robot; demo_off and pokamind_token already report that, and
        # there is nothing to sign with.  Reporting it again would bury the one line
        # that matters.
        return GateResult("session_chain", SKIP, "no_robot_token")
    if not ev.pokamind.reached:
        # One dead transport, one failure line - the pokamind gate owns it.
        return GateResult("session_chain", SKIP, "pokamind_unreachable")
    sso_url = f"{ev.pokamind_base}/partner/sso/login"
    if not ev.sso.reached:
        return GateResult(
            "session_chain", FAIL, "sso_unreachable",
            f"no HTTP answer from {sso_url}: the server is down or POKAMIND_BASE/the "
            "reverse tunnel points somewhere else",
        )
    if ev.sso.status >= 500:
        return GateResult(
            "session_chain", FAIL, "sso_server_error",
            f"{sso_url} answered {ev.sso.status}: check the server log: the partner may "
            "not be provisioned (ask Pokamind)",
        )
    if ev.sso.status >= 400:
        # Content-free by construction: the status, the NAME of the error key, and
        # Pokamind's own per-robot scope code when it sent one of the fixed set.
        return GateResult("session_chain", FAIL, "sso_token_rejected",
                          _sso_rejection_hint(ev, ev.sso, badge=False))
    if ev.sso.status not in _REDIRECT_STATUSES:
        return GateResult(
            "session_chain", FAIL, "sso_not_a_redirect",
            f"{sso_url} answered {ev.sso.status} instead of a 302 to the app; the "
            "kiosk has nothing to navigate to",
        )
    origin = _origin(ev.sso.location)
    if not origin:
        return GateResult(
            "session_chain", FAIL, "sso_location_missing",
            "the 302 carried no absolute http(s) Location: the server's app origin "
            "is unset or relative, so the kiosk cannot navigate (ask Pokamind)",
        )
    if not urlsplit(ev.sso.location).path.startswith(_CALLBACK_PATH_PREFIX):
        return GateResult(
            "session_chain", FAIL, "sso_callback_path_unexpected",
            f"the 302 pointed at {origin} but not at {_CALLBACK_PATH_PREFIX}<partner>; "
            "the server's app origin is aimed at the wrong app (ask Pokamind)",
        )
    if not _has_kiosk_flag(ev.sso.location):
        return GateResult(
            "session_chain", FAIL, "sso_kiosk_flag_missing",
            "the redirect carries no emoriKiosk=1: ask Pokamind to enable kiosk mode for "
            "this partner - without the flag the screenless kiosk never gets past the "
            "device-check page and the robot waits forever",
        )
    if not ev.frontend.reached:
        return GateResult(
            "session_chain", FAIL, "frontend_unreachable",
            f"Pokamind redirects to {origin} but nothing answers there: the app host is "
            "down or unreachable from this network (on a dev setup, start the app or fix "
            "its tunnel) - this is the single most common cause of a robot that goes "
            "quiet after the badge scan",
        )
    if ev.frontend.status != 200:
        return GateResult(
            "session_chain", FAIL, "frontend_error",
            f"{origin}/ answered {ev.frontend.status}: the kiosk iframe will load that "
            "error page instead of a session",
        )
    return GateResult("session_chain", PASS, "chain_ok")


def gate_badge_session(ev: Evidence) -> GateResult:
    """Walk the chain a REGISTERED BADGE walks, claims and all.

    The session_chain probe deliberately sends no course/pathway claims, so it stays
    green on a robot where every badge is dead: a badge whose pathway matched no
    module left the robot stuck with its antennas open while every other gate passed.

    What this gate can prove is bounded, and it says so: SSO accepting a badge-shaped
    token means the claims are well formed and the partner is known.  Whether the
    pathway matches a real module category is decided in the app's module filter,
    which no server-side probe can see -- so the remediation always names the roster.
    """

    if _sso_credential(ev) == "none":
        return GateResult("badge_session", SKIP, "no_robot_token")
    if not ev.pokamind.reached:
        return GateResult("badge_session", SKIP, "pokamind_unreachable")
    if not ev.badge_pathway:
        # No pathway pinned = every badge sends what the anonymous drop-in sends, which
        # session_chain already walked. That is the SAFE roster shape, not a gap.
        return GateResult("badge_session", SKIP, "no_roster_pathway")
    if not ev.badge_sso.reached:
        return GateResult(
            "badge_session", FAIL, "badge_sso_unreachable",
            "the badge login got no HTTP answer while the anonymous one did: re-run "
            "after the pokamind/session_chain gates are green",
        )
    if ev.badge_sso.status >= 400:
        return GateResult("badge_session", FAIL, "badge_sso_rejected",
                          _sso_rejection_hint(ev, ev.badge_sso, badge=True))
    if ev.badge_sso.status not in _REDIRECT_STATUSES:
        return GateResult(
            "badge_session", FAIL, "badge_sso_not_a_redirect",
            f"the badge login answered {ev.badge_sso.status} instead of a 302; the kiosk "
            "has nothing to navigate to after a badge scan",
        )
    if not _has_kiosk_flag(ev.badge_sso.location):
        return GateResult(
            "badge_session", FAIL, "badge_kiosk_flag_missing",
            "the badge redirect carries no emoriKiosk=1: ask Pokamind to enable kiosk "
            "mode for this partner",
        )
    return GateResult("badge_session", PASS, "badge_claims_accepted")


def _menu_listing(options) -> str:
    """``say '<key>' -> module <id>`` for each ``(key, module_id)`` pair."""

    return ", ".join(f"say '{key}' -> module {module_id}" for key, module_id in options)


# How a robot gets the menu that belongs to it, whatever it is speaking now.
_MENU_REMEDY = (
    "with its own Pokamind token the robot builds its menu from its Pokamind record "
    "(admin page: Pokamind tab -> Sync now), or save one on the admin page's Menu tab; a "
    f"robot without a token: {_TOKEN_FIX}"
)


def _menu_fix(ev: Evidence) -> str:
    """Where THIS robot's served menu is fixed: it depends on the tier it comes from."""

    menu = ev.menu
    if menu.override_env or menu.tier == "env":
        return (f"the file {menu.override_env or 'EMORI_VOICE_TOPICS'} names "
                f"({menu.source or '?'})")
    if menu.tier == "site":
        return ("the menu saved on the admin page's Menu tab (edit it there, or Reset to "
                "Pokamind)")
    if menu.tier == "auto":
        return ("this menu is built from the robot's Pokamind record, so fix the module "
                "in Pokamind, then Pokamind tab -> Sync now")
    if menu.tier == "prod":
        return (f"{menu.source} (installed before this release; a menu saved on the Menu "
                "tab outranks it)")
    if menu.tier in ("dev", "builtin") or not menu.source:
        return _MENU_REMEDY
    return menu.source


def _menu_pass_code(ev: Evidence, default: str) -> str:
    """The PASS code names where a verified menu comes from when that is this robot's."""

    return {"site": "voice_menu_site",
            "auto": "voice_menu_from_pokamind"}.get(ev.menu.tier, default)


def _placeholder_ids(ev: Evidence) -> list[tuple[str, str]]:
    return [(key, module_id) for key, module_id in ev.menu.options
            if module_id.startswith(_PLACEHOLDER_ID_PREFIXES)]


def _gate_menu_provenance(ev: Evidence) -> GateResult | None:
    """Is the robot speaking a menu that belongs to it?  None when it is.

    A menu saved on the admin page (``site``) or built from this robot's Pokamind
    record (``auto``) is this robot's own; ``prod`` is a menu a fork ships; ``dev`` (the
    simulator's development menus) and ``builtin`` never are: their module ids are
    placeholders no Pokamind record carries.
    """

    menu = ev.menu
    if not menu.source:
        return GateResult(
            "voice_menu", FAIL, "voice_menu_built_in_default",
            "no menu file resolved at all, so the robot is offering the BUILT-IN "
            f"default menu, whose module ids are placeholders: {_MENU_REMEDY}",
        )
    if menu.source.endswith("voice_topics.example.yaml"):
        return GateResult(
            "voice_menu", FAIL, "voice_menu_example_file",
            "the active menu is the shipped EXAMPLE (configs/voice_topics.example.yaml), "
            f"which carries placeholder module ids: {_MENU_REMEDY}",
        )
    placeholders = _placeholder_ids(ev)
    if placeholders:
        # Ids no Pokamind record carries: whatever file they came from, a visitor who
        # says that number gets a robot that never starts.
        if menu.override_env or menu.tier == "env":
            fix = (f"point {menu.override_env or 'EMORI_VOICE_TOPICS'} at a real menu, "
                   "or unset it")
        elif menu.tier == "site":
            fix = f"Menu tab -> Reset to Pokamind (or remove {menu.source})"
        elif menu.tier == "auto":
            fix = "Pokamind tab -> Sync now rebuilds it from the record"
        else:
            fix = f"remove {menu.source}; {_MENU_REMEDY}"
        return GateResult(
            "voice_menu", FAIL, "voice_menu_placeholder_ids",
            f"the spoken menu ({menu.source}) offers placeholder module ids no Pokamind "
            f"record carries ({_menu_listing(placeholders)}): every such pick fails. {fix}",
        )
    if menu.override_env:
        # An env var is a deliberate, visible operator decision and the one menu
        # choice a deploy cannot overwrite (.env is not tracked), so pointing away
        # from the robot's own menu is not by itself a defect - it is how a site runs
        # its own menu, or a robot is run while its menu is known-bad.  Say so; let
        # voice_menu_in_scope judge the ids.
        if menu.prod_digest and menu.digest and menu.digest != menu.prod_digest:
            return GateResult(
                "voice_menu", WARN, "voice_menu_override_differs_from_prod",
                f"{menu.override_env}={menu.source} overrides the fork's menu "
                f"({menu.prod_source}) and the two files differ; that is a deliberate "
                "override, but nothing regenerates it - voice_menu_in_scope checks its "
                "module ids against this robot's Pokamind scope",
            )
        return None
    if menu.tier == "site":
        # Saved on the admin page (or adopted from an older install's menu): this
        # robot's own, outranking every shipped menu by design. Not a shadow.
        return None
    if menu.tier == "auto":
        if menu.authored in _MENU_DRAFT_AUTHORED:
            some = "some of " if menu.authored == "mixed" else ""
            return GateResult(
                "voice_menu", WARN, "voice_menu_auto_draft",
                f"the spoken menu ({menu.source}) was built from this robot's Pokamind "
                f"record, but {some}its options are named after the modules' screen "
                "titles (authored "
                f"'{menu.authored}'): Pokamind holds no spoken name or keywords for them. "
                "A visitor can still pick by number; a name may not be recognised. Ask "
                "Pokamind to author spoken names and keywords for these modules, or edit "
                "them on the admin page's Menu tab (Save makes it this robot's own menu)",
            )
        return None
    if not menu.prod_source:
        return GateResult(
            "voice_menu", FAIL, "voice_menu_no_prod_menu",
            f"this robot is pointed at {ev.pokamind_base} but has no menu of its own for "
            f"lang={ev.lang or '?'} (none saved on the admin page, none built from its "
            "Pokamind record, no configs/prod/voice_topics"
            f"{'' if ev.lang == 'en' else '.' + (ev.lang or 'xx')}.yaml shipped by a "
            f"fork), so it fell back to {menu.source}: {_MENU_REMEDY}; or set "
            f"EMORI_VOICE_TOPICS_{(ev.lang or 'xx').upper()} to the file you mean",
        )
    if menu.source != menu.prod_source and menu.digest != menu.prod_digest:
        # Reachable only if the loader's precedence has been changed or a file was
        # dropped in front of configs/prod: keep the check anyway, because "the live
        # menu is not the generated one" is the single fact that catches a shadowed
        # or clobbered menu on its own.
        return GateResult(
            "voice_menu", FAIL, "voice_menu_not_the_prod_menu",
            f"the live menu is {menu.source}, not the generated {menu.prod_source}, and "
            "the two differ: a file-level deploy (sudo tar -x / git archive / rsync) has "
            "shadowed the production menu. Remove the shadowing file and restart "
            f"{_gateway_unit(ev)}",
        )
    return None


def _unverified_hint(ev: Evidence) -> str:
    menu = ev.menu
    if menu.tier == "auto":
        return (f"the menu built from this robot's last Pokamind sync ({menu.source}) "
                "offers modules Pokamind's live answer does not cover (see "
                "voice_menu_in_scope): Pokamind tab -> Sync now rebuilds it from the "
                "current record")
    what = {
        "site": "the menu saved on this robot (admin Menu tab)",
        "env": f"the file {menu.override_env or 'EMORI_VOICE_TOPICS'} names",
    }.get(menu.tier, "the fork's menu file")
    if not _has_token(ev):
        return (f"the live menu ({menu.source}) is {what}, and nothing has checked its "
                f"module ids: only Pokamind can, asked with this robot's token - {_TOKEN_FIX}")
    if _whoami_ok(ev) is not None:
        return (f"the live menu ({menu.source}) is {what}, and Pokamind's answer does not "
                "cover every option it offers: voice_menu_in_scope names them and the fix")
    return (f"the live menu ({menu.source}) is {what}, and Pokamind could not be asked "
            "about its module ids this run (see pokamind_whoami): re-run once it answers")


def gate_voice_menu(ev: Evidence) -> GateResult:
    """Every module the robot OFFERS must be one it can start.

    A stale id is the purest silent failure in the stack: the robot speaks the option,
    hears the number, starts the session - and the frame never comes up, which every
    watchdog reports as ``session_never_started``.  Nothing else in the system notices.

    Two questions, because only Pokamind knows what this robot may start:

    1. **Provenance** - always: is the live menu this robot's own (saved on the admin
       page, built from its Pokamind record, or a fork's menu), or a development or
       placeholder menu a deploy left in front of it?  It needs nothing but the
       filesystem.
    2. **Scope** - with the robot's own token, Pokamind's answer
       (``voice_menu_in_scope``): every offered id in this robot's scope and
       startable settles the menu.

    Unproven is WARN, and it says who can prove it.
    """

    if not ev.menu.loaded:
        if ev.menu.reason == "menu_loader_unavailable":
            return GateResult("voice_menu", SKIP, "menu_loader_unavailable")
        return GateResult(
            "voice_menu", FAIL, "voice_menu_unreadable",
            "the active voice menu could not be parsed into a VoiceMenu; the robot will "
            "fall back to the built-in default menu whose module ids are placeholders - "
            "check the menu file the host app serves (a menu saved on the admin page's "
            "Menu tab: Reset to Pokamind; configs/site/auto: Pokamind tab -> Sync now "
            "rebuilds it; a fork's configs/prod menu) and any EMORI_VOICE_TOPICS* "
            "override",
        )
    if not ev.menu.options:
        return GateResult(
            "voice_menu", FAIL, "voice_menu_empty",
            "the active menu offers no options: the robot would ask a question nobody "
            f"can answer - check {ev.menu.source or 'the admin page Menu tab'}",
        )

    provenance = _gate_menu_provenance(ev)
    if provenance is not None:
        return provenance
    # Pokamind itself, asked with this robot's own token, just said every offered
    # module is in scope and startable (voice_menu_in_scope): a live verification.
    scope = _menu_scope_verdict(ev)
    if scope is not None and scope.status == PASS:
        return GateResult("voice_menu", PASS,
                          _menu_pass_code(ev, "modules_verified_by_whoami"))
    if ev.menu.tier == "auto" and scope is None:
        # Built from Pokamind's last verified answer about THIS token (the host app
        # serves the tier only then), and Pokamind cannot be asked right now: the
        # pokamind_whoami gate owns that failure, one line per fault.
        return GateResult("voice_menu", PASS, "voice_menu_from_pokamind")
    return GateResult("voice_menu", WARN, "voice_menu_unverified_against_prod",
                      _unverified_hint(ev))


def gate_gesture(ev: Evidence) -> GateResult:
    if not ev.gesture_enabled:
        # EMORI_GESTURE=0 is REQUIRED on the MuJoCo sim (gravity drift reads as taps).
        return GateResult("gesture", SKIP, "gesture_disabled")
    if ev.gesture_mode != "push_turn":
        return GateResult(
            "gesture", FAIL, "gesture_mode_back_drive",
            "set EMORI_GESTURE_MODE=push_turn: gravity_compensation returns HTTP 500 on daemon "
            "v1.9.0, so the head stays stiff and back_drive sensing can never fire",
        )
    if not ev.gesture_invert:
        # The two are ONE physical fact, not two independent knobs.  push_turn
        # reads a stiff head, a stiff head turns as ROLL, and this mechanism's
        # roll/turn coupling is inverted (measured: an operator LEFT reports
        # d_roll=+0.236, i.e. RIGHT).  Unset therefore does not mean "no
        # inversion configured", it means every signed head turn is reversed, with
        # nothing anywhere saying so.
        return GateResult(
            "gesture", FAIL, "gesture_invert_unset",
            "set EMORI_GESTURE_INVERT=1 alongside EMORI_GESTURE_MODE=push_turn: on this "
            "firmware a human turn shows up as ROLL with an inverted sign, so the signed "
            "head-turn direction is reversed until it is set (set 0 explicitly if this "
            "robot was measured the other way)",
        )
    if ev.gesture_invert not in ("0", "1"):
        # The driver compares against the LITERAL '1'
        # (driver.py GestureTicker.__init__: os.environ.get(...) == "1"), so a
        # truthy-looking EMORI_GESTURE_INVERT=true/yes/on/True does NOT invert.
        # Accepting any non-empty value here made this gate PASS a robot whose
        # every head turn reads the opposite way -- the exact
        # failure the gate was added to make impossible, now with a green matrix
        # over it.  Mirror the driver: only '1' and an explicit '0' are answers.
        return GateResult(
            "gesture", FAIL, "gesture_invert_not_literal_one",
            "EMORI_GESTURE_INVERT must be the literal 1 or 0: the driver tests it as "
            "== \"1\", so true/yes/on/True read as NOT inverted and every turn reads "
            "the opposite way - set 1 (this robot's measured coupling) or 0",
        )
    return GateResult("gesture", PASS, "push_turn")


def gate_voice_delay(ev: Evidence) -> GateResult:
    """The sink the kiosk's voice is meant to go through, when a delay is set.

    WARN, not FAIL: without it the voice goes straight to the speaker and the head runs a
    little behind it, which is how a robot with no delay behaves anyway.
    """

    if ev.voice_delay_ms in ("", "0"):
        return GateResult("voice_delay", PASS, "voice_delay_off")
    if not ev.voice_nodes.available or ev.voice_nodes.returncode != 0:
        return GateResult("voice_delay", WARN, "voice_delay_unreadable",
                          "could not ask PipeWire for its nodes (pw-cli)")
    if 'node.name = "emori_voice"' not in ev.voice_nodes.stdout:
        return GateResult("voice_delay", WARN, "voice_delay_sink_missing",
                          "EMORI_VOICE_DELAY_MS is set and the emori_voice sink is not there, so "
                          "the voice goes straight to the speaker: sudo systemctl enable --now "
                          f"emori-voice-delay, then restart {_gateway_unit(ev)}")
    return GateResult("voice_delay", PASS, f"voice_delay_{ev.voice_delay_ms}ms")


def gate_voice_tap(ev: Evidence) -> GateResult:
    """Whether the robot's voice reaches the motion as it is played.

    WARN, not FAIL: without the tap the motion falls back to fixed pacing, and a tap delivering in
    blocks still works, with the motion that follows it up to 128 ms later. The median gap between
    frames tells them apart: about 20 ms as they are made and about 0 in blocks. A long stall
    drains the pipe in a rush and can read as blocks for about a second, so look again before
    believing it.
    """

    link = _payload(ev.app_status).get("voice_link")
    if isinstance(link, dict) and link.get("enabled") is True and link.get("available") is not True:
        return GateResult("voice_tap", WARN, "voice_tap_down",
                          "the voice tap is not delivering audio, so motion falls back to fixed "
                          f"pacing: restart {_gateway_unit(ev)}")
    gap = link.get("frame_gap_ms") if isinstance(link, dict) else None
    if type(gap) not in (int, float):
        return GateResult("voice_tap", PASS, "voice_tap_not_reporting")
    if gap < _VOICE_TAP_BLOCKS_BELOW_MS:
        cure = ("the tap runs inside the hub's image, and its stdbuf is what is wrong: rebuild it "
                "with a deploy"
                if ev.hub_mode else
                "`command -v stdbuf` (it comes with coreutils), then restart "
                + _gateway_unit(ev))
        return GateResult("voice_tap", WARN, "voice_tap_in_blocks",
                          f"the voice tap's frames arrive in blocks (median gap {gap} ms, not "
                          f"about 20), so motion that follows the voice runs up to 128 ms late. {cure}")
    return GateResult("voice_tap", PASS, "voice_tap_live")


def gate_admin(ev: Evidence) -> GateResult:
    """The phone superadmin service (emori-admin, :8070).

    WARN by design: the robot runs sessions perfectly well without it. A FAIL here
    would block go-live, speak "I am not ready" at whoever is standing there, and
    exit non-zero over a management surface - which is how an advisory finding ends
    up suppressed. What its absence costs is the field-deploy path: no Wi-Fi change,
    no volume, no language switch from a phone.

    ONE exception, and it FAILs (``admin_wifi_backend_missing``): emori-admin is up
    but its own Wi-Fi status or volume read, through the backend it would use for the
    phone, hits a route this robot does not serve or a tool it does not have. That is
    not a service having a bad minute, it is a deploy defect that stays until someone
    fixes it - e.g. a v2 robot whose Network tab answered "HTTP 404" while nothing
    raised its hotspot either. Anything transient (the daemon restarting,
    NetworkManager slow, a sound card still enumerating) stays a WARN.
    """

    if not ev.admin.reached:
        return GateResult(
            "admin", WARN, "admin_unreachable",
            "the phone admin service is not answering: `sudo systemctl restart "
            "emori-admin` (deploy/robot/install.sh) - sessions still run, but Wi-Fi, "
            "volume and the language switch cannot be reached from a phone",
        )
    payload = _payload(ev.admin)
    if ev.admin.status != 200 or payload.get("status") != "ok":
        return GateResult(
            "admin", WARN, "admin_unhealthy",
            "the admin port answered but not status=ok: `journalctl -u emori-admin -n 100`",
        )
    if payload.get("service") != _ADMIN_SERVICE_NAME:
        # The hostapp's OWN /healthz also answers {"status":"ok"}, so an
        # EMORI_ADMIN_PORT pointed at 8080 (or any other live service) would
        # otherwise be reported as a healthy admin service that does not exist.
        return GateResult(
            "admin", WARN, "admin_port_foreign",
            "something OTHER than emori-admin answers on EMORI_ADMIN_PORT (its "
            f"/healthz must say service={_ADMIN_SERVICE_NAME!r}): fix EMORI_ADMIN_PORT "
            "in /opt/emori/.env, or stop whatever else is bound to that port",
        )
    backend = _gate_admin_backend(ev)
    # /healthz says whether an admin PIN is configured (an emori-admin that predates the
    # field says nothing, and is not judged). Without one every :8070 route answers 503:
    # the phone cannot provision this robot at all. A deploy defect (FAIL) still wins.
    if backend.status != FAIL and payload.get("pin_configured") is False:
        return GateResult(
            "admin", WARN, "admin_pin_unset",
            "emori-admin has no admin PIN (EMORI_ADMIN_PIN), so every admin page route "
            "answers 503 and nothing can be set up from a phone: deploy again (install.sh "
            "generates one and shows it once), or set a 6-digit EMORI_ADMIN_PIN in "
            "/opt/emori/.env and `sudo systemctl restart emori-admin`",
        )
    return backend


_ADMIN_PARTS = (("wifi", "Wi-Fi status"), ("volume", "volume read"))
_ADMIN_RESTART = "`sudo systemctl restart emori-admin`, then re-run readycheck"
_ADMIN_WAY_IN = ("until then reach the robot over Ethernet/SSH (`sudo nmcli device wifi "
                 "connect \"<SSID>\" password \"<password>\"` puts it on a site Wi-Fi) - "
                 "docs/robot-admin.md, Troubleshooting")


def _gate_admin_backend(ev: Evidence) -> GateResult:
    """The deep half of :func:`gate_admin` (emori-admin's ``/healthz/deep``)."""

    if not ev.admin_token_set:
        return GateResult(
            "admin", WARN, "admin_token_unset",
            "EMORI_ADMIN_TOKEN is empty in /opt/emori/.env, so readycheck cannot ask "
            "emori-admin whether Wi-Fi and volume work (and the phone's language switch "
            "is off): re-run deploy/robot/install.sh (it generates one), then restart "
            f"emori-admin and {_gateway_unit(ev)}",
        )
    deep = ev.admin_deep
    if not deep.reached:
        return GateResult(
            "admin", WARN, "admin_backend_unavailable",
            "emori-admin answered /healthz but not its Wi-Fi/volume self-check in time: "
            "`journalctl -u emori-admin -n 100`, then re-run readycheck",
        )
    payload = _payload(deep)
    if deep.status == 404:
        if _port_8000_owner(ev) == "daemon":
            # v1: Pollen's daemon still serves /wifi and /api/volume, so the phone's
            # Network and Audio tabs work; this is old code, not a dead tab.
            return GateResult(
                "admin", WARN, "admin_deep_missing",
                f"the running emori-admin has no {_ADMIN_DEEP_PATH} route: it is older "
                "than this checkout. Pollen's daemon answers Wi-Fi and volume on this "
                "robot, so the phone still works, but readycheck cannot check it: "
                f"{_ADMIN_RESTART}",
            )
        return GateResult(
            "admin", FAIL, "admin_wifi_backend_missing",
            f"the running emori-admin has no {_ADMIN_DEEP_PATH} route: it is older than "
            "this checkout and may still send Wi-Fi/volume to daemon routes a v2 robot "
            "does not serve (a v2 robot whose Network tab answered HTTP 404). "
            f"{_ADMIN_RESTART}; {_ADMIN_WAY_IN}",
        )
    if deep.status == 503 and payload.get("error") == "admin_token_unset":
        return GateResult(
            "admin", WARN, "admin_token_unset",
            "emori-admin is running without EMORI_ADMIN_TOKEN (.env edited after it "
            f"started): {_ADMIN_RESTART}",
        )
    if deep.status in (401, 403):
        return GateResult(
            "admin", WARN, "admin_token_rejected",
            "emori-admin refused readycheck's EMORI_ADMIN_TOKEN: /opt/emori/.env changed "
            f"since emori-admin started. {_ADMIN_RESTART}",
        )
    parts = {key: payload.get(key) for key, _ in _ADMIN_PARTS}
    if (deep.status != 200 or payload.get("service") != _ADMIN_SERVICE_NAME
            or not all(type(part) is dict for part in parts.values())):
        return GateResult(
            "admin", WARN, "admin_backend_unavailable",
            f"emori-admin's {_ADMIN_DEEP_PATH} did not answer in the expected shape: "
            "`journalctl -u emori-admin -n 100`",
        )
    stale = _admin_stale_hint(ev, payload)
    if stale:
        return GateResult("admin", FAIL, "admin_stale", stale)
    backend = _admin_slug(payload.get("backend"))
    reason = _admin_slug(payload.get("reason"))
    missing = [(label, _admin_slug(parts[key].get("error"))) for key, label in _ADMIN_PARTS
               if parts[key].get("route_missing") is True
               or parts[key].get("error") in _ADMIN_BACKEND_MISSING]
    if missing:
        return GateResult("admin", FAIL, "admin_wifi_backend_missing",
                          _admin_missing_hint(missing, backend, reason))
    privileges = payload.get("wifi_privileges")
    privileges = privileges if type(privileges) is dict else {"ok": True}
    if privileges.get("ok") is not True \
            and privileges.get("error") in _ADMIN_PRIVILEGES_MISSING:
        return GateResult("admin", FAIL, "admin_wifi_privileges_missing",
                          _admin_privileges_hint(_admin_slug(privileges.get("error"))))
    failing = [(label, _admin_slug(parts[key].get("error"))) for key, label in _ADMIN_PARTS
               if parts[key].get("ok") is not True]
    if privileges.get("ok") is not True:
        failing.append(("Wi-Fi privilege check", _admin_slug(privileges.get("error"))))
    if failing:
        what = ", ".join(f"{label} ({error})" for label, error in failing)
        where = ("the reachy-mini daemon (Services tab, or `systemctl status "
                 "reachy-mini-daemon`)" if backend == "daemon" else
                 "`journalctl -u emori-admin -n 100`")
        return GateResult(
            "admin", WARN, "admin_backend_unavailable",
            f"emori-admin could not do its {what} just now (backend={backend}); if it "
            f"persists, check {where}",
        )
    return GateResult("admin", PASS, "ok")


def _commit_word(marker: object) -> str:
    """The commit in a deploy marker, as hex only (the hint is printed and spoken)."""

    match = _SHA_RE.search(marker) if type(marker) is str else None
    return match.group(0)[:12] if match else "an unknown commit"


def _admin_stale_hint(ev: Evidence, payload: dict) -> str:
    """Why the running emori-admin is older than the code on disk, or ``""``.

    It reports the deploy marker it read at start; the deploy writes the marker before
    it restarts anything. A different marker -- or none, from a build that does not
    report one -- is a process that kept running across a deploy (`enable --now`
    does not restart a running unit), and every other answer it gives is about the
    OLD code. Without a marker on disk (a dev box, a hand-made install) there is
    nothing to hold it to.
    """

    if not ev.deployed_commit:
        return ""
    running = payload.get("commit")
    if type(running) is str and running == ev.deployed_commit:
        return ""
    started = (f"started on {_commit_word(running)}" if type(running) is str and running
               else "does not say which deploy it started on")
    return (f"the running emori-admin is older than the code on disk: it {started}, and "
            f"{_DEPLOYED_MARKER} says {_commit_word(ev.deployed_commit)}, so the phone is "
            f"talking to the previous deploy. {_ADMIN_RESTART}")


def _hub_stale_hint(ev: Evidence) -> str:
    """Why the running hub is older than the code on disk, or ``""``.

    It says which deploy marker it read at start, and the deploy writes the marker before it
    restarts anything. A hub with a session in progress is left running across a deploy, and
    then runs the old code in memory and imports anything later from the new core.
    """

    if not ev.deployed_commit or ev.hub_status.status != 200:
        return ""
    running = _payload(ev.hub_status).get("hub_deployed")
    if type(running) is str and running == ev.deployed_commit:
        return ""
    started = (f"started on {_commit_word(running)}" if type(running) is str and running
               else "does not say which deploy it started on")
    return (f"the running hub is older than the code on disk: it {started}, and "
            f"{_DEPLOYED_MARKER} says {_commit_word(ev.deployed_commit)}. `sudo systemctl restart "
            f"{_HUB_UNIT}` once no session is running. If the deploy said the hub's image was "
            "not built from this code, deploy again first")


def _port_8000_owner(ev: Evidence) -> str:
    """Who answers ``:8000``: ``"node"`` (v2, the robot node's shim), ``"daemon"``
    (Pollen's daemon), or ``""`` when nothing says.

    What ``:8000`` itself says comes first: the shim's ``backend`` marker, or a status
    without one (Pollen's daemon -- even beside a v2 container, which is then the
    failure gate_ros2 names). Silent, the fence flag or a running v2 container still
    say the node owns it.
    """

    payload = _payload(ev.daemon)
    if ev.daemon.reached and payload.get("backend") == _SHIM_BACKEND:
        return "node"
    if ev.daemon.reached and ev.daemon.status == 200 and payload and "backend" not in payload:
        return "daemon"
    if ev.v2_fence or ev.ros2_flavor == "v2":
        return "node"
    return ""


def _admin_slug(value: object) -> str:
    """A word from emori-admin's answer, or ``?`` -- nothing else reaches a hint."""

    return value if type(value) is str and SAFE_CODE_RE.fullmatch(value) else "?"


def _admin_privileges_hint(error: str) -> str:
    what = ("sudo is not installed" if error == "sudo_missing" else
            "sudo refuses them (`sudo -n -l` said no: /etc/sudoers.d/emori-admin is missing "
            "or out of date, and there is no passwordless sudo)")
    return (f"emori-admin runs Wi-Fi itself on this robot (v2) but cannot run the nmcli "
            f"commands that change it as root - {what}. It can read the Wi-Fi, but every "
            "join fails, the hotspot does not come up, and a robot that loses its Wi-Fi "
            "does not raise its hotspot at boot. Re-run a full install (install.sh "
            "validates and installs the sudoers fragment), or by hand: `sudo visudo -c -f "
            "/opt/emori/deploy/robot/sudoers-emori-admin && sudo install -m 0440 "
            "/opt/emori/deploy/robot/sudoers-emori-admin /etc/sudoers.d/emori-admin`, then "
            f"re-run readycheck; {_ADMIN_WAY_IN}")


def _admin_missing_hint(missing: list[tuple[str, str]], backend: str, reason: str) -> str:
    what = " and ".join(label for label, _ in missing)
    errors = {error for _, error in missing}
    if errors <= {"nmcli_missing", "alsa_missing"}:
        tools = []
        if "nmcli_missing" in errors:
            tools.append("NetworkManager (`nmcli`: `sudo apt install network-manager`)")
        if "alsa_missing" in errors:
            tools.append("alsa-utils (`aplay`/`amixer`: `sudo apt install alsa-utils`)")
        return (f"emori-admin runs Wi-Fi and volume itself on this robot (backend={backend}) "
                f"but its {what} needs {' and '.join(tools)}, which is not installed. "
                f"Install it, then {_ADMIN_RESTART}; {_ADMIN_WAY_IN}")
    if reason == "forced" and backend == "daemon":
        fix = ("EMORI_ADMIN_HOST_BACKEND=daemon in /opt/emori/.env forces Pollen's daemon "
               f"on a robot whose :8000 does not serve it: set it to auto, then {_ADMIN_RESTART}")
    else:
        fix = (f"{_ADMIN_RESTART}; if it persists, redeploy - this emori-admin has no host "
               "backend for that route")
    return (f"emori-admin's {what} hit a route this robot does not serve (HTTP 404, "
            f"backend={backend}, {reason}): the phone's Network/Audio tabs are dead - "
            f"a v2 robot whose Network tab answered HTTP 404. {fix}; {_ADMIN_WAY_IN}")


# How stale the newest heartbeat may be before the file this check resolved is
# judged NOT to be the one the live hostapp writes.  Three cadences plus a floor:
# the loop is an asyncio task competing with a session on one CM4 core, and a
# single late beat must not accuse an operator of a misconfigured path.
_DASH_HEARTBEAT_GRACE_FACTOR = 3.0
_DASH_HEARTBEAT_GRACE_FLOOR_S = 300.0


def _dash_heartbeat_grace_s(interval_s: float) -> float:
    return max(_DASH_HEARTBEAT_GRACE_FLOOR_S, _DASH_HEARTBEAT_GRACE_FACTOR * interval_s)


def _demo_branch(ev: Evidence) -> bool:
    """Would ``/api/start`` hand the kiosk the demo url, as the RUNNING process sees it?

    ``/healthz`` reports what the live hostapp computed at import time, and that is
    the only authoritative answer: the repo's most-documented trap is an edited
    ``/opt/emori/.env`` with no ``systemctl restart``, and then this process's own
    environment says "live" while the process serving the kiosk is still in demo.
    Deriving it here would blame the camera bridge (or the Pokamind module)
    for a dashboard that is empty because the hostapp is one restart away from live.

    The env-derived answer is the fallback for the one case healthz cannot cover -
    a hostapp that is not answering at all.
    """

    if ev.healthz.reached:
        return _payload(ev.healthz).get("demo") is not False
    return ev.start_demo_branch


def gate_dashboard_data(ev: Evidence) -> GateResult:
    """Is the management dashboard being FED - not merely reachable.

    The dashboard answering 200 with zeros everywhere looks identical to a quiet
    site, and the four ways it happens are all upstream of the dashboard code:

    a. ``/api/start`` takes the demo branch, so Pokamind is never asked for a real
       session and never sends a completion payload back.  Every outcome column
       stays NULL for the honest reason that nothing was ever measured.
    b. The ``EMORI_DASH_DB`` this process resolves is not the file the hostapp
       writes (a relative default read from another directory, an edited .env the
       running process never re-read).  Proven, not assumed: the hostapp writes a
       heartbeat on a fixed cadence, so a reachable hostapp and no beat at all in
       this file means these are two different files.  Only checked where a beat is
       actually expected: ``EMORI_HEARTBEAT_S=0`` disables the writer by design
       (.env.example), and accusing that robot of a bad path is a false red.
    c. NO session has ever recorded a cycle - the camera analysis stream has never
       reached this file, so attention, engagement and every per-session curve are
       empty.  A single cycle-less session is NOT this: an abandonment, a covered
       lens and an audio-only conversation all produce zero cycles legitimately, so
       the last completed session having none is a WARN.
    d. That session has NULL ``performance_scores``: the conversation happened and
       Pokamind returned no competency scores.  WARN, not FAIL - the session ran,
       the learner got their summary, and the fix is on the Pokamind side (ask
       Pokamind), not on the robot.

    Read-only throughout (see :meth:`SystemProbe.dashboard_data`), and it reports
    counts only: no session id and no excerpt reaches the matrix.
    """

    data = ev.dashboard_data
    # (a) first: in the demo branch every later finding is explained by it, and
    # "no scores on the last session" would send an operator to Pokamind for a
    # token that is missing on the robot.
    if _demo_branch(ev):
        return GateResult(
            "dashboard_data", FAIL, "start_takes_demo_branch",
            "/api/start would hand the kiosk the DEMO url (server.py: no Pokamind "
            f"token, or HOSTAPP_DEMO=1): {_TOKEN_FIX} ({_TOKEN_DOC}), keep HOSTAPP_DEMO "
            f"empty or 0 in /opt/emori/.env, then RESTART {_gateway_unit(ev)} - a demo "
            "session never reports an outcome, so the dashboard can only show zeros",
        )
    if not data.ok:
        if data.reason == "db_missing":
            return GateResult(
                "dashboard_data", FAIL, "dash_db_missing",
                f"no dashboard database at {data.path or '(unset)'}: point EMORI_DASH_DB at "
                "an ABSOLUTE path in /opt/emori/.env (a relative one resolves against "
                f"whatever directory the process was started in) and restart {_gateway_unit(ev)}",
            )
        if data.reason == "db_no_schema":
            return GateResult(
                "dashboard_data", FAIL, "dash_db_foreign",
                f"the file at {data.path} is not a dashboard database (no sessions table): "
                "check EMORI_DASH_DB in /opt/emori/.env - the hostapp creates its schema "
                "on first write, so an unwritten path means nothing has ever been recorded",
            )
        # db_unreadable / db_query_failed / db_path_unset: honest degradation.
        return GateResult("dashboard_data", SKIP, data.reason or "dash_db_unreadable")
    # (b) the file this process resolved must be the one the RUNNING hostapp writes.
    # Only judgeable while the hostapp is up AND a heartbeat is expected: a stopped
    # hostapp writes no beats (that is its downtime, not a path mismatch) and
    # EMORI_HEARTBEAT_S=0 turns the writer off by design.
    beats_expected = (data.heartbeat_interval_s > 0 and ev.healthz.reached
                      and _payload(ev.healthz).get("status") == "ok")
    if beats_expected and data.last_heartbeat_ts <= 0:
        # NO beat has ever been written here.  The strongest evidence there is that
        # this is a different file from the one the hostapp writes.
        return GateResult(
            "dashboard_data", FAIL, "dash_db_not_written",
            f"the hostapp is up but {data.path} contains no heartbeat at all: this is not "
            "the file it writes. Compare EMORI_DASH_DB here with the running unit's "
            f"(`systemctl show {_gateway_unit(ev)} -p Environment`, `-p WorkingDirectory`) and "
            "make both an absolute path",
        )
    if beats_expected:
        grace = _dash_heartbeat_grace_s(data.heartbeat_interval_s)
        if data.now - data.last_heartbeat_ts > grace:
            # Beats ARE in this file, so the path is right and the LOOP is the fault
            # - a different fix, and pointing at EMORI_DASH_DB here would waste it.
            return GateResult(
                "dashboard_data", FAIL, "dash_heartbeat_stale",
                f"{data.path} is the right file (it has heartbeats) but the newest is older "
                f"than {int(grace)}s while the hostapp answers /healthz: the heartbeat task "
                "has died, so uptime_pct decays for a robot that is up. Restart "
                f"{_gateway_unit(ev)} and check the journal for the heartbeat writer",
            )
    if data.finished_sessions <= 0:
        # Nothing has finished yet.  Not a PASS (there is no evidence the pipeline
        # works) and not a FAIL (a freshly installed robot is not broken).
        return GateResult("dashboard_data", SKIP, "no_finished_session_yet")
    # (c) NOTHING in the whole file has ever produced a cycle: the only cycle claim
    # this gate can prove.  One cycle-less session proves nothing - abandonments,
    # covered lenses and audio-only conversations all legitimately have none.
    if not data.any_cycles_ever:
        return GateResult(
            "dashboard_data", FAIL, "dash_no_cycles_ever",
            "no session in this database has EVER recorded an analysis cycle: the camera "
            "path is what produces them - fix the camera_bridge gate first, then confirm "
            f"the kiosk relays pokamind_analysis_update (`journalctl -u {_gateway_unit(ev)} | "
            "grep cycles`). Attention, engagement and the session curve are empty without them",
        )
    if not data.last_completed:
        # Sessions have finished, but none of them COMPLETED, so there is no row to
        # judge (c)/(d) on.  Not a fault of the dashboard feed.
        return GateResult("dashboard_data", SKIP, "no_completed_session_yet")
    if data.last_finished_cycles <= 0:
        return GateResult(
            "dashboard_data", WARN, "dash_last_session_no_cycles",
            "the most recent COMPLETED session recorded zero analysis cycles: legitimate "
            "for an audio-only conversation or a covered lens, but if the camera should "
            "have been running, check the camera_bridge gate and that the kiosk relays "
            "pokamind_analysis_update - that session's engagement curve is empty",
        )
    # (d) scored is Pokamind's job, and its absence is advisory: the session ran.
    if not data.last_finished_scored:
        return GateResult(
            "dashboard_data", WARN, "dash_last_session_unscored",
            "the most recent completed session carries no performance_scores: the completion "
            "payload arrived without them, which is normally the module's analysis settings "
            "in Pokamind not asking for a scored analysis for that module - ask Pokamind to "
            "check them; the robot side is already storing whatever it is sent",
        )
    return GateResult("dashboard_data", PASS, "fed")


def _ros2_compose_file(ev: Evidence) -> str:
    return "docker-compose.v2.robot.yml" if ev.ros2_flavor == "v2" else "docker-compose.robot.yml"


def _ros2_recreate(ev: Evidence) -> str:
    """How this robot's ROS container is recreated: v1's unit, v2's compose file.

    Never the v1 unit on a v2 robot: on a fenced robot install.sh has disabled it, and
    restarting it would START the v1 graph beside the v2 one.
    """

    if ev.ros2_flavor == "v2":
        return "`docker compose -f deploy/ros2/docker-compose.v2.robot.yml up -d`"
    return "`sudo systemctl restart emori-ros2`"


def _ros2_container_name(ev: Evidence) -> str:
    """The container this robot is actually running, for the hints to name.

    A v2 robot told to look at `docker logs emori-ros2` is a person running a command
    that answers "No such container" while their graph is right there.
    """

    return _ROS2_V2_CONTAINER if ev.ros2_flavor == "v2" else _ROS2_CONTAINER


def gate_ros2(ev: Evidence) -> GateResult:
    """The containerised ros4hri graph - and only where an operator armed it.

    ``EMORI_WITH_ROS2`` is off on every shipped robot, and this gate is a PASS there
    rather than a SKIP: the ROS lane is not a dependency that could not be probed, it
    is a feature nobody asked for, and a SKIP would fail ``--strict`` on a robot that
    is completely healthy.  The v2 fence (``/opt/emori/V2_OWNS_THE_BUS``) arms it on its
    own: there the v2 graph IS the robot, so it is never a feature nobody asked for.
    On a fenced robot a running v1 graph is a FAIL of its own (``v1_graph_on_v2_robot``),
    and the native route (``reachy-mini-ros2.service``, no container) is judged by the
    shim on ``:8000`` alone (``ros2_up_native``).  A docker that does not answer this
    user is a SKIP (``docker_unreachable``), never an absent container.

    Armed, it proves the four things that make the lane safe rather than merely
    present, in the order a broken one breaks:

    a. **The container is up.** ``docker inspect .State.Running``.  Not the systemd
       unit: ``emori-ros2.service`` is ``Restart=always`` around ``docker compose up``,
       so a unit that is "active" while the image crash-loops is exactly the shape of
       green-looking nothing this whole module exists to refuse.
    b. **``FIWARE_BRIDGE_ENABLED=0`` inside it.**  The hostapp already historizes every
       cycle; a second writer double-counts every KPI in the FIWARE time series, and the
       bridge is enabled by default, so this is a trap you fall into by omission.
       Asked of the CONTAINER, because compose interpolates ``.env`` once at ``up``.
    c. **The GRAPH is running, not just the process hosting it.**  ``ros2 node list``
       inside the container.  The CMD is ``ros2 launch ... slice2.launch.py``, and
       ``ros2 launch`` keeps the other nodes alive when one dies at import - a missing
       ``hri_msgs`` in ``hri_context_analyzer`` costs the whole ``/context/*`` fan-out
       and the ``expression`` topic while ``.State.Running`` stays ``true`` and
       ``printenv`` still answers.  (a) alone is the same green-looking nothing this
       module refuses one level up.
    d. **The lane has somewhere to POST.**  ``skill_facade`` only POSTs when the motion
       owner is ``ros``; when it is, ``/api/skill`` must actually be mounted, or every
       skill request is answered 403 and the graph decides beautifully and moves
       nothing.  Probed with a GET on the POST-only route - 405 mounted, 404 not - so
       the check itself can never submit motion.

    Between (b) and (d) it also compares the container's own motion owner with the
    host's: an operator who edits ``EMORI_ROS_MOTION_OWNER`` in ``/opt/emori/.env`` and
    restarts nothing is running a graph on the previous answer, which reads exactly
    like "ROS mode does not work".

    TOPIC-level verification stays a runbook step: it needs the launch file's
    ``person_id``, and this gate must not grow a second source of truth for the topic
    graph.  Node names are the launch file's own ``name=``, need no parameter, and are
    the cheaper question anyway.
    """

    if ev.v2_without_fence:
        return GateResult(
            "ros2", FAIL, "motor_container_without_fence",
            f"{_ROS2_V2_CONTAINER} is running with the fence ({_V2_FENCE_FLAG}) down, so it and "
            "Pollen's daemon can both open the motor bus: bash "
            "deploy/robot/ros2_native/v2-stack.sh apply v1 takes it down")
    if not ev.ros2_enabled:
        return GateResult("ros2", PASS, "ros2_lane_off")
    if ev.v2_fence and ev.ros2_flavor == "v1":
        # The fence says the v2 graph is the robot; a v1 graph running instead is one
        # left from the robot's v1 days (an emori-ros2.service still enabled), and it
        # drives nothing: Pollen's daemon, which it asks to move the robot, cannot start.
        return GateResult(
            "ros2", FAIL, "v1_graph_on_v2_robot",
            f"{_ROS2_CONTAINER} (the v1 graph) is running and {_ROS2_V2_CONTAINER} is not, "
            f"on a robot whose v2 fence ({_V2_FENCE_FLAG}) keeps Pollen's daemon off: "
            "nothing moves this robot. `sudo systemctl disable --now emori-ros2` (install.sh "
            "step 11 does it too), then bring the v2 graph up: `docker compose -f "
            "deploy/ros2/docker-compose.v2.robot.yml up -d`, or `sudo systemctl start "
            "reachy-mini-ros2` on the native route (deploy/robot/ros2_native/README.md)",
        )
    if ev.ros2_native and not _container_running(ev.ros2_container):
        return _gate_ros2_native(ev)
    if not ev.ros2_container.available:
        # docker itself is missing. Armed by EMORI_WITH_ROS2=1, install.sh step 11 says
        # so and enables nothing, so this is an un-run install, not a broken robot. Armed
        # by the fence alone (no native unit, see above), the v2 container cannot exist
        # either, which the daemon gates report as a robot that does not answer.
        return GateResult("ros2", SKIP, "docker_unavailable")
    if (ev.ros2_container.returncode != 0 and ev.ros2_docker.available
            and ev.ros2_docker.returncode != 0):
        # Both inspects failed and docker itself does not answer: the container may be
        # running perfectly well behind a socket this user cannot open.
        return GateResult(
            "ros2", SKIP, "docker_unreachable",
            "docker is installed but did not answer readycheck (`docker version` failed), "
            "so nothing here can tell whether the ROS container runs: the user running "
            "readycheck is not in the docker group (`sudo usermod -aG docker pollen`, then "
            "log in again) or dockerd is down (`systemctl status docker`)",
        )
    if ev.ros2_both_running:
        return GateResult(
            "ros2", FAIL, "two_ros_graphs_running",
            f"{_ROS2_CONTAINER} (v1) and {_ROS2_V2_CONTAINER} (v2) are BOTH running: two "
            "engagement lanes deciding what the robot does, two publishers per topic, and "
            "under v2 a second process reaching for /dev/ttyAMA3. Stop one -- `docker "
            f"compose -f deploy/ros2/docker-compose.v2.robot.yml down` for v2, `sudo "
            "systemctl stop emori-ros2` for v1",
        )
    if ev.ros2_container.returncode != 0:
        if ev.ros2_flavor == "v2":
            return GateResult(
                "ros2", FAIL, "ros2_container_absent",
                f"there is no {_ROS2_V2_CONTAINER} container, and the v2 fence "
                f"({_V2_FENCE_FLAG}) keeps Pollen's daemon off: NOTHING can move this "
                "robot. Create it from the robot's image (`docker compose -f "
                "deploy/ros2/docker-compose.v2.robot.yml up -d`, "
                "deploy/robot/ros2_native/README.md), or lower the fence if this robot "
                "is going back to v1 (the README's 'Rolling back to v1')",
            )
        return GateResult(
            "ros2", FAIL, "ros2_container_absent",
            f"EMORI_WITH_ROS2=1 but there is no {_ROS2_CONTAINER} container: load the "
            "arm64 image and enable the unit (`EMORI_WITH_ROS2=1 bash "
            "deploy/robot/install.sh` step 11, which loads "
            "/opt/emori/emori-ros2-humble-arm64.tgz), or set EMORI_WITH_ROS2=0 if this "
            "robot is not meant to run the ROS graph",
        )
    if not _container_running(ev.ros2_container):
        if ev.ros2_flavor == "v2":
            return GateResult(
                "ros2", FAIL, "ros2_container_stopped",
                f"the {_ROS2_V2_CONTAINER} container exists but is not running, so "
                "nothing moves this robot: `docker logs --tail 50 emori-ros2-v2`, then "
                "`docker start emori-ros2-v2` -- a missing /dev/video41 fails its start "
                "(`devices:`; readycheck's camera_bridge and gaze gates)",
            )
        return GateResult(
            "ros2", FAIL, "ros2_container_stopped",
            f"the {_ROS2_CONTAINER} container exists but is not running: `sudo "
            "systemctl status emori-ros2` and `docker logs emori-ros2` - an amd64 image "
            "on the CM4 dies with `exec format error` (build it with `docker buildx "
            "build --platform linux/arm64`)",
        )
    if not ev.ros2_fiware.available:
        return GateResult("ros2", SKIP, "ros2_env_unreadable")
    if ev.ros2_fiware.returncode != 0 or ev.ros2_fiware.stdout.strip() != "0":
        return GateResult(
            "ros2", FAIL, "ros2_fiware_bridge_on",
            "FIWARE_BRIDGE_ENABLED is not 0 inside the ROS container: the hostapp "
            "already historizes every cycle, so the bridge would write a SECOND row for "
            "each one and every ARISE KPI would be counted twice. It is set in "
            f"deploy/ros2/{_ros2_compose_file(ev)} - recreate the container "
            f"({_ros2_recreate(ev)}) after fixing it",
        )
    # Both sides through the same normaliser, and again here rather than only in
    # `collect`: a typo is the shipped default on both ends, so it can never be
    # reported as drift (and the gate stays correct for any caller's Evidence).
    container_owner = _motion_owner(ev.ros2_owner.stdout
                                    if ev.ros2_owner.returncode == 0 else "")
    host_owner = _motion_owner(ev.ros_motion_owner)
    if container_owner != host_owner:
        return GateResult(
            "ros2", FAIL, "ros2_owner_drift",
            f"the running container drives motion as {container_owner!r} while "
            f"/opt/emori/.env says {host_owner!r}: compose interpolates .env "
            "once, at `up`, so the edit has not taken effect. "
            f"{_ros2_recreate(ev)} to recreate it with the current value"
            + (" (set in /opt/emori/deploy/ros2/.env: compose never reads /opt/emori/.env)"
               if ev.ros2_flavor == "v2" else ""),
        )
    hub_verdict = _gate_ros2_hub(ev, container_owner)
    if hub_verdict is not None:
        return hub_verdict
    # The graph, not the process that hosts it. Deliberately AFTER the two env checks,
    # which answer instantly, and before the intake probe: a robot whose graph is half
    # dead should not be told about /api/skill first.
    if not ev.ros2_nodes.available:
        return GateResult("ros2", SKIP, "ros2_graph_unreadable")
    if ev.ros2_nodes.returncode != 0:
        return GateResult(
            "ros2", FAIL, "ros2_graph_unlistable",
            f"`ros2 node list` failed inside {_ros2_container_name(ev)}, so nothing here knows "
            "whether the graph is running: `docker exec emori-ros2 bash -lc 'source "
            "/opt/ros/humble/setup.bash && source /emori/ros2_ws/install/setup.bash && "
            "ros2 node list'` and `docker logs emori-ros2` - an unbuilt overlay "
            "(`/emori/ros2_ws/install` missing) fails exactly here",
        )
    # Suffix match on the '/'-prefixed, possibly namespaced names `ros2 node list` prints.
    listed = {line.strip().rsplit("/", 1)[-1]
              for line in ev.ros2_nodes.stdout.splitlines() if line.strip()}
    # v2 puts the robot itself in the graph, so its absence is not a missing observer --
    # it is a robot with no motors attached to anything, and the four EMORI nodes would
    # keep deciding and publishing exactly as if there were.
    expected = _ROS2_NODES + ((_ROS2_V2_NODE,) if ev.ros2_flavor == "v2" else ())
    missing = [name for name in expected if name not in listed]
    if missing:
        return GateResult(
            "ros2", FAIL, "ros2_graph_incomplete",
            f"the {_ros2_container_name(ev)} container is running but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not in `ros2 node list`: `ros2 "
            "launch` keeps the rest of the graph alive when one node dies at import, so "
            "the container looks healthy while that node's topics publish NOTHING all "
            f"session. `docker logs {_ros2_container_name(ev)}` - a missing hri_msgs or "
            "an unbuilt overlay is the usual cause",
        )
    failed_nodes = (_payload(ev.hub_status).get("hub_nodes") or {}).get("failed") or []
    if ev.hub_mode and failed_nodes:
        # The hub runs on without a node it could not build, so nothing else here would see it.
        return GateResult(
            "ros2", FAIL, "ros2_hub_node_failed",
            f"the hub could not build {', '.join(str(name) for name in failed_nodes)}, so what "
            f"they publish and serve is missing. `journalctl -u {_HUB_UNIT} -n 200`",
        )
    if ev.hub_mode and _HUB_NODE not in listed:
        # The graph is one DDS domain, so the motor container's node list holds the hub's
        # node as well: its absence is a hub that never reached the robot node, or a
        # discovery that does not cross the two containers.
        return GateResult(
            "ros2", FAIL, "ros2_hub_node_missing",
            f"{_HUB_CONTAINER} is running but its node {_HUB_NODE} is not in `ros2 node list` "
            f"as {_ros2_container_name(ev)} sees it: the hub has no link to the robot node. "
            f"`journalctl -u {_HUB_UNIT} -n 100`, and check that both containers use the "
            "same ROS_DOMAIN_ID and the loopback peers profile",
        )
    if ev.ros2_flavor == "v2":
        # The one thing that distinguishes "v2 is running" from "v2 is running and the
        # hostapp is talking to something else": Pollen's daemon has no `backend` field,
        # the shim answers `reachy_mini_ros2`. The failure this catches: the shim can
        # lose a race for :8000 with the TIME_WAIT sockets of the daemon it replaced, and
        # the node comes up owning the serial bus with nothing serving the hostapp while
        # every other gate here is green.
        if not ev.daemon.reached:
            return GateResult("ros2", SKIP, "daemon_port_unreachable")
        backend = _payload(ev.daemon).get("backend")
        if backend != _SHIM_BACKEND:
            return GateResult(
                "ros2", FAIL, "shim_not_serving_the_daemon_port",
                f"{_ROS2_V2_CONTAINER} is running but {ev.reachy_base_url}/api/daemon/status "
                f"reports backend={backend or 'absent (Pollen daemon)'}: the hostapp is not "
                "talking to the robot node. Either the daemon was never stopped (`sudo "
                "systemctl stop reachy-mini-daemon`; the fence is "
                "/opt/emori/V2_OWNS_THE_BUS) or the shim failed to bind -- "
                "`docker logs emori-ros2-v2 | grep 'REST shim'`",
            )
    # The intake matters only when the facade actually POSTs to it. v2's default
    # transport is ros_native: the facade calls the robot node's actions, /api/skill is
    # not in the path, and demanding it would fail a robot for missing a route it has no
    # use for. Anything that is not explicitly ros_native is treated as HTTP, because
    # requiring the intake is the safe direction to be wrong in.
    transport = (ev.ros2_transport.stdout.strip()
                 if ev.ros2_transport.returncode == 0 else "")
    if ev.hub_mode:
        # The hub's process got its environment from the unit's files, which `docker exec
        # printenv` does not show: it says so itself.
        transport = str(_payload(ev.hub_status).get("skill_transport") or "").strip()
    if container_owner == "ros" and transport != _ROS_NATIVE_TRANSPORT:
        if not ev.skill_api.reached:
            return GateResult("ros2", SKIP, "hostapp_unreachable")
        if ev.skill_api.status != 405:
            # 404: the route was never mounted. Anything else on a POST-only path is
            # not this endpoint either, and both mean the same thing to the facade.
            return GateResult(
                "ros2", FAIL, "skill_api_not_mounted",
                "EMORI_ROS_MOTION_OWNER=ros but /api/skill is not mounted, so every "
                "skill request the graph POSTs is refused and the robot never moves: "
                "set EMORI_SKILL_API=1 AND EMORI_SKILL_KEY (the intake refuses to mount "
                f"without the secret) in /opt/emori/.env and restart {_gateway_unit(ev)} - the "
                "container needs the same key",
            )
    return GateResult("ros2", PASS, "ros2_up_v2" if ev.ros2_flavor == "v2" else "ros2_up")


def _gate_ros2_hub(ev: Evidence, container_owner: str) -> GateResult | None:
    """The second container: the hub, asked as the motor container is.

    ``None`` when the robot is not in hub mode or the hub passes. Running, then
    FIWARE_BRIDGE_ENABLED=0 in it, then the robot link and the motion owner it reports
    about itself. The owner has three sources (the host's .env, the motor container, the
    hub process) and any two that differ are a graph deciding on the previous answer.
    """

    if not ev.hub_mode:
        return None
    unit = _HUB_UNIT
    if not ev.hub_container.available:
        return GateResult("ros2", SKIP, "docker_unavailable")
    if not _container_running(ev.hub_container):
        absent = ev.hub_container.returncode != 0
        return GateResult(
            "ros2", FAIL, "ros2_hub_container_absent" if absent else "ros2_hub_container_stopped",
            f"the hub flag ({_HUB_FLAG}) is up but the {_HUB_CONTAINER} container is "
            f"{'missing' if absent else 'not running'}: nothing serves the kiosk. "
            f"`sudo systemctl status {unit}` and `journalctl -u {unit} -n 100`; "
            "deploy/robot/hub-stack.sh apply hostapp puts the host application back",
        )
    if not ev.hub_fiware.available:
        return GateResult("ros2", SKIP, "ros2_env_unreadable")
    if ev.hub_fiware.returncode != 0 or ev.hub_fiware.stdout.strip() != "0":
        return GateResult(
            "ros2", FAIL, "ros2_hub_fiware_bridge_on",
            "FIWARE_BRIDGE_ENABLED is not 0 inside the hub container: the hub historizes "
            "every cycle already, so a bridge there would write a second row for each. It is "
            "set in deploy/hub/docker-compose.hub.robot.yml - fix it and "
            f"`sudo systemctl restart {unit}`",
        )
    if not ev.hub_status.reached:
        return GateResult("ros2", SKIP, "hostapp_unreachable")
    status = _payload(ev.hub_status)
    if ev.hub_status.status != 200 or status.get("runtime") != _RUNTIME_ROS:
        return GateResult(
            "ros2", FAIL, "ros2_hub_not_ros",
            "the hub's /api/robot/status does not report runtime=ros "
            f"(HTTP {ev.hub_status.status}, runtime={status.get('runtime') or 'absent'}): "
            "its robot client is not the ROS link. "
            f"`journalctl -u {unit} -n 100`",
        )
    hub_owner = _motion_owner(str(status.get("motion_owner") or ""))
    host_owner = _motion_owner(ev.ros_motion_owner)
    if not (container_owner == host_owner == hub_owner):
        return GateResult(
            "ros2", FAIL, "ros2_owner_drift",
            f"motion owner differs between the sources: /opt/emori/.env says {host_owner!r}, "
            f"the motor container drives {container_owner!r}, the hub reports {hub_owner!r}. "
            "Compose reads each file once, at `up`, and the hub reads its environment once, "
            "at start: set the same value in /opt/emori/.env and /opt/emori/deploy/ros2/.env, "
            f"recreate the motor container and `sudo systemctl restart {unit}`",
        )
    return None


def _gate_ros2_native(ev: Evidence) -> GateResult:
    """A fenced robot on the native route (Route B): the robot node is a systemd unit.

    There is no container to ask for its environment or its node list, so what is
    judged is the one thing that says the node is up and serving the host app: the
    daemon port answering as the node's shim.
    """

    if not ev.daemon.reached:
        return GateResult("ros2", SKIP, "daemon_port_unreachable")
    backend = _payload(ev.daemon).get("backend")
    if backend != _SHIM_BACKEND:
        return GateResult(
            "ros2", FAIL, "shim_not_serving_the_daemon_port",
            f"{ev.reachy_base_url}/api/daemon/status reports backend="
            f"{backend or 'absent (Pollen daemon)'} on a fenced robot with "
            "reachy-mini-ros2.service: the host app is not talking to the robot node. "
            "`systemctl status reachy-mini-ros2` and `journalctl -u reachy-mini-ros2 -n 100`",
        )
    return GateResult("ros2", PASS, "ros2_up_native")


def gate_hub_identity(ev: Evidence) -> GateResult:
    """Which gateway answers :8090, and whether the hub is wired the way it needs to be.

    Without the hub flag this is a PASS unless the hub answers anyway (an old process left
    behind a lowered flag). With it, ``/healthz.runtime`` has to be the transport
    ``EMORI_ROBOT_TRANSPORT`` asks for (``ros`` unless set): the host application answering
    behind a raised flag reads ``http``, a robot with a gateway and no link to its motors.
    Then two things only a container can get wrong:

    - the cache directory it mounts is writable by its user (uid 1000), or the first model
      or add-on file written there fails;
    - the add-on is installed for the hub exactly when it is installed for the host's own
      tools (admin, readycheck, settings). The hub finds it through a directory the deploy
      fills; a missing or stale one would otherwise show up later as a malformed status.
    """

    runtime = _payload(ev.healthz).get("runtime") if ev.healthz.reached else None
    if not ev.hub_mode:
        if runtime == _RUNTIME_ROS:
            return GateResult(
                "hub_identity", FAIL, "hub_answering_without_flag",
                f"/healthz reports runtime=ros but the hub flag ({_HUB_FLAG}) is down: a hub "
                f"process is running that systemd would not start. `sudo systemctl stop "
                f"{_HUB_UNIT}`, or raise the flag with deploy/robot/hub-stack.sh apply ros",
            )
        return GateResult("hub_identity", PASS, "hub_off")
    if not ev.healthz.reached:
        return GateResult("hub_identity", SKIP, "hostapp_unreachable")
    wanted = ev.robot_transport or _RUNTIME_ROS
    if wanted not in (_RUNTIME_ROS, _RUNTIME_HTTP):
        return GateResult(
            "hub_identity", FAIL, "hub_transport_invalid",
            "EMORI_ROBOT_TRANSPORT must be ros or http (or empty for ros): the hub refuses "
            f"to start on anything else. Fix it in /opt/emori/.env, then `sudo systemctl "
            f"restart {_HUB_UNIT}`",
        )
    if runtime != wanted:
        return GateResult(
            "hub_identity", FAIL, "hub_wrong_runtime",
            f"the hub flag ({_HUB_FLAG}) is up and EMORI_ROBOT_TRANSPORT asks for {wanted}, "
            f"but /healthz reports runtime={runtime or 'absent'}: the gateway answering :8090 "
            f"is not the hub. `sudo systemctl status {_HUB_UNIT} emori-hostapp`; "
            "deploy/robot/hub-stack.sh apply hostapp lowers the flag if the hub cannot run",
        )
    if ev.hub_cache.available and ev.hub_cache.returncode != 0:
        return GateResult(
            "hub_identity", FAIL, "hub_cache_not_writable",
            f"{_HUB_CACHE_DIR} is not writable by the hub's user (uid 1000): "
            f"`sudo chown -R pollen: {_HUB_CACHE_DIR}`, then `sudo systemctl restart "
            f"{_HUB_UNIT}`",
        )
    if ev.host_has_addon is not None and ev.hub_addon_status.reached:
        hub_has = ev.hub_addon_status.status == 200 and bool(ev.hub_addon_status.payload)
        if ev.host_has_addon and not hub_has:
            return GateResult(
                "hub_identity", FAIL, "hub_addon_missing",
                "an add-on is installed for this robot's own tools but the hub does not "
                "see it: its directory for the hub container (/opt/emori/hub-site) is "
                "missing or older than the add-on. Deploy again, which installs it, then "
                f"`sudo systemctl restart {_HUB_UNIT}`",
            )
        if hub_has and not ev.host_has_addon:
            return GateResult(
                "hub_identity", FAIL, "hub_addon_unexpected",
                "the hub reports an add-on that this robot's own tools do not find: "
                "/opt/emori/hub-site holds files of an add-on that was removed, or a quick "
                "deploy shipped one it could only install for the hub. A full deploy installs "
                "it for both; --remove-addons, or deleting that directory, takes it away. Then "
                f"restart {_HUB_UNIT}",
            )
    stale = _hub_stale_hint(ev)
    if stale:
        return GateResult("hub_identity", WARN, "hub_restart_pending", stale)
    return GateResult("hub_identity", PASS, f"hub_{wanted}")


def gate_hub_audio(ev: Evidence) -> GateResult:
    """The hub's way to the speaker and the microphone: PipeWire, through one socket.

    The container has no device nodes. ``/api/status`` says whether the voice tap is
    delivering audio and whether the capture gain was applied; a wpctl that cannot talk to
    the host's PipeWire only logs, and the first sign would otherwise be turns that read as
    silence. With a voice delay set, the sink the tap listens on must exist as well.
    """

    if not ev.hub_mode:
        return GateResult("hub_audio", PASS, "hub_audio_off")
    if not ev.app_status.reached:
        return GateResult("hub_audio", SKIP, "hostapp_unreachable")
    status = _payload(ev.app_status)
    if ev.voice_delay_ms not in ("", "0"):
        if not ev.voice_nodes.available or ev.voice_nodes.returncode != 0:
            return GateResult("hub_audio", SKIP, "voice_nodes_unreadable")
        if _SINK_NODE_LINE not in ev.voice_nodes.stdout:
            return GateResult(
                "hub_audio", FAIL, "hub_audio_sink_missing",
                "EMORI_VOICE_DELAY_MS is set and the emori_voice sink the hub's voice tap "
                "listens on is not there: `sudo systemctl enable --now emori-voice-delay`, "
                f"then `sudo systemctl restart {_HUB_UNIT}`",
            )
    link = status.get("voice_link")
    link = link if isinstance(link, dict) else {}
    if link.get("enabled") is not False and link.get("available") is not True:
        return GateResult(
            "hub_audio", FAIL, "hub_voice_link_down",
            "the hub's voice tap is not delivering audio: the container cannot reach "
            "PipeWire's socket (/run/user/1000/pipewire-0) or its client cannot record. "
            f"`journalctl -u {_HUB_UNIT} -n 100 | grep -i voice`; restart {_HUB_UNIT} "
            "after PipeWire is up",
        )
    if not status.get("mic_profile"):
        return GateResult(
            "hub_audio", FAIL, "hub_mic_profile_unset",
            "the capture gain was never applied (mic_profile is empty): wpctl in the "
            "container did not reach the host's PipeWire, and a session would hear nothing. "
            f"`journalctl -u {_HUB_UNIT} -n 100 | grep -i wpctl`; restart {_HUB_UNIT}",
        )
    return GateResult("hub_audio", PASS, "hub_audio_ok")


GATES = (
    gate_hostapp,
    gate_demo_off,
    gate_robot_link,
    gate_daemon,
    gate_motors,
    gate_daemon_routes,
    gate_camera_bridge,
    gate_gaze,
    gate_tts,
    gate_stt,
    gate_pokamind,
    gate_clock,
    gate_pokamind_token,
    gate_identity,
    gate_pokamind_whoami,
    gate_site_config,
    gate_session_chain,
    gate_badge_session,
    gate_voice_menu,
    gate_voice_menu_in_scope,
    gate_gesture,
    gate_voice_delay,
    gate_voice_tap,
    gate_admin,
    gate_dashboard_data,
    gate_ros2,
    gate_hub_identity,
    gate_hub_audio,
)


def active_gates() -> list:
    """The built-in gates with an installed add-on's, each after the gate it names."""

    gates = list(GATES)
    for extra in _contributed_gates():
        names = [getattr(gate, "__name__", "") for gate in gates]
        wanted = "gate_" + extra.after
        slot = names.index(wanted) + 1 if extra.after and wanted in names else len(gates)
        gates.insert(slot, extra.run)
    return gates


def evaluate(evidence: Evidence) -> list[GateResult]:
    return [gate(evidence) for gate in active_gates()]


def announce_failure(results, evidence: Evidence, probe) -> bool:
    """Say ONE short line about a failed run, through the robot's own speaker.

    The robot is screenless: a matrix nobody can see is the same as no matrix at all.
    The line is picked by what actually broke - claiming "I cannot reach Pokamind" on a
    robot whose motors are disabled would send the operator to the wrong machine - and by
    EMORI_LANG, falling back to English for a language we have no line for.

    The RESOLVED language, not the raw EMORI_LANG, is what /api/tts is asked for:
    PiperTts._voice falls back to the first loaded voice for a tag it has no model for
    (tts.py:50-52), so asking for another language while speaking the English fallback line
    would hand that sentence to the Catalan voice.
    """

    failed = [result for result in results if result.status == FAIL]
    if not failed:
        return False
    key = evidence.lang.split("-")[0].split("_")[0].lower()  # identity._lang_code
    if key not in SPOKEN_LINES:
        key = "en"
    link_line, local_line = SPOKEN_LINES[key]
    text = link_line if any(result.name in _LINK_GATES for result in failed) else local_line
    return bool(probe.speak(evidence.hostapp_url, key, text))


def exit_code(results, *, strict: bool = False) -> int:
    # WARN is deliberately absent from both branches, including --strict: an
    # advisory that can turn a wrapper script red is an advisory somebody deletes.
    if any(result.status == FAIL for result in results):
        return 1
    if strict and any(result.status == SKIP for result in results):
        return 2
    return 0


def format_matrix(results) -> str:
    width = max((len(result.name) for result in results), default=4)
    lines = [f"{'GATE'.ljust(width)}  STATUS  DETAIL"]
    lines.extend(
        f"{result.name.ljust(width)}  {_LABELS[result.status]}    {result.code}"
        for result in results
    )
    counts = {status: sum(1 for r in results if r.status == status) for status in _STATUSES}
    lines.append("")
    lines.append(
        f"PASS {counts[PASS]}  FAIL {counts[FAIL]}  SKIP {counts[SKIP]}  WARN {counts[WARN]}"
    )
    lines.extend(
        f"  ! {result.name}: {result.hint}" for result in results if result.status == FAIL
    )
    # A distinct marker, not a second "!": an advisory line that reads like a
    # failure is one an operator learns to skip past.
    lines.extend(
        f"  ~ {result.name}: {result.hint}" for result in results if result.status == WARN
    )
    # A PASS that carries a detail (site_config's KEY=source pairs) prints it under its
    # own marker: information, never a finding.
    lines.extend(
        f"  = {result.name}: {result.hint}" for result in results
        if result.status == PASS and result.hint
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emori-readycheck",
        description="EMORI robot readiness self-check (read-only).",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the matrix")
    parser.add_argument("--strict", action="store_true", help="exit 2 when any gate SKIPs")
    parser.add_argument("--no-speak", action="store_true",
                        help="never speak the failure line through the robot speaker")
    args = parser.parse_args(argv)
    probe = SystemProbe()
    evidence = collect(probe)
    results = evaluate(evidence)
    if args.json:
        print(json.dumps([asdict(result) for result in results], sort_keys=True))
    else:
        print(identity_line(evidence))
        print(format_matrix(results))
    if not args.no_speak and not args.json:
        # AFTER the report, never instead of it: synthesis plus playback costs seconds,
        # and a reader at a terminal must not wait on the speaker.  Best-effort - a
        # boot-time run is the only notification anybody standing next to a screenless
        # robot will ever get, but failing to speak must not change the verdict.
        # --json implies a MACHINE consumer (the shell wrappers' documented read-only
        # contract): machines never want seconds of audio as a side effect.
        announce_failure(results, evidence, probe)
    return exit_code(results, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
