# Pokamind session contract (robot side)

> **Scope.** What the robot receives from a **live Pokamind session** and what it
> sends back, as seen from the robot. Pokamind is a separate service; this repository
> contains only the robot-side client. The authoritative parser is
> `src/emori_adapter/sources/websocket.py`; the event names are its constants.
> Section 7 is the **provisioning contract**: how a robot learns its own configuration
> from its Pokamind record (`GET /api/robot/whoami`, contract 2).

---

## 1. What reaches the robot

During a session Pokamind analyses the learner on a **~2 second cycle**. Two channels
of that analysis cross into the robot's page:

| Channel | What | Relayed to EMORI? |
|---|---|---|
| Communication styles | **31 canonical styles**, `{style: 0..100}`, a rolling distribution | **yes** — the full dense distribution, every cycle |
| Body language | categorical posture / head / gesture / energy fields + engagement/confidence scores | **yes** — 4 categorical fields + 2 scores |
| Other analysis channels | used inside Pokamind for its own report | **no** — never sent to the robot per cycle |

EMORI targets the 31 styles listed in `src/emori_adapter/constants.py::COMM_STYLES`.
**Only the styles and body channels cross the iframe boundary** — the wire envelope
is deliberately thin (see §3).

## 2. The transport — iframe `postMessage` → hostapp hub

The Pokamind session runs in an `<iframe>` on the robot's own kiosk page, and posts
each finalised cycle to that **parent page** — in EMORI the **robot's hostapp is that
parent** (the "partner"):

```
Reachy Mini kiosk — hostapp (`emori-hostapp`, the "partner")
   │  1. /session embeds Pokamind in an <iframe> via JWT SSO
   │  2. Pokamind postMessage()s each analysis cycle / AI utterance / completion
   │  3. the /session page forwards every pokamind_* postMessage to POST /api/cycle
   ▼
POST /api/cycle — THE hub: stamps the active session's identity, then fans out to
   ├─ the in-process robot driver   (Translator → BehaviorEngine → RobotPlayer → daemon)
   ├─ FIWARE                        (per-session NGSI-LD entities, per-cycle historization)
   ├─ the /ws/relay broadcast       (consumed by the ROS 2 live publisher and `emori-run --source pokamind`)
   ├─ the JSONL session recorder    (deterministic replay fixtures)
   └─ the session tracker           (spoken summary + results email at completion)
```

The hostapp is **the single robot driver** during a live session. `/ws/relay` has
one producer — `/api/cycle` broadcasts every stamped envelope — and its consumers
(the ROS 2 publisher with `source:=pokamind`, `emori-run --source pokamind`, any
debugger) are **observers**: they never drive the robot a second time.

### 2.1 Messages the session page handles

The `/session` page (`src/emori_adapter/hostapp/pages.py`) forwards by message
**type** (the `pokamind_` prefix), because after the SSO redirect the session
document runs on a different origin from the one the frame was opened on. Any
`pokamind_*` message also proves the frame came up, which disarms the
session-start deadline.

| Type | Meaning | Where it goes |
|---|---|---|
| `pokamind_analysis_update` | one analysis cycle (§3.1) | `/api/cycle` |
| `pokamind_ai_utterance` | one sentence of the AI's speech (§3.3) | `/api/cycle` |
| `pokamind_tts_ended` | the AI voice finished speaking a turn; the first one (end of the scenario intro) also makes the page send `wake` | `/api/cycle` |
| `pokamind_turn_armed` / `pokamind_turn_submitted` / `pokamind_turn_rejected` | the learner's turn opened / was submitted / was refused | `/api/cycle` (watchdog and counters) |
| `pokamind_session_completed` | the session ended (§3.4) | `/api/cycle` |
| `pokamind_auth_failed` | Pokamind could not sign the learner in | the page's session-start failure path |
| `pokamind_emori_vad` | the in-frame voice-activity detector's state | shown on the page; a throttled `pokamind_user_speaking` goes to `/api/cycle` while the learner speaks |
| `pokamind_emori_dbg` | lifecycle and debug stages | `/api/emori-debug` (journal) |

### 2.2 Commands the page sends into the frame

`{type: "emori_cmd", cmd}`, posted to the session frame:

| `cmd` | Sent when |
|---|---|
| `wake` | begin the learner's turn: the scenario intro finished, a head nudge, or the `B` key |
| `endpoint` | submit the current answer (`S` key; normally the in-frame endpointer does this itself) |
| `end` | end the session: a push of the head back or a head nudge while active, a watchdog, or the `E` key |
| `reprompt` | ask the frame to repeat the prompt |
| `tt_patch` | turn-taking tuning overrides; posted only to explicit target origins, never `"*"`, and repeated until the frame acknowledges it |

