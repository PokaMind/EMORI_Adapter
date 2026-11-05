"""Shared doubles for the emori-admin service tests (no tests of its own).

Every seam the admin service has on the outside world is one of two module
attributes — ``probes.run`` (subprocess) and ``probes.request`` (httpx) — so a
test replaces those two and the whole service becomes deterministic on a
Windows box with no nmcli, no systemctl, no journalctl, no wpctl and no daemon.

That is also the production reality this code has to survive, which is why the
DEFAULT of both doubles is "nothing is there": an unregistered command reports
``missing`` and an unregistered URL reports ``unreachable``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from emori_adapter.admin import host_backend, mixer, netman, probes

PIN = "246813"
SALT = "admin-unit-test-salt"

# Env keys that must not leak in from the developer's own .env (any earlier test
# that imports emori_adapter.config loads it into os.environ for the session).
ADMIN_ENV_KEYS = (
    "EMORI_ADMIN_PIN", "EMORI_ADMIN_SALT", "EMORI_ADMIN_TOKEN", "EMORI_ADMIN_PORT",
    "EMORI_ADMIN_HOST", "EMORI_ADMIN_STATE_FILE",
    "EMORI_AP_SSID", "EMORI_AP_PASSWORD", "EMORI_ADMIN_WIFI_WATCHDOG",
    "EMORI_ADMIN_WIFI_GRACE_S", "EMORI_MIC_SESSION_VOL", "EMORI_MIC_IDLE_VOL",
    "EMORI_ROBOT_NAME", "EMORI_ROBOT_ID", "REACHY_BASE_URL", "HOSTAPP_PORT",
    "POKAMIND_BASE", "EMORI_ADMIN_HOST_BACKEND", "EMORI_PLAY_TARGET",
)


class FakeRun:
    """Stands in for ``probes.run``.  Unmatched argv -> the command is absent."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.envs: list[dict | None] = []
        self._rules: list[tuple[tuple[str, ...], probes.Cmd]] = []

    def add(self, prefix, *, stdout: str = "", returncode: int = 0,
            stderr: str = "") -> None:
        self._rules.append((tuple(prefix), probes.Cmd(
            ok=returncode == 0, ran=True, returncode=returncode, stdout=stdout,
            stderr=stderr, error="" if returncode == 0 else "exit_%d" % returncode)))

    def add_failure(self, prefix, *, error: str = "timeout") -> None:
        self._rules.append((tuple(prefix), probes.Cmd(error=error)))

    def __call__(self, argv, *, timeout_s=probes.CMD_TIMEOUT_S, runner=None, env=None):
        self.calls.append(list(argv))
        self.timeouts.append(timeout_s)
        self.envs.append(env)
        for prefix, result in self._rules:
            if tuple(argv[:len(prefix)]) == prefix:
                return result
        return probes.Cmd(error="missing")

    def argv_for(self, *prefix) -> list[list[str]]:
        return [call for call in self.calls if tuple(call[:len(prefix)]) == prefix]

    def ran(self, *prefix) -> bool:
        return bool(self.argv_for(*prefix))


def http_ok(payload=None, *, status: int = 200, content: bytes = b"",
            content_type: str = "application/json") -> probes.Http:
    return probes.Http(ok=status < 400, reached=True, status=status, payload=payload,
                       content=content, content_type=content_type,
                       error="" if status < 400 else "http_%d" % status)


def http_status(status: int, payload=None) -> probes.Http:
    return http_ok(payload, status=status)


