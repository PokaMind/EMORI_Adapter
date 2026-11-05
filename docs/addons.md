# Writing and deploying an add-on

The training kiosk runs complete with no add-on installed. An add-on is a separately
installed Python package that adds a mode which shares the robot with training sessions
(its own routes, its own settings, its own admin tab). Everything an add-on can
touch goes through one module, `src/emori_adapter/hostapp/extensions.py`, so the kiosk
never imports an add-on by name.

This page is for the author of such a package.

## Discovery

Publish a class under the `emori_adapter.extensions` entry-point group:

```toml
[project.entry-points."emori_adapter.extensions"]
my_addon = "my_addon.extension:MyExtension"
```

- At most one add-on is loaded: the first entry point by name. A second is logged and
  ignored.
- `EMORI_ADDONS=off` (also `0` or `false`) skips discovery. The kiosk then behaves exactly
  as if nothing were installed.
- The entry-point module is imported by the hostapp, by the admin service and by the
  readiness check, and `emori_adapter.site_settings` imports it while it is itself being
  imported. Keep the module light: import media and network stacks inside `attach`, and
  import `emori_adapter` modules inside functions, not at module level.
- A broken add-on never takes the kiosk down. An import or construction failure is logged
  and the `HostExtension` defaults apply: every badge is accepted and the robot is never
  held.

## `HostExtension`: the live hooks

Subclass `emori_adapter.hostapp.extensions.HostExtension`. Every hook has a "no add-on
here" default. `attach` runs once while the app is built; the others may run on any thread.

| Hook | When it runs | Default |
|---|---|---|
| `attach(ctx)` | Once, before any other hook. `ctx` is an `ExtensionContext`: `driver`, `gesture_ticker`, `lang_state`, `mic_claim(bool)` (raise the capture gain while the add-on holds the microphone), `session_alive()` (is a training session really exchanging turns, or only navigated to), `overrides` (the test overrides of `_build_app`) and `ros` (`None` unless the hub hosts the app with its facade nodes) | nothing |
| `async start()` | Lifespan startup, on the event loop. A failure leaves training available | nothing |
| `async shutdown()` | Lifespan teardown, before the hostapp's own workers stop | nothing |
| `on_head_turn(direction)` | A signed head turn (`gestures.GestureDirection`) while no session runs | nothing |
| `on_head_back()` | The head was pushed back and held while no session runs. Answer True only if that ended a mode, so the robot acknowledges only an exit that happened | `False` |
| `selection_reserved()` | Asked by the gesture detector: `True` while a badge scan owns the head, so the detector stands down | `False` |
| `busy()` | `True` while the add-on's mode runs: the kiosk then refuses a language switch or a service restart | `False` |
| `claim_badge(payload)` | A decoded badge asks to start a training session. Answer `BADGE_OK`, `BADGE_REFUSED` or `BADGE_PENDING`; only `BADGE_OK` lets `POST /api/badge/claim` accept it | `BADGE_OK` |
| `bind_session(session_id)` | A session was minted for a claimed badge. Only `True` accepts it | `True` |
| `session_opened(session_id)` | The kiosk opened that session's page. Only `True` accepts it | `True` |
| `session_ended(session_id=None)` | A training session ended, or its mint expired unopened | nothing |
| `status()` | Content-free state for `GET /api/addon/status` and the dashboard: `{"mode": str, "enabled": {name: bool}, ...}`, or `None` | `None` |
| `hint_lines(lang)` | Short lines for the landing page in `lang`, served by `GET /api/addon/hints` | `()` |
| `register_routes(app)` | Add HTTP routes to the hostapp (a FastAPI app) | nothing |
| `admin_api()` | An object the hostapp's `/api/admin/*` routes delegate the add-on's actions to, or `None` | `None` |

`extensions.claim_timeout_s()` returns the badge-scan-to-`/session` deadline
(`EMORI_TRAINING_NAV_TIMEOUT_S`, 10-600 s, default 60) for an add-on that waits on a claim.

## `contributed`: static classmethods

The settings layer, the admin service and the readiness check run before, or without, a
live hostapp. They read what an add-on offers by calling a **classmethod** on the entry-point
class through `extensions.contributed(hook, *args)`. It answers `None` when there is no
add-on, no such method, or the method raises. Each is optional.