## 3. The contract — what actually crosses the wire

### 3.1 `pokamind_analysis_update` (one per ~2 s cycle — note how THIN it is)

```js
window.parent.postMessage({
  type: "pokamind_analysis_update",
  communication_styles: { "mirroring": 3.2, ... },  // all 31 styles, 0..100, dense (sum ~100)
  body_language: {
    posture: "leaning_forward",   // leaning_forward|leaning_back|slouched|stiff|upright|relaxed|not_visible
    head_position: "straight",    // nodding|shaking|tilted_left|tilted_right|down|straight|not_visible
    gesture_type: "open_palm",    // open_palm|pointing|relaxed|not_visible
    overall_energy: "medium",     // high|medium|low
    engagement_score: 72,         // 0..100
    confidence_score: 65          // 0..100
  },
  body_language_metrics: { engagement: 72, confidence: 65 },
  seq: 12,
  timestamp: "2026-01-01T10:30:24.000Z"
}, "<robot-host-origin>");   // a specific origin, not "*", for per-cycle data
```

That is the **whole** envelope. There are **no** `facial_metrics`, `tone_metrics`,
`verbal_metrics`, `prosody`, `transcript_delta`, `meta`, `session_id` or
`person_id` fields on the wire.

Two facts the parser depends on:

- **Any `body_language` / `body_language_metrics` value may be `null`.** Whenever
  Pokamind's body analysis fails on a frame (the learner looks away), every body
  value arrives as `null`. `sanitize_envelope()`
  (`src/emori_adapter/sources/websocket.py`) drops nulls so a no-face frame degrades
  to a styles-only cycle instead of failing `/api/cycle` and freezing the robot.
- **The body vocabulary is Pokamind's, not ours.** The adapter folds the live
  values onto its canonical vocab via `constants.BODY_VALUE_NORMALIZE`
  (`leaning_forward→lean_forward`, `straight→upright`, `tilted_left/right→tilted`,
  `medium→moderate`, ...); values with no behavioural meaning (`stiff`, `relaxed`,
  `not_visible`) pass through untouched.

### 3.2 Hostapp-stamped fields (added at `/api/cycle`, **not** sent by Pokamind)

`LiveDriver.stamp()` (`src/emori_adapter/hostapp/driver.py`) stamps the active
session's identity onto **every** envelope before recording, broadcasting, or
driving the robot:

| Field | Stamped from |
|---|---|
| `session_id` | minted at SSO time: `<student>-<utctimestamp>` |
| `person_id` | the roster identity (QR badge / voice walk-in) |
| `lang` | the session's `EMORI_LANG` (the same value pinned via the SSO JWT's `emori_lang` claim) |
| `meta.topic` | the verbally chosen module's topic name |

The stamped identity always wins over anything in a payload — live envelopes carry
no identity of their own, and a stale payload id must never override the active
session. Downstream consumers (the ROS 2 live publisher, the recorder) never have
to guess.

### 3.3 `pokamind_ai_utterance` (per assistant sentence)

```js
{ type: "pokamind_ai_utterance", text: "...", seq, timestamp,
  cue_category, cue_body, source }   // the last three optional
```

No audio URL — the language comes from the session's `emori_lang`, and audio plays
through the kiosk Chromium. Each spoken sentence arrives once; the hostapp's
`UtteranceDeduper` guards only against transport-level double delivery (same `seq`,
source, cue and normalized text within 1.5 s), so a roleplay character legitimately
repeating an identical line (new `seq`) still gets its talking gesture, and a sentence
that is only a cue is delivered. `seq` is not globally
monotonic: the scenario intro and the conversation turns number their sentences
independently. `cue_category` / `cue_body`, when present, tag the sentence's
expression; the hostapp bounds both (they arrive from a cross-origin frame).

### 3.4 `pokamind_session_completed`

```js
{ type: "pokamind_session_completed", partner, session_id, status: "completed", results }
```

Ends the stream. The hostapp finalizes the session: farewell gesture, localized
spoken summary, FIWARE summary attributes, results email — and `/ws/relay`
consumers end their session loop. `results` is present only when the session was
signed in with the `emori_kiosk` claim (§4).

## 4. Signing in: the partner SSO token the hostapp mints

The hostapp opens the session frame at
`<POKAMIND_BASE>/partner/sso/login?token=<jwt>&partner=emori-robot`
(`build_sso_url` in `src/emori_adapter/hostapp/jwt_sso.py`). With a robot token the
JWT is signed with the robot's own key (EdDSA, `kid` = robot id,
`aud=pokamind-partner-sso`, 600 s lifetime, unique `jti`). A robot without a token
signs nothing: it serves demo sessions only and never opens this URL.