class FakeHttp:
    """Stands in for ``probes.request``.  Unmatched (method, path) -> unreachable."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._routes: dict[tuple[str, str], probes.Http] = {}

    def add(self, method: str, path: str, result: probes.Http) -> None:
        self._routes[(method.upper(), path)] = result

    def __call__(self, method, url, *, timeout_s=probes.HTTP_TIMEOUT_S, params=None,
                 json_body=None, headers=None):
        path = urlsplit(url).path
        self.calls.append({"method": method.upper(), "url": url, "path": path,
                           "timeout_s": timeout_s, "params": params,
                           "json": json_body, "headers": headers or {}})
        return self._routes.get((method.upper(), path), probes.Http(error="unreachable"))

    def called(self, method: str, path: str) -> list[dict]:
        return [c for c in self.calls
                if c["method"] == method.upper() and c["path"] == path]


def reset_backends(monkeypatch, tmp_path=None) -> None:
    """Forget every cached Wi-Fi/volume backend decision and host-side state.

    The selector, the host Wi-Fi router's last error and the mixer's card are
    process-wide (as on the robot), so a test that taught them something must
    not hand it to the next one. The v2 fence flag is pointed at a path that
    cannot exist, so no dev box is ever mistaken for a fenced v2 robot.
    """

    host_backend.reset()
    flag = ("/nonexistent/emori-test/V2_OWNS_THE_BUS" if tmp_path is None
            else str(tmp_path / "V2_OWNS_THE_BUS"))
    monkeypatch.setattr(host_backend.selector(), "fence_flag", flag)
    monkeypatch.setattr(netman, "_last_error", None)
    # A mutation runs inline in a test: no thread outlives it, no sleep needed.
    monkeypatch.setattr(netman, "_spawn", lambda target: target())
    monkeypatch.setattr(mixer, "_spawn", lambda target: target())
    mixer.reset()


def install(monkeypatch, *, run: FakeRun | None = None,
            http: FakeHttp | None = None) -> tuple[FakeRun, FakeHttp]:
    """Cut every outside-world seam.  Returns the two doubles."""

    fake_run = run if run is not None else FakeRun()
    fake_http = http if http is not None else FakeHttp()
    monkeypatch.setattr(probes, "run", fake_run)
    monkeypatch.setattr(probes, "request", fake_http)
    reset_backends(monkeypatch)
    # Belt and braces: internet_ok() falls through to a REAL socket connect when
    # nmcli cannot answer, and a unit test must never touch the network.
    monkeypatch.setattr(probes, "_tcp_reachable", lambda url: False)
    return fake_run, fake_http


def base_env(monkeypatch, *, pin: str | None = PIN, token: str = "",
             state_file: str | None = None) -> None:
    for key in ADMIN_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    if pin is not None:
        monkeypatch.setenv("EMORI_ADMIN_PIN", pin)
    monkeypatch.setenv("EMORI_ADMIN_SALT", SALT)
    monkeypatch.setenv("EMORI_ROBOT_NAME", "Example Robot")
    if token:
        monkeypatch.setenv("EMORI_ADMIN_TOKEN", token)
    if state_file:
        monkeypatch.setenv("EMORI_ADMIN_STATE_FILE", state_file)


# --------------------------------------------------------------------------
# a NetworkManager and an ALSA stack, for the host (v2, no daemon) backend
# --------------------------------------------------------------------------
V2_STATUS = {"state": "running", "backend": "reachy_mini_ros2", "no_media": True}
NOT_FOUND = {"detail": "Not Found"}


def _cmd_ok(stdout: str = "") -> probes.Cmd:
    return probes.Cmd(ok=True, ran=True, stdout=stdout)


def _cmd_fail(returncode: int, stderr: str = "") -> probes.Cmd:
    return probes.Cmd(ran=True, returncode=returncode, stderr=stderr,
                      error="exit_%d" % returncode)


def terse(*fields: str) -> str:
    """One row the way ``nmcli -t`` prints it: ``:`` and ``\\`` escaped."""

    return ":".join(f.replace("\\", "\\\\").replace(":", "\\:") for f in fields)


class FakeNM:
    """A NetworkManager with one radio (``wlan0``), standing in for ``probes.run``.

    It understands exactly the argv emori-admin's host backend runs and keeps
    state across them (profiles, the active one), so a failed join really
    leaves a profile behind for the code to delete. Any OTHER root command is
    recorded in ``unknown_root`` and answers like a missing binary: a test can
    assert that nothing unexpected ran as root.
    """

    LIST_FIELDS = "IN-USE,SSID,BSSID,MODE,CHAN,FREQ,RATE,SIGNAL,SECURITY"

    def __init__(self, *, saved=(), active=None, hotspot=False, scan=()) -> None:
        self.profiles: dict[str, dict] = {}
        for name in saved:
            self.add_profile(name)
        if hotspot:
            self.add_profile("Hotspot", ssid="emori-setup-ap", hotspot=True)
        self.active = active
        self.scan = list(scan)      # (in_use, ssid, bssid, signal, security)
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.timeouts: list[float] = []
        self.unknown_root: list[list[str]] = []
        self.missing = False
        # sudo -n -l: False = every grant is there; True = sudo says no (no fragment).
        self.sudo_denied = False
        self.join_fails: tuple[int, str] | None = None
        self.rescan_fails: list[tuple[int, str]] = []
        self.up_fails: dict[str, tuple[int, str]] = {}

    def add_profile(self, name: str, *, ssid: str | None = None, psk: str = "",
                    hotspot: bool = False) -> None:
        self.profiles[name] = {"uuid": "uuid-%d" % (len(self.profiles) + 1),
                               "ssid": ssid or name, "psk": psk, "hotspot": hotspot}

    def connections_text(self) -> str:
        rows = [terse("Wired connection 1", "uuid-eth", "802-3-ethernet", "eth0")]
        for name, profile in self.profiles.items():
            rows.append(terse(name, profile["uuid"], "802-11-wireless",
                              "wlan0" if self.active == name else ""))
        return "\n".join(rows) + "\n"

    def list_text(self) -> str:
        rows = []
        for in_use, ssid, bssid, signal, security in self.scan:
            rows.append(terse(in_use, ssid, bssid, "Infra", "6", "2437 MHz",
                              "130 Mbit/s", str(signal), security))
        return "\n".join(rows) + ("\n" if rows else "")

    def root_calls(self) -> list[list[str]]:
        """Commands RUN as root (a ``sudo -n -l`` only asks; see ``listed``)."""

        return [c for c in self.calls if c[:3] == ["sudo", "-n", "nmcli"]]

    def listed(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["sudo", "-n", "-l"]]

    def __call__(self, argv, *, timeout_s=probes.CMD_TIMEOUT_S, runner=None, env=None):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(env)
        self.timeouts.append(timeout_s)
        if self.missing:
            return probes.Cmd(error="missing")
        if argv[:4] == ["sudo", "-n", "-l", "nmcli"]:
            # Asked, never run: sudo prints the command it would allow, or refuses.
            if self.sudo_denied:
                return _cmd_fail(1, "Sorry, user pollen is not allowed to execute "
                                    "'/usr/bin/nmcli ...' as root on reachy-mini.")
            return _cmd_ok("/usr/bin/nmcli %s\n" % " ".join(argv[4:]))
        if argv[:3] == ["sudo", "-n", "nmcli"]:
            return self._root(argv, argv[3:])
        if argv[:1] == ["nmcli"]:
            return self._read(argv[1:])
        return probes.Cmd(error="missing")

    def _read(self, args: list[str]) -> probes.Cmd:
        if args == ["-t", "-f", "NAME,UUID,TYPE,DEVICE", "connection", "show"]:
            return _cmd_ok(self.connections_text())
        if args[:5] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"]:
            return _cmd_ok(terse("wlan0", "wifi", "connected" if self.active else
                                 "disconnected", self.active or "") + "\n")
        if args[:4] == ["-t", "-g", "802-11-wireless.ssid", "connection"]:
            profile = self.profiles.get(args[-1])
            return _cmd_ok(profile["ssid"] + "\n") if profile else _cmd_fail(10)
        return probes.Cmd(error="missing")  # other read-only probes: absent here

    def _root(self, argv: list[str], args: list[str]) -> probes.Cmd:
        if args == ["device", "wifi", "rescan"]:
            if self.rescan_fails:
                return _cmd_fail(*self.rescan_fails.pop(0))
            return _cmd_ok()
        if args == ["-t", "-f", self.LIST_FIELDS, "device", "wifi", "list"]:
            return _cmd_ok(self.list_text())
        if args[:3] == ["connection", "up", "id"] and len(args) == 4:
            name = args[3]
            if name not in self.profiles:
                return _cmd_fail(10, "Error: unknown connection '%s'." % name)
            if name in self.up_fails:
                return _cmd_fail(*self.up_fails[name])
            self.active = name
            return _cmd_ok("Connection successfully activated\n")
        if args[:3] == ["connection", "delete", "id"] and len(args) == 4:
            name = args[3]
            if name not in self.profiles:
                return _cmd_fail(10, "Error: unknown connection '%s'." % name)
            del self.profiles[name]
            if self.active == name:
                self.active = None
            return _cmd_ok("Connection '%s' successfully deleted.\n" % name)
        if args[:3] == ["device", "wifi", "connect"] and len(args) in (4, 6):
            ssid = args[3]
            psk = args[5] if len(args) == 6 and args[4] == "password" else ""
            # NetworkManager keeps the profile even when the activation fails --
            # which is why the daemon deletes it afterwards -- and the radio has
            # left whatever it was on (the hotspot included) by then.
            self.add_profile(ssid, psk=psk)
            if self.join_fails is not None:
                self.active = None
                return _cmd_fail(*self.join_fails)
            self.active = ssid
            return _cmd_ok("Device 'wlan0' successfully activated with 'uuid-x'.\n")
        if args[:4] == ["device", "wifi", "hotspot", "con-name"] and len(args) == 9:
            name, ssid, psk = args[4], args[6], args[8]
            self.add_profile(name, ssid=ssid, psk=psk, hotspot=True)
            self.active = name
            return _cmd_ok("Device 'wlan0' successfully activated with 'uuid-h'.\n")
        self.unknown_root.append(argv)
        return probes.Cmd(error="missing")


APLAY_L = """\
**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 2: Audio [Reachy Mini Audio], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""