| Classmethod | Called with | Returns |
|---|---|---|
| `settings_registry()` | nothing | A list of `site_settings.KeySpec` (build them with `site_settings.make_spec` and the `*_check` helpers; `verified_origin="<error>"` marks a Pokamind origin, saved only when whoami answers from it) the settings layer adds to its registry. Registered keys get the admin page's save, source labels and restart handling |
| `session_deadlines()` | nothing | `{key: (code default in s, least margin in s)}` for registered settings holding a deadline that must stay that far behind the later session-start deadline; a save breaking it is refused (`value_breaks_deadline_order`) |
| `hardware_defaults(profile, env)` | The hardware profile name and the environment so far | `{key: value}` supplied when the profile is installed and the key is empty |
| `pinned_settings(env)` | The environment so far | `{key: value}` shipped defaults for an installed robot |
| `scope_features(features, previous)` | The `features` map of the robot's Pokamind record and the previous snapshot (or `None`) | Truthy when the add-on is provisioned by the record; stored with the snapshot |
| `scope_settings(snapshot)` | The scope snapshot | `{key: value}` derived from the Pokamind record, for registered keys |
| `scope_restart_reasons(before, after, local_keys)` | Two snapshots and the keys a local value overrides | A list of restart-reason strings |
| `whoami_feature_keys()` | nothing | The exact set of names the add-on expects in `features`. Without it any small set of named booleans is accepted |
| `readiness_gates()` | nothing | A list of `readycheck.ContributedGate` the readiness check adds (`name`, `run`, `after`, `key`, `collect`, `voice_languages`, `clock_strict`) |
| `admin_addon()` | nothing | A descriptor the admin service reads, every attribute optional: `tab_id`, `tab_label`, `html`, `js` (the admin page's tab), `pin_settings` (keys whose change needs the PIN again), `auto_apply_reasons` (restart reasons applied unattended), `overview()`, `setup(kit)`, `register(app, kit)` |

## Deploying an add-on

Put the package at `addons/<name>/` in the checkout, with a `pyproject.toml`. The folder is
not part of the public repository (it is git-ignored), so an add-on stays private to
whoever holds it.

- **Shipping.** `Deploy-to-Robot` copies every `addons/<name>/` that holds a
  `pyproject.toml` to `/opt/emori/addons/<name>`, without tests, caches or version-control
  data. `--no-addons` copies none and leaves an add-on already on the robot installed;
  `--remove-addons` uninstalls every add-on on the robot and deletes `/opt/emori/addons`.
  Settings an add-on saved stay in `configs/site/settings.env` and `secrets/site.env`, in
  case it comes back; remove those lines by hand to wipe them.
- **Installing.** `deploy/robot/install.sh` runs `pip install -e addons/<name>` for each.
- **`deploy/install-hook.sh`** (optional). Runs once at install, after pip and before any
  unit starts, with `/opt/emori` as the working directory. Use it for what pip cannot do
  (system packages, seeded files). `env_ensure KEY value` and `env_set KEY value` are
  available to add a key to `.env`. A voice for another language:
  `bash scripts/get_voice_models.sh <lang>=<its path under rhasspy/piper-voices>`; the hostapp
  maps every voice under `voices/` at its next start. A failing hook is reported and does not stop the
  install.
- **`deploy/pre-start.sh`** (optional). Run by `deploy/robot/addon-prestart.sh` before every
  hostapp start. Use it to wait for something the hostapp opens once at startup. It should give up on its own after a bounded wait; the hostapp starts whether or not the
  hook succeeded.
- **`scripts/addon_smoke.py`** (optional). `deploy/robot/rig-smoke.sh` runs it after the
  core checks, with the arguments it was given. Print one `PASS`/`FAIL`/`SKIP` line per
  check, content-free (no tokens or session content), and exit 0, 1 (a check failed)
  or 2 (`--strict` and one skipped). `rig-smoke.sh --addons` runs only the add-ons' smoke
  tests, and `--only` runs only the core checks it names.

## What an operator can see

- `GET /api/addon/status` on the hostapp (`:8090`): the add-on's `status()`, or `503
  addon_unavailable` when there is none. `install.sh` prints it at the end.
- `GET /api/addon/hints`: `{"lines": [...]}`, empty with no add-on.
- `POST /api/badge/claim` is the public QR hand-over; an add-on takes part only through
  `claim_badge`, `bind_session`, `session_opened` and `session_ended`.
- The usage events an add-on's modes log appear on the dashboard as `ext_<mode>`.
- The admin page's Home and Health tabs show the add-on's overview and readiness gates.