| Claim | Value |
|---|---|
| `student_id`, `email`, `name` | the learner: a roster badge, or the robot's own `robot-<robot-id>@emori.local` for a drop-in |
| `course_id`, `pathway`, `module_id` | the course, the badge pathway, and the module picked from the spoken menu |
| `emori_lang` | the kiosk language; pins the session language |
| `emori_kiosk` | `true`: the session runs on a screenless kiosk |
| `robot_id`, `aud`, `jti`, `iat`, `exp` | set by the robot token itself; `extra` claims can never override them |

`emori_kiosk` asks Pokamind for the kiosk behaviour the robot relies on: the
device-check step advances on its own, the in-frame turn-taking hooks are active
(the endpointer, the `emori_cmd` commands, the `pokamind_tts_ended` and turn
events), and `pokamind_session_completed` carries `results` for the spoken summary
and the results email. The first turn still starts only after the scenario intro
has finished playing, so the microphone never opens over the first question.

## 5. Privacy on the robot

- The analysis relay stays on the robot's localhost (session page →
  `/api/cycle` → local consumers); no learner analysis leaves the device except
  the metrics the FIWARE layer is configured to historize.
- The page forwards only `pokamind_*` messages, the hostapp bounds the text fields
  it accepts from them, and the page posts any command that carries data to
  explicit target origins only.

## 6. Field mapping summary

`AnalysisCycle` (this repo) ← the live envelope. See `configs/mapping.yaml` and
`src/emori_adapter/constants.py` for the authoritative lists.

| AnalysisCycle field | Live source |
|---|---|
| `communication_styles` | the dense 31-style distribution (every cycle) |
| `body_language` | `posture`, `head_position`, `gesture_type`, `overall_energy` + `engagement_score`/`confidence_score` (any value nullable; normalized via `BODY_VALUE_NORMALIZE`) |
| `body_language_metrics` | `{engagement, confidence}` (nullable) |
| `session_id` / `person_id` / `meta.topic` | **stamped by the hostapp** at `/api/cycle` (§3.2) |
| `facial_metrics` / `tone_metrics` / `verbal_metrics` / `prosody` / `transcript_delta` | **mock-only** (`MockPokamindSource` `shape="rich"`) — always empty on the live path |

Because the live style distribution is dense (each style ~3–10%), style channels
are scored as **deviations from a per-session running mean** (`GroupBaseline`;
tune via the `translation:` section of `configs/behavior.yaml` —
`style_gain`/`style_alpha`/`style_warmup`), with engagement bands defaulting to
0.62/0.38 (`engagement_high`/`engagement_low`).

Downstream, these become `hri_msgs/EngagementLevel` on
`/humans/persons/<id>/engagement_status`, the `emori_msgs/Context` on
`/humans/persons/<id>/context`, and the FIWARE `Session` entity — see
[ros4hri-mapping.md](ros4hri-mapping.md) and [architecture.md](architecture.md).

The ROS-side reader of the same relay is `PokamindBridgeSource(ws_url=...)`, which
yields `AnalysisCycle`s for **observers**:
`emori-run --source pokamind --ws-url ws://localhost:8080/ws/relay` (or set
`POKAMIND_WS_URL`); the ROS 2 publisher's `source:=pokamind` uses the same source.
The hostapp also has a **demo mode** that synthesises cycles so the whole loop runs
without a live Pokamind.

## 7. Provisioning contract (robot side)

A robot deployed from this repository configures itself from **its own Pokamind
record**. The only Pokamind input a person gives it is the robot's token
(`pkr1.<robot-id>.<key>`); everything else in this section is what the robot then
asks Pokamind, what it accepts, and what it does with the answer. The robot-side
code is `src/emori_adapter/hostapp/pokamind_whoami.py` (the client and its parser),
`src/emori_adapter/hostapp/pokamind_scope.py` (the sync and the snapshot),
`src/emori_adapter/site_settings.py` (what is derived, and the precedence) and
`src/emori_adapter/hostapp/menu_builder.py` (the spoken menus).

### 7.1 The request

```
GET {POKAMIND_BASE}/api/robot/whoami
Authorization: Bearer <EdDSA assertion: kid = robot_id, aud = pokamind-robot-control, exp - iat = 45 s>
Accept: application/json
X-EMORI-Contract: 2
```