AMIXER_SCONTENTS = """\
Simple mixer control 'PCM',0
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 127
  Front Left: Playback 104 [82%] [-5.75dB] [on]
  Front Right: Playback 104 [82%] [-5.75dB] [on]
Simple mixer control 'PCM',1
  Capabilities: pvolume pswitch pswitch-joined
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 127
  Front Left: Playback 127 [100%] [0.00dB] [on]
  Front Right: Playback 127 [100%] [0.00dB] [on]
Simple mixer control 'Mic',0
  Capabilities: cvolume cswitch cswitch-joined
  Capture channels: Front Left - Front Right
  Limits: Capture 0 - 31
  Front Left: Capture 19 [61%] [12.00dB] [on]
  Front Right: Capture 19 [61%] [12.00dB] [on]
"""


def amixer_get(percent: int, kind: str = "Playback") -> str:
    return ("Simple mixer control 'X',0\n  Capabilities: pvolume\n"
            "  Front Left: %s 100 [%d%%] [0.00dB] [on]\n"
            "  Front Right: %s 100 [%d%%] [0.00dB] [on]\n" % (kind, percent, kind, percent))


class FakeAlsa:
    """``aplay``/``amixer``/``pw-play`` for the host volume backend.

    Levels are kept per (control, index), so a set is visible to the next get.
    Unknown commands answer like a missing binary.
    """

    def __init__(self, *, aplay: str = APLAY_L, scontents: str = AMIXER_SCONTENTS) -> None:
        self.aplay = aplay
        self.scontents = scontents
        self.levels: dict[tuple[str, str], int] = {("PCM", "0"): 82, ("Mic", "0"): 61}
        self.calls: list[list[str]] = []
        self.missing: set[str] = set()
        self.unstartable: set[str] = set()   # installed, but the exec fails (os_error)
        self.failing_controls: set[str] = set()

    def played(self) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["pw-play"]]

    def __call__(self, argv, *, timeout_s=probes.CMD_TIMEOUT_S, runner=None, env=None):
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] in self.missing:
            return probes.Cmd(error="missing")
        if argv[0] in self.unstartable:
            return probes.Cmd(error="os_error")
        if argv == ["aplay", "-l"]:
            return _cmd_ok(self.aplay)
        if argv[0] == "pw-play":
            return _cmd_ok()
        if argv[0] != "amixer":
            return probes.Cmd(error="missing")
        args = argv[1:]
        if args[:1] == ["-c"]:
            args = args[2:]
        if args == ["scontents"]:
            return _cmd_ok(self.scontents)
        verb, target = args[0], args[1]
        name, _, index = target.partition(",")
        if name in self.failing_controls:
            return _cmd_fail(1, "amixer: Unable to find simple control '%s',%s"
                             % (name, index))
        if verb == "sget":
            kind = "Capture" if name == "Mic" else "Playback"
            return _cmd_ok(amixer_get(self.levels.get((name, index), 0), kind))
        if verb == "sset":
            self.levels[(name, index)] = int(args[2].rstrip("%"))
            return _cmd_ok()
        return probes.Cmd(error="missing")


def backend_error_words() -> set[str]:
    """Every error word a Wi-Fi/volume call can come back with, read from the source.

    ``NmcliError(msg, "<slug>")``, ``MixerError("<slug>")`` and ``error="<slug>"`` in
    netman/mixer, plus the selector's ``route_missing`` and the unreachable
    ``daemon_down``. Read with ast, so a new slug in either module shows up here (and
    in every test that holds the phone page and readycheck to it) without anybody
    remembering to list it.
    """

    import ast
    import inspect

    words = {host_backend.ROUTE_MISSING, "daemon_down"}
    for module in (netman, mixer):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", ""))
            candidates = []
            if name == "NmcliError" and len(node.args) == 2:
                candidates.append(node.args[1])
            if name == "MixerError" and node.args:
                candidates.append(node.args[0])
            candidates.extend(k.value for k in node.keywords if k.arg == "error")
            for candidate in candidates:
                if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str) \
                        and candidate.value:
                    words.add(candidate.value)
    return words