`POKAMIND_BASE` is the Pokamind API origin (default `https://api.pokamind.com`);
it must be `https` (plain `http` only for a loopback host). The assertion is signed
with the robot's own token, so the request needs no other credential.

`X-EMORI-Contract: 2` opts the robot into the contract-2 response keys. The server
rule the robot relies on: without the header, or with any value other than exactly
`2`, the answer is the v1 body, byte for byte. With `2` the server **may** add any
subset of the four optional keys in §7.3. It never removes, renames or retypes a v1
key and never adds any other key. Every robot running this release sends the header
and accepts both bodies.

### 7.2 The v1 response (200)

| Key | Type | Notes |
|---|---|---|
| `robot_id` | string | must equal the signing token's id, else `whoami_robot_id_mismatch` |
| `display_name` | string | |
| `status` | `"active"` \| `"revoked"` | only `active` is ever recorded |
| `credential` | `{kind: "fleet"\|"registry", key_fingerprint: string\|null, key_version: integer >= 0\|null}` | exact key set. `fleet`: a per-robot token, Pokamind holds its Ed25519 public key; `registry`: an older server-side robot entry with no per-robot key (fingerprint `null`). A fleet fingerprint must be the token's, else `whoami_key_mismatch` |
| `org` | string | |
| `partner` | string | compared only (readycheck `partner_mismatch` against an explicit `EMORI_PARTNER_NAME`) |
| `course_id` | string | `""` = no course |
| `languages` | [string] | **order = preference order** |
| `pathways` | [string] | exact category strings |
| `identity_domains` | [string] | the e-mail domains a person on the roster may have |
| `modules` | [module] | **order = menu order** within each language |
| `features` | flat map of named booleans (at most 16; names `^[a-z][a-z0-9_]{0,31}$`) | the kiosk reads none; an installed add-on may pin the exact names it needs |

A module row: `module_id` (`^[A-Za-z0-9_-]{1,1500}$`), `title`, `category` and
`language` (string or null), `available` (bool), `problem` (`not_found` \|
`not_visible_to_robot` \| `language_mismatch` \| null).

Every string is at most 500 characters and every list at most 500 items; booleans
must be real booleans.

### 7.3 Contract-2 keys (optional, advisory)

| Where | Key | Valid value | When absent or malformed |
|---|---|---|---|
| top level | `default_language` | lowercase base code `^[a-z]{2,3}$`, one of `languages` (by base code) when that list is not empty | the first `languages` entry the robot ships (`ca`, `en`), else the robot's default |
| top level | `app_origin` | `https://<host>[:port]` (`http` only for `localhost`, `127.0.0.1`, `::1`), no path, query, fragment or userinfo, at most 200 characters | the web-app origin is derived from `POKAMIND_BASE` |
| module row | `menu_name` | 1-60 characters, no comma, CR, LF or other control character | the robot drafts a name from `title` |
| module row | `menu_keywords` | at most 12 strings of 1-40 characters, none with a comma, CR or LF | derived from the name |

Each key may also be `null`, which means absent.

### 7.4 How the robot parses it

- **Required keys are strict**, exactly as in v1: every one must be present with its
  type, and `credential` is an exact key set, and `features` is a map of named booleans.
- **The optional keys are advisory.** A malformed value is treated as absent and its
  key **name** (for example `app_origin` or `modules[3].menu_name`) is recorded in
  the parse result's `ignored_keys`. It never invalidates the body.
- **An unknown key still invalidates the whole body** (`invalid_whoami_response`):
  a key that is neither required nor one of the four optional keys, or an optional key
  in the wrong place (for example `menu_name` at the top level).
- The parse result records `contract: 2` when the body carried any contract-2 key
  (valid or not, even `null`), else `contract: 1`.
- Nothing from a body is ever logged. Every failure is a fixed code (§7.9).

### 7.5 Order semantics

- `languages` lists the record's languages in preference order. The kiosk offers the
  ones the robot ships (`ca`, `en`) in that order; a record that lists none of them
  restricts nothing.
- `modules` lists the modules in menu order. Per kiosk language, the robot offers the
  modules with `available: true` and `problem: null` whose `language` base code
  (lower-cased, cut at `-` or `_`) is that language, in record order, **at most six**
  (spoken numbers stop at six). The modules already on the menu come first, in their
  previous order, and new ones are numbered after them; when a module leaves the
  scope, every later option moves up a number (the robot speaks the whole menu every
  time, so nobody has to remember a number).

### 7.6 What the robot derives, and what wins

A verified answer supplies these values, **only to settings left unset on the
robot**:

| From | Setting | Rule |
|---|---|---|
| `course_id` | `EMORI_COURSE_ID` | when non-empty; the course claim of drop-in sessions and of badges whose roster names no course |
| `pathways` | `EMORI_PATHWAYS` | joined by commas; a pathway that itself holds a comma is skipped (and counted in the log). The People tab offers only these |
| `display_name` | `EMORI_ROBOT_NAME` | when non-empty and not just the robot id |
| `default_language`, `languages` | `EMORI_LANG` | the kiosk's boot language (§7.3) |
| `modules` (+ `menu_name`, `menu_keywords`) | `configs/site/auto/voice_topics.<lang>.yaml` | rebuilt on every verified sync and served live, no restart |
| `identity_domains` | People tab | an e-mail outside them is refused |
| `app_origin` | the session page's `postMessage` target origins | |

**Precedence**, for every setting: admin-saved > explicit `.env` > derived from
Pokamind > hardware profile > code default. An empty value means unset.
`EMORI_SITE_AUTO=0` switches every Pokamind-derived value off (the menus then come
from the site, fork, development or built-in tiers). A robot whose `.env` sets the
course, the pathways, the name, the language and its menus behaves exactly as before.
See [configuration.md](configuration.md#where-each-setting-comes-from).

### 7.7 When the robot asks

Each whoami is a signed, single-use request and Pokamind rate-limits them, so the
robot asks a bounded number of times:

- once **10 s after the hostapp starts**, then **every 6 hours**; a failed sync is
  retried after **10 minutes** and then **30 minutes**, after which the 6-hour rhythm
  resumes;
- on the admin page's **Sync now** and **Verify** (Verify records the answer it
  already has);
- when a token is **replaced** on the admin page: the new token's verified answer is
  recorded **before** the token file is saved;
- when a token is **installed by the deploy** (`install-token.sh` runs
  `python -m emori_adapter.hostapp.token_verify --token-file <candidate>
  --write-scope`): only a verified token's answer is recorded, before the token is
  moved into place.

Without a token the robot never asks.

### 7.8 The snapshot, and offline behaviour

A verified answer (`status: active`, about this robot, holding this key) is written
to `configs/site/pokamind_scope.json` (mode 0600, atomic) with: the schema version,
when it was fetched, the robot id, the token's key fingerprint and the record's key
version, the contract, a `sha256` digest of the answer, the parsed answer itself, the
menu numbering and any state an installed add-on stores with the snapshot. The robot reads the snapshot at
start; it ignores one about another robot id, or one taken with another key when both
fingerprints are known (a replaced token).

A failed, refused or offline sync changes nothing: the **last-good snapshot stays**
and the robot keeps running on it. readycheck warns when there is none
(`scope_not_synced`) and when it is older than seven days (`scope_stale`).

A sync never restarts anything. Menus change live. A derived value that needs a
restart (for example the course changing) is reported as a restart
reason (`scope_changed`, or one an add-on adds), and only for settings no local
value overrides; the restart itself happens when the robot is idle.

### 7.9 Failure codes

| Code | Meaning |
|---|---|
| `ok` | answered and verified |
| `no_robot_token` | this robot has no token: nothing is asked |
| `whoami_base_url_invalid` | `POKAMIND_BASE` is not an `https` origin (or a loopback `http` one) |
| `whoami_signing_unavailable` | the token could not sign (cryptography missing) |
| `whoami_unreachable`, `whoami_timeout` | no answer (also a captive portal or proxy answering 401/403 without Pokamind's JSON) |
| `whoami_auth_failed` | Pokamind refused the assertion (rotated key, another backend, clock off) |
| `robot_revoked`, `robot_unknown` | Pokamind's own refusal codes |
| `whoami_not_supported`, `whoami_rate_limited`, `whoami_unavailable`, `whoami_server_error`, `whoami_rejected` | 404, 429, 503, other 5xx, other 4xx |
| `invalid_whoami_response` | the body broke §7.4 |
| `whoami_robot_id_mismatch`, `whoami_key_mismatch` | the answer is about another robot, or names another key |
| `robot_not_active` | answered, but `status` is not `active`: not recorded |
| `scope_write_failed`, `scope_record_failed` | the snapshot could not be written; the last-good one stays |

The snapshot itself reports `scope_missing`, `scope_unreadable`,
`scope_schema_unknown`, `scope_robot_mismatch` or `scope_fingerprint_mismatch` when
it cannot be used.

### 7.10 Future additions

New whoami response keys travel behind `X-EMORI-Contract: 3` (and so on), so the
server can ship first. A response key added without a header gate must ship on the
robot first, because the robot refuses unknown keys. A new **request** key ships on
the server first, because a server that does not know a request field refuses the
request.
