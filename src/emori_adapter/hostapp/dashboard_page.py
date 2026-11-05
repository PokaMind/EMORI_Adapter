"""HTML for the ARISE management dashboard (routes live in ``hostapp/dashboard.py``).

Same convention as ``hostapp/pages.py``: fully self-contained Python string
constants with ``__PLACEHOLDER__`` tokens substituted by the caller. No Jinja2,
no StaticFiles, no CDN, no web fonts — the robot venv is fixed and a local network is
routinely offline, so every byte the browser needs is in this file. Charts are
hand-rolled SVG built from JSON the page fetches itself: nothing is rendered
server-side, because ~0.9 of one core is all the CM4 has spare.

Visually this is a Pokamind product, not a robot debug page: warm off-white
canvas (#FFFBF8), flat white cards with a 1px brand-tinted hairline at 14px
radius, one dusty cornflower blue (#5C83BF) carrying the brand, and the
saturated status colours used *only* where a number passes or fails a target.
The radar uses the product's chart style (0.12 fill, 2px stroke, r3.5
white-stroked markers, #e8e8e8 web, alternating #fafafa/#fff rings).

``DASHBOARD_JS`` is exported separately from ``DASHBOARD_HTML`` on purpose: the
render functions are pure enough to be driven under node against real API JSON
(see ``tests/test_dashboard.py``), which is the only way to prove that an empty
store or a half-empty comparison renders "—" instead of ``NaN``.

Placeholders:
  ``__ROBOT__``    JSON string, the robot's display name
  ``__DEFLANG__``  JSON string, ``"en"`` or ``"ca"`` (the manager can toggle)
  ``__TARGETS__``  JSON object of the ARISE target percentages
  ``__ATTMIN__``   JSON number, the attention threshold the KPI counts against
  ``__ERROR__``    JSON string, login error key (``""``, ``"bad"``, ``"rate"``)
"""

from __future__ import annotations

# Shared chrome for all three pages. Deliberately tiny and duplicated into each
# constant rather than assembled at request time: these are served a handful of
# times a day and readability beats saving 2 KB of process memory.
_BASE_CSS = """
*{box-sizing:border-box}
:root{--bg:#FFFBF8;--card:#fff;--bd:rgba(76,110,162,.12);--bd2:rgba(76,110,162,.22);
 --pri:#5C83BF;--pri2:#7FA1D4;--prid:#4A6A9B;--ink:#333;--mut:#6B7280;--slate:#646E78;
 --ok:#10B981;--warn:#F59E0B;--bad:#EF4444;--amber:#D97706}
body{font-family:Nunito,'Segoe UI',Roboto,sans-serif;margin:0;background:var(--bg);
 color:var(--ink);font-size:15px;line-height:1.45;-webkit-text-size-adjust:100%}
a{color:var(--pri)}
a.btn{text-decoration:none;display:inline-block}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:20px}
button{font:inherit;cursor:pointer}
.btn{border:1px solid var(--pri);border-radius:10px;background:var(--pri);color:#fff;
 padding:9px 15px;font-weight:600;text-transform:none;transition:all .3s cubic-bezier(.4,0,.2,1)}
.btn:hover{background:var(--prid);border-color:var(--prid)}
.btn.ghost{background:transparent;border-color:var(--bd);color:var(--ink)}
.btn.ghost:hover{background:rgba(92,131,191,.04);border-color:var(--pri)}
input[type=password]{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--bd2);
 background:#fff;color:var(--ink);font:inherit}
input[type=password]:focus{border-color:var(--pri);outline:2px solid rgba(92,131,191,.3);outline-offset:1px}
.muted{color:var(--mut)}
.eyebrow{font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.08em;
 color:var(--slate);margin:0 0 5px}
.err{color:var(--bad);min-height:1.3em;font-size:14px}
.mark{width:40px;height:40px;border-radius:50%;background:#fff7f1;display:grid;
 place-items:center;margin:0 0 12px}
.mark i{width:16px;height:16px;border-radius:50%;background:var(--pri);display:block}
"""

# --------------------------------------------------------------------------
# login
# --------------------------------------------------------------------------
LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>EMORI dashboard</title><style>""" + _BASE_CSS + """
body{display:grid;place-items:center;min-height:100vh;padding:18px}
.card{width:min(94vw,392px)}
h1{font-size:1.25rem;font-weight:700;letter-spacing:-.01em;margin:0 0 2px}
h2{font-size:13px;margin:0 0 18px;font-weight:400;color:var(--mut)}
label{display:block;font-size:13px;font-weight:600;margin:0 0 6px;color:var(--slate)}
.sub{font-size:12px;color:var(--mut);margin-top:16px}
</style></head><body>
<form class=card method=post action="/dashboard/login">
<div class=mark><i></i></div>
<h1>EMORI &mdash; ARISE Management</h1>
<h2>Gesti&oacute; ARISE &middot; <span id=robot></span></h2>
<label for=pw>Management password / Contrasenya de gesti&oacute;</label>
<input id=pw name=password type=password autocomplete=current-password autofocus>
<p class=err id=err></p>
<button class=btn type=submit style="width:100%">Sign in / Entra</button>
<p class=sub>Shared management access. Sessions expire after 12 hours.<br>
Acc&eacute;s compartit de gesti&oacute;. La sessi&oacute; caduca al cap de 12&nbsp;hores.</p>
</form>
<script>
document.getElementById('robot').textContent=__ROBOT__;
const E=__ERROR__, M={
 bad:'Wrong password. / Contrasenya incorrecta.',
 rate:'Too many attempts \\u2014 wait a minute. / Massa intents \\u2014 espera un minut.'};
if(M[E]) document.getElementById('err').textContent=M[E];
</script></body></html>"""


# --------------------------------------------------------------------------
# not configured (503)
# --------------------------------------------------------------------------
NOT_CONFIGURED_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>EMORI dashboard &mdash; not configured</title><style>""" + _BASE_CSS + """
body{display:grid;place-items:center;min-height:100vh;padding:18px}
.card{width:min(94vw,470px)}
h1{font-size:1.15rem;font-weight:700;letter-spacing:-.01em;margin:0 0 10px}
code{background:rgba(92,131,191,.08);padding:2px 6px;border-radius:6px;color:var(--pri)}
p{margin:0 0 12px;color:var(--mut)}
</style></head><body><div class=card>
<div class=mark><i></i></div>
<h1>Dashboard not configured</h1>
<p>The management dashboard has no password, so it refuses to serve. There is no
anonymous mode.</p>
<p>Set <code>EMORI_DASH_PASSWORD</code> in the robot's environment and restart the
host app.</p>
<hr style="border:0;border-top:1px solid var(--bd);margin:16px 0">
<h1>Tauler no configurat</h1>
<p>El tauler de gesti&oacute; no t&eacute; contrasenya i per aix&ograve; no se serveix. No hi ha
mode an&ograve;nim.</p>
<p>Defineix <code>EMORI_DASH_PASSWORD</code> a l'entorn del robot i reinicia
l'aplicaci&oacute;.</p>
</div></body></html>"""


# --------------------------------------------------------------------------
# the dashboard
# --------------------------------------------------------------------------
_STRINGS_JS = """
const T={
en:{
 title:"EMORI \\u2014 ARISE Management Dashboard",
 eyebrow:"Management report",
 sub:"Robot", d7:"7 days", d30:"30 days", d90:"90 days",
 logout:"Sign out", refreshed:"updated", loading:"loading\\u2026",
 offline:"cannot reach the robot \\u2014 retrying",
 kpi_sessions:"Sessions", kpi_staff:"Unique staff", kpi_walkups:"Walk-ups",
 kpi_attention:"Attention", kpi_recovery:"Engagement recovery",
 kpi_uptime:"Uptime", kpi_completion:"Completion", kpi_addon:"Add-on use",
 kpi_engagement:"Avg engagement", kpi_completed:"Completed", kpi_abandoned:"Abandoned",
 kpi_duration:"Avg duration", kpi_competency:"Competency mean",
 kpi_robot_ended:"Robot-ended",
 target:"target", of:"of", nodata:"no data yet",
 attention_help:"share of 2\\u2011second samples at or above the engaged threshold",
 recovery_help:"recovery_success \\u00f7 attention_drop events",
 uptime_help:"heartbeats present \\u00f7 heartbeats expected",
 completion_help:"completed \\u00f7 (completed + abandoned) sessions. A session counts as \\
completed when it produced its outcome \\u2014 nobody presses \\u201cfinish\\u201d, so read \\
it beside Robot-ended",
 robot_ended_help:"sessions the ROBOT ended: the person walked away, left the frame, or \\
a turn stalled. Pokamind still analyses most of them, so they count as completed too",
 er_walkaway_ends:"walked away", er_presence_ends:"left the frame",
 er_watchdog_force_ends:"turn stalled", er_session_never_started:"never started",
 er_end_local_fallback:"no reply from Pokamind", er_gesture_end:"ended by the person, no analysis", er_preempted:"next learner arrived",
 er_session_start_failed:"session never opened",
 er_interrupted:"interrupted (robot restarted)",
 kpi_notstarted:"Never opened", kpi_conversations:"Conversations",
 notstarted_help:"sessions whose conversation never opened - the badge was scanned but \
Pokamind never started. A technical failure, so they are kept out of the completion rate",
 nof:"of", samples:"analysis samples", drops:"attention drops",
 beats:"heartbeats expected", finished:"finished conversations",
 toofew:"too few to judge", notmeasured:"not measured",
 nocurve:"No analysis cycle was recorded for this session, so there is no engagement \
curve. The camera analysis is what produces them.",
 nocomp:"No session in this window carries Pokamind competency scores yet.",
 nocompn:"scored of",
 nodelta_none:"Neither period has a scored session, so no change can be computed.",
 nodelta_before:"The before period has no scored session, so no change can be computed \
- the after profile is shown on its own.",
 nodelta_after:"The after period has no scored session yet, so no change can be computed.",
 nolegend:"no data",
 nomodule:"(no module recorded)",
 m_notstarted:"Never opened",
 rosterover:"More distinct badges practised than the roster lists - the roster size is \
out of date, so participation reads above 100%.",
 s_scored:"Scored",
 noeng_all:"No session in this window recorded camera analysis, so Engagement is \
blank for every row. The analysis cycles are what fill it.",
 nochart:"Nothing measured in this window yet.",
 addon_help:"times each add-on mode was started",
 trends:"Trends", t_sessions:"Sessions per day", t_engagement:"Average engagement per day",
 competency:"Team competency", competency_src:"averages of the Pokamind end-of-session scores",
 c_communication_clarity:"Communication clarity", c_engagement_presence:"Engagement & presence",
 c_emotional_intelligence:"Emotional intelligence", c_reasoning_quality:"Reasoning quality",
 c_technique_application:"Technique application", c_adaptability:"Adaptability",
 s_communication_clarity:"Clarity", s_engagement_presence:"Presence",
 s_emotional_intelligence:"Emotion", s_reasoning_quality:"Reasoning",
 s_technique_application:"Technique", s_adaptability:"Adapting",
 profile:"Competency profile",
 staff:"Staff progression", s_code:"Code", s_sessions:"Sessions", s_score:"Avg score",
 s_engagement:"Engagement", s_trend:"Trend", s_flag:"Status", s_last:"Last seen",
 coaching:"Coaching suggested", improving:"improving", flat:"steady", declining:"declining",
 coach_help:"a scored decline, or a mean below 50 across at least three scored sessions",
 coach_none:"Nobody is flagged in this window.",
 improved:"Most improved", improved_help:"largest sustained gain in competency scores",
 improved_none:"Not enough evidence yet \\u2014 three scored sessions are needed before \\
anyone appears here.",
 persession:"pts/session", scored:"scored sessions",
 framing:"Decision support only. These pseudonymous indicators describe how the robot's \\
sessions went; they are not an assessment of any person and make no clinical claim. \\
Competency decisions remain with human supervisors (ARISE People-Before-Machines).",
 walkup_note:"walk-up bucket \\u2014 many different people, never flagged",
 recent:"Recent sessions", r_when:"When", r_person:"Person", r_module:"Module",
 r_lang:"Lang", r_dur:"Duration", r_eng:"Engagement", r_scores:"Scores", r_status:"Status",
 yes:"yes", no:"no", detail:"Session detail", close:"Close",
 curve:"Engagement and attention over the session", events:"Events",
 status_active:"session in progress", status_idle:"idle",
 fiware_on:"FIWARE/NGSI-LD feed on", fiware_off:"FIWARE/NGSI-LD feed off",
 fiware_skipped:"no broker reachable at last session start",
 heartbeat:"last heartbeat", addon:"Add-on",
 footer:"Data is pseudonymized: the robot stores no names and no e-mail addresses. \\
Walk-up sessions are not linked to a person. Times are UTC.",
 empty:"Nothing recorded in this window yet.",
 export:"Export CSV", print:"Print",
 compare:"Compare periods", compare_off:"Single period",
 cmp_title:"Before and after", cmp_sub:"period B against period A, on the same measurements",
 cmp_a:"Before", cmp_b:"After", was:"was", pts:"pts", rel:"rel.",
 cmp_frame:"A delta is context, not an assessment: it says the sessions in this period \\
differed from the last, not that anyone improved or declined. Read it beside the sample size.",
 cmp_radar:"Competency profile, before and after",
 cmp_bars:"Change per competency",
 lowsample:"Small sample \\u2014 read with caution",
 spandiff:"Periods are different lengths", daysu:"days",
 cmprates:"compare rates, not totals",
 ncomp:"not comparable, different period lengths",
 badrange:"Those dates cannot be compared \\u2014 check the range.",
 p30:"Last 30 vs previous 30", p7:"Last 7 vs previous 7", p90:"Last 90 vs previous 90",
 pmonth:"This month so far vs all of last month",
 funnel:"Monthly participation",
 funnel_roster:"Roster size",
 funnel_bars:"the count above each bar is the staff who practised that month",
 funnel_help:"staff who practised at least once each month, against the roster",
 funnel_noroster:"No roster size is configured, so participation cannot be computed \\u2014 \\
the bars show how many staff practised each month.",
 heat:"Usage by weekday and hour", heat_help:"session starts, robot-local time",
 heat_none:"No sessions to place on the grid yet.",
 wd0:"Mon", wd1:"Tue", wd2:"Wed", wd3:"Thu", wd4:"Fri", wd5:"Sat", wd6:"Sun",
 motion:"The robot's motion, sentence by sentence",
 motion_help:"which family of gesture the robot performed for each sentence it spoke, and \\
how each plan ended",
 motion_family:"By family", motion_outcome:"By outcome",
 motion_lost:"Records the robot could not keep, so missing above",
 motion_not_moved:"Sentences whose plan moved nothing",
 plan_no_family:"talking, no family",
 po_finished:"finished", po_preempted:"interrupted", po_voice_end:"cut when the voice stopped",
 po_skipped:"skipped", po_unfinished:"still running when the session ended",
 mods:"Scenario difficulty",
 mods_help:"how sessions went per module \\u2014 a low score with high abandonment is either \\
a hard scenario or a badly written one",
 m_module:"Module", m_n:"Sessions", m_completion:"Completion", m_eng:"Engagement",
 m_comp:"Competency", m_robot:"Robot-ended"
},
ca:{
 title:"EMORI \\u2014 Tauler de gestió ARISE",
 eyebrow:"Informe de gestió",
 sub:"Robot", d7:"7 dies", d30:"30 dies", d90:"90 dies",
 logout:"Surt", refreshed:"actualitzat", loading:"carregant\\u2026",
 offline:"no es pot connectar amb el robot \\u2014 reintentant",
 kpi_sessions:"Sessions", kpi_staff:"Personal únic", kpi_walkups:"Visites lliures",
 kpi_attention:"Atenció", kpi_recovery:"Recuperació de l'engagement",
 kpi_uptime:"Disponibilitat", kpi_completion:"Compleció", kpi_addon:"Ús del complement",
 kpi_engagement:"Engagement mitjà", kpi_completed:"Completades", kpi_abandoned:"Abandonades",
 kpi_duration:"Durada mitjana", kpi_competency:"Competència mitjana",
 kpi_robot_ended:"Acabades pel robot",
 target:"objectiu", of:"de", nodata:"encara no hi ha dades",
 attention_help:"proporció de mostres de 2\\u00a0s igual o per sobre del llindar d'engagement",
 recovery_help:"esdeveniments recovery_success \\u00f7 attention_drop",
 uptime_help:"batecs rebuts \\u00f7 batecs esperats",
 completion_help:"sessions completades \\u00f7 (completades + abandonades). Una sessió \\
compta com a completada quan ha produït el seu resultat \\u2014 ningú prem \\u201cacabar\\u201d, \\
llegiu-ho al costat d'Acabades pel robot",
 robot_ended_help:"sessions que ha acabat el ROBOT: la persona ha marxat, ha sortit del \\
camp de visió o un torn s'ha encallat. Pokamind n'analitza la majoria, així que també \\
compten com a completades",
 er_walkaway_ends:"ha marxat", er_presence_ends:"ha sortit del camp",
 er_watchdog_force_ends:"torn encallat", er_session_never_started:"no ha arrencat",
 er_end_local_fallback:"cap resposta de Pokamind", er_gesture_end:"acabada per la persona, sense an\u00e0lisi", er_preempted:"ha arribat la següent persona",
 er_session_start_failed:"la sessió no s'ha obert",
 er_interrupted:"interromput (el robot s'ha reiniciat)",
 kpi_notstarted:"No s'han obert", kpi_conversations:"Converses",
 notstarted_help:"sessions on la conversa no s'ha arribat a obrir: s'ha escanejat la \
credencial per\u00f2 Pokamind no ha comen\u00e7at mai. \u00c9s una fallada t\u00e8cnica i per aix\u00f2 queden \
fora de la taxa de compleci\u00f3",
 nof:"de", samples:"mostres d'an\u00e0lisi", drops:"caigudes d'atenci\u00f3",
 beats:"batecs esperats", finished:"converses acabades",
 toofew:"massa poques per valorar", notmeasured:"no mesurat",
 nocurve:"No s'ha registrat cap cicle d'an\u00e0lisi en aquesta sessi\u00f3 i per tant no hi ha \
corba d'engagement. Els cicles els produeix l'an\u00e0lisi de la c\u00e0mera.",
 nocomp:"Cap sessi\u00f3 d'aquesta finestra no t\u00e9 encara puntuacions de competi\u00e8ncia de Pokamind.",
 nocompn:"puntuades de",
 nodelta_none:"Cap dels dos per\u00edodes no t\u00e9 sessions puntuades: no es pot calcular cap canvi.",
 nodelta_before:"El per\u00edode anterior no t\u00e9 cap sessi\u00f3 puntuada i no es pot calcular el \
canvi: es mostra nom\u00e9s el perfil posterior.",
 nodelta_after:"El per\u00edode posterior encara no t\u00e9 cap sessi\u00f3 puntuada i no es pot \
calcular el canvi.",
 nolegend:"sense dades",
 nomodule:"(sense m\u00f2dul registrat)",
 m_notstarted:"No s'han obert",
 rosterover:"Han practicat m\u00e9s credencials diferents de les que t\u00e9 la plantilla: la mida \
de la plantilla est\u00e0 desactualitzada i la participaci\u00f3 surt per sobre del 100%.",
 s_scored:"Puntuades",
 noeng_all:"Cap sessi\u00f3 d'aquesta finestra no ha registrat an\u00e0lisi de c\u00e0mera i per \
aix\u00f2 la columna Engagement \u00e9s buida a totes les files. Els cicles d'an\u00e0lisi s\u00f3n \
els que l'omplen.",
 nochart:"Encara no s'ha mesurat res en aquesta finestra.",
 addon_help:"vegades que s'ha iniciat cada mode del complement",
 trends:"Tendències", t_sessions:"Sessions per dia", t_engagement:"Engagement mitjà per dia",
 competency:"Competències de l'equip",
 competency_src:"mitjanes de les puntuacions de final de sessió de Pokamind",
 c_communication_clarity:"Claredat comunicativa", c_engagement_presence:"Engagement i presència",
 c_emotional_intelligence:"Intel\\u00b7ligència emocional", c_reasoning_quality:"Qualitat del raonament",
 c_technique_application:"Aplicació de la tècnica", c_adaptability:"Adaptabilitat",
 s_communication_clarity:"Claredat", s_engagement_presence:"Presència",
 s_emotional_intelligence:"Emoció", s_reasoning_quality:"Raonament",
 s_technique_application:"Tècnica", s_adaptability:"Adaptació",
 profile:"Perfil de competències",
 staff:"Progressió del personal", s_code:"Codi", s_sessions:"Sessions",
 s_score:"Puntuació mitjana", s_engagement:"Engagement", s_trend:"Tendència",
 s_flag:"Estat", s_last:"Darrera vegada",
 coaching:"Acompanyament suggerit", improving:"millorant", flat:"estable", declining:"baixant",
 coach_help:"una baixada puntuada, o una mitjana per sota de 50 en almenys tres sessions puntuades",
 coach_none:"Ningú marcat en aquesta finestra.",
 improved:"Més progrés", improved_help:"el guany sostingut més gran en les puntuacions",
 improved_none:"Encara no hi ha prou evidència \\u2014 calen tres sessions puntuades perquè \\
hi aparegui ningú.",
 persession:"punts/sessió", scored:"sessions puntuades",
 framing:"Només suport a la decisió. Aquests indicadors pseudonimitzats descriuen com han \\
anat les sessions del robot; no són una avaluació de cap persona ni fan cap afirmació \\
clínica. Les decisions sobre competències continuen en mans de supervisors humans \\
(principi ARISE de les persones abans que les màquines).",
 walkup_note:"grup de visites lliures \\u2014 moltes persones diferents, mai marcat",
 recent:"Sessions recents", r_when:"Quan", r_person:"Persona", r_module:"Mòdul",
 r_lang:"Idioma", r_dur:"Durada", r_eng:"Engagement", r_scores:"Puntuacions", r_status:"Estat",
 yes:"sí", no:"no", detail:"Detall de la sessió", close:"Tanca",
 curve:"Engagement i atenció al llarg de la sessió", events:"Esdeveniments",
 status_active:"sessió en curs", status_idle:"inactiu",
 fiware_on:"Canal FIWARE/NGSI-LD actiu", fiware_off:"Canal FIWARE/NGSI-LD aturat",
 fiware_skipped:"cap broker accessible en començar la darrera sessió",
 heartbeat:"darrer batec", addon:"Complement",
 footer:"Les dades estan pseudonimitzades: el robot no desa cap nom ni cap adreça \\
electrònica. Les visites lliures no es vinculen a cap persona. Les hores són UTC.",
 empty:"Encara no hi ha res registrat en aquesta finestra.",
 export:"Exporta CSV", print:"Imprimeix",
 compare:"Compara períodes", compare_off:"Període únic",
 cmp_title:"Abans i després", cmp_sub:"període B contra període A, amb les mateixes mesures",
 cmp_a:"Abans", cmp_b:"Després", was:"abans", pts:"punts", rel:"rel.",
 cmp_frame:"Una diferència és context, no una avaluació: diu que les sessions d'aquest \\
període no han estat com les anteriors, no que ningú hagi millorat o empitjorat. \\
Llegiu-la al costat de la mida de la mostra.",
 cmp_radar:"Perfil de competències, abans i després",
 cmp_bars:"Canvi per competència",
 lowsample:"Mostra petita \\u2014 interpreteu amb cautela",
 spandiff:"Els períodes tenen durades diferents", daysu:"dies",
 cmprates:"compareu taxes, no totals",
 ncomp:"no comparable, durades de període diferents",
 badrange:"Aquestes dates no es poden comparar \\u2014 reviseu l'interval.",
 p30:"Darrers 30 vs 30 anteriors", p7:"Darrers 7 vs 7 anteriors",
 p90:"Darrers 90 vs 90 anteriors",
 pmonth:"Aquest mes fins ara vs tot el mes passat",
 funnel:"Participació mensual",
 funnel_roster:"Mida de la plantilla",
 funnel_bars:"el nombre sobre cada barra és el personal que ha practicat aquell mes",
 funnel_help:"personal que ha practicat almenys una vegada cada mes, sobre la plantilla",
 funnel_noroster:"No hi ha cap mida de plantilla configurada i la participació no es pot \\
calcular \\u2014 les barres mostren quantes persones han practicat cada mes.",
 heat:"Ús per dia i hora", heat_help:"inicis de sessió, hora local del robot",
 heat_none:"Encara no hi ha sessions per situar a la graella.",
 wd0:"Dl", wd1:"Dt", wd2:"Dc", wd3:"Dj", wd4:"Dv", wd5:"Ds", wd6:"Dg",
 motion:"El moviment del robot, frase per frase",
 motion_help:"quina família de gest ha fet el robot a cada frase que ha dit, i com ha acabat \\
cada pla",
 motion_family:"Per família", motion_outcome:"Per resultat",
 motion_lost:"Registres que el robot no ha pogut guardar i que falten a dalt",
 motion_not_moved:"Frases el pla de les quals no ha mogut res",
 plan_no_family:"parlant, sense família",
 po_finished:"acabat", po_preempted:"interromput", po_voice_end:"tallat quan la veu ha parat",
 po_skipped:"omès", po_unfinished:"encara en curs quan ha acabat la sessió",
 mods:"Dificultat dels escenaris",
 mods_help:"com han anat les sessions per mòdul \\u2014 una puntuació baixa amb molt \\
abandonament és o bé un escenari difícil o bé un de mal escrit",
 m_module:"Mòdul", m_n:"Sessions", m_completion:"Compleció", m_eng:"Engagement",
 m_comp:"Competència", m_robot:"Acabades pel robot"
}};
"""

_DASH_CSS = """
body{padding:16px 16px 44px;max-width:1180px;margin:0 auto}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end;margin:6px 2px 18px}
.hgrp{flex:1 1 300px}
h1{font-size:1.25rem;font-weight:700;letter-spacing:-.01em;margin:0}
.who{font-size:13px;color:var(--mut);margin-top:3px}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tabs{display:flex;gap:4px;background:rgba(92,131,191,.06);padding:4px;border-radius:12px}
.tab{border:0;border-radius:9px;background:transparent;color:var(--mut);
 padding:6px 12px;font-size:13px;font-weight:600}
.tab[aria-pressed=true]{background:var(--pri);color:#fff}
.btn.amber{background:transparent;border-color:var(--amber);color:var(--amber)}
.btn.amber:hover{background:rgba(217,119,6,.06);border-color:var(--amber);color:var(--amber)}
.btn.ghost[aria-pressed=true]{background:rgba(92,131,191,.15);border-color:transparent;
 color:var(--pri)}
section{margin:0 0 16px}
h2{font-size:1.05rem;font-weight:700;letter-spacing:-.01em;margin:0 0 3px}
.hint{font-size:12px;color:var(--mut);margin:0 0 12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px}
.tile{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:16px 18px}
.tile .k{font-size:12px;font-weight:500;color:var(--mut);display:block;min-height:2.4em}
.tile .v{font-size:1.5rem;font-weight:700;line-height:1.1;font-variant-numeric:tabular-nums;
 display:block;margin:4px 0 2px}
.tile .t{font-size:11px;color:var(--mut);display:block}
.tile .bar{height:5px;border-radius:3px;background:rgba(76,110,162,.1);margin:8px 0 6px;position:relative}
.tile .bar i{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:var(--pri)}
.tile .bar u{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--slate);opacity:.5}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
svg{width:100%;height:auto;display:block}
svg.radar{max-width:340px;margin:0 auto}
.comp{display:grid;gap:10px;align-content:start}
.comp .row{display:grid;grid-template-columns:minmax(104px,1fr) 3fr auto;gap:10px;
 align-items:center;font-size:13px}
.comp .track{height:9px;border-radius:6px;background:rgba(76,110,162,.08)}
.comp .track i{display:block;height:100%;border-radius:6px;background:var(--pri)}
.comp .n{font-variant-numeric:tabular-nums;color:var(--ink);font-weight:600;min-width:2.6em;
 text-align:right}
.tw{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:520px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--bd);white-space:nowrap}
th{color:var(--mut);font-weight:600;font-size:12px}
tbody tr.click{cursor:pointer}
tbody tr.click:hover{background:rgba(92,131,191,.04)}
.chip{display:inline-block;border-radius:999px;padding:2px 10px;font-size:11px;font-weight:600;
 background:rgba(92,131,191,.08);color:var(--pri)}
.chip.warn{background:rgba(245,158,11,.12);color:#B45309}
.pill{display:inline-block;border-radius:999px;padding:2px 10px;font-size:11px;
 background:rgba(76,110,162,.06);color:var(--mut)}
/* the WHO-ended-it pill, beside the status one: quieter, because the status is
   still the headline and this is the qualifier that stops it lying */
.pill.mut{background:transparent;border:1px solid var(--bd);color:var(--slate)}
.up{color:var(--ok)}.down{color:var(--bad)}.flatc{color:var(--mut)}
.note{font-size:12px;color:var(--mut);background:rgba(92,131,191,.05);
 border-left:3px solid var(--pri);border-radius:0 10px 10px 0;padding:11px 13px;margin:0 0 14px}
.nline{font-size:12px;color:var(--slate);margin:10px 0 0;display:flex;gap:8px;
 flex-wrap:wrap;align-items:center}
.presets{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 14px}
.presets button{border:1px solid var(--bd);border-radius:40px;background:#fff;color:var(--mut);
 padding:6px 14px;font-size:12px;font-weight:600}
.presets button[aria-pressed=true]{background:rgba(92,131,191,.15);color:var(--pri);
 border-color:transparent}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--slate);
 justify-content:center;margin-top:4px}
.legend i{display:inline-block;width:14px;height:3px;border-radius:2px;vertical-align:middle;
 margin-right:5px}
.dbar{display:grid;grid-template-columns:minmax(104px,1fr) 3fr;gap:10px;align-items:center;
 font-size:13px;margin-bottom:9px}
.dbar .lb{font-weight:600;color:#2C3539;font-size:12px}
.dbar .tr{position:relative;height:24px;border-radius:6px;background:rgba(76,110,162,.06)}
.dbar .tr:after{content:'';position:absolute;left:50%;top:2px;bottom:2px;width:1px;
 background:rgba(76,110,162,.25)}
.dbar .tr i{position:absolute;top:3px;bottom:3px;border-radius:6px}
.dbar .tr b{position:absolute;top:50%;transform:translateY(-50%);font-size:12px;font-weight:600}
.dbar .tr b.inside{color:#fff}
.heat{display:grid;grid-template-columns:2.6em repeat(24,1fr);gap:2px;font-size:10px;
 color:var(--slate);min-width:520px}
.heat i{height:15px;border-radius:3px;background:rgba(92,131,191,var(--a,0));
 box-shadow:inset 0 0 0 1px rgba(76,110,162,.07)}
.heat span{line-height:15px;font-weight:600}
.heat span.hr{text-align:center;color:var(--mut);font-weight:400}
.empt{border:1px dashed var(--bd);border-radius:12px;padding:18px 16px;text-align:center;
 font-size:12.5px;color:var(--mut);line-height:1.5;background:rgba(92,131,191,.02)}
.tile .t.few{color:#B45309}
.imp{display:flex;justify-content:space-between;gap:10px;align-items:baseline;
 padding:9px 0;border-bottom:1px solid var(--bd);font-size:13px}
.imp:last-child{border-bottom:0}
.imp b{font-weight:600}
footer{font-size:12px;color:var(--mut);margin-top:24px;border-top:1px solid var(--bd);
 padding-top:14px}
dialog{border:1px solid var(--bd);border-radius:16px;background:#fff;color:var(--ink);
 padding:20px;width:min(94vw,760px);box-shadow:0 8px 24px rgba(0,0,0,.08)}
dialog::backdrop{background:rgba(51,51,51,.35)}
.dhead{display:flex;gap:10px;align-items:baseline;margin-bottom:8px}
.dhead h2{flex:1}
.evs{font-size:12px;color:var(--mut);margin-top:10px;max-height:26vh;overflow:auto}
.evs div{padding:3px 0;border-bottom:1px solid var(--bd)}
body.cmp .wnd{display:none}
#cmp{display:none}
body.cmp #cmp{display:block}
@media (max-width:560px){h1{font-size:1.1rem}.tile .v{font-size:1.3rem}
 .card{padding:16px}.dbar{grid-template-columns:1fr;gap:4px}}
@media print{
 body{background:#fff;padding:0;max-width:none}
 .tools,.presets,.err,dialog{display:none!important}
 section,.card,.tile{break-inside:avoid;page-break-inside:avoid}
 .card,.tile{border-color:#ddd}
 .tw{overflow:visible}
 a{color:inherit;text-decoration:none}
}
"""

_DASH_BODY = """
<header>
 <div class=hgrp>
  <p class=eyebrow id=eyebrow></p>
  <h1 id=h1></h1>
  <div class=who><span id=robotname></span> &middot; <span id=live></span> &middot;
   <span id=stamp class=muted></span></div>
 </div>
 <div class=tools>
  <div class=tabs>
   <button class=tab id=d7></button>
   <button class=tab id=d30></button>
   <button class=tab id=d90></button>
  </div>
  <button class="btn ghost" id=cmpbtn></button>
  <a class="btn amber" id=exportbtn href="/api/dashboard/export.csv?days=7" download></a>
  <button class="btn ghost" id=printbtn></button>
  <button class="btn ghost" id=langbtn>CA</button>
  <form method=post action="/dashboard/logout" style="display:inline">
   <button class="btn ghost" id=logout type=submit></button></form>
 </div>
</header>
<p class=err id=neterr></p>

<section id=cmp>
 <p class=eyebrow id=e_cmp></p>
 <h2 id=h_cmp></h2><p class=hint id=h_cmp_sub></p>
 <div class=presets id=presets></div>
 <p class=note id=cmpframing></p>
 <div class=tiles id=cmptiles></div>
 <p class=nline id=cmpn1></p>
 <div class=grid2 style="margin-top:16px">
  <div class=card><p class=eyebrow id=e_cmpradar></p><div id=cmpradar></div>
   <div class=legend id=cmplegend></div><p class=nline id=cmpn2></p></div>
  <div class=card><p class=eyebrow id=e_cmpbars></p><div id=cmpbars></div>
   <p class=nline id=cmpn3></p></div>
 </div>
</section>

<section class=wnd><div class=tiles id=tiles></div></section>

<section class=wnd><h2 id=h_trends></h2><div class=grid2>
 <div class=card><p class=eyebrow id=h_tsessions></p><div id=chart_sessions></div></div>
 <div class=card><p class=eyebrow id=h_tengagement></p><div id=chart_engagement></div></div>
</div></section>

<section class="card wnd"><h2 id=h_comp></h2><p class=hint id=h_comp_src></p>
 <div class=grid2><div id=compradar></div><div class=comp id=comp></div></div>
 <p class=nline id=compn></p></section>

<section class=card><h2 id=h_funnel></h2><p class=hint id=h_funnel_help></p>
 <div id=funnel></div><p class=hint id=funnelnote style="margin:10px 0 0"></p></section>

<section class=card><h2 id=h_heat></h2><p class=hint id=h_heat_help></p>
 <div class=tw><div id=heat></div></div>
 <p class=hint id=heatnote style="margin:10px 0 0"></p></section>

<section class="card wnd"><h2 id=h_motion></h2><p class=hint id=h_motion_help></p>
 <div class=grid2>
  <div><p class=eyebrow id=e_motionfamily></p><div id=motionfamily></div></div>
  <div><p class=eyebrow id=e_motionoutcome></p><div id=motionoutcome></div></div>
 </div>
 <p class=hint id=motionnote style="margin:10px 0 0"></p></section>

<section class=card><h2 id=h_mods></h2><p class=hint id=h_mods_help></p>
 <div class=tw><table><thead><tr id=modshead></tr></thead><tbody id=mods></tbody></table></div>
</section>

<section class=grid2>
 <div class=card><h2 id=h_improved></h2><p class=hint id=h_improved_help></p>
  <div id=improved></div></div>
 <div class=card><h2 id=h_coach></h2><p class=hint id=h_coach_help></p>
  <div id=coach></div></div>
</section>

<section class=card><h2 id=h_staff></h2>
 <p class=note id=framing></p>
 <div class=tw><table><thead><tr id=staffhead></tr></thead><tbody id=staff></tbody></table></div>
</section>

<section class=card><h2 id=h_recent></h2>
 <div class=tw><table><thead><tr id=recenthead></tr></thead><tbody id=recent></tbody></table></div>
 <p class=hint id=recentnote style="margin:10px 0 0"></p>
</section>

<footer><span id=foot></span><br><span id=feed></span></footer>

<dialog id=dlg><div class=dhead><h2 id=dtitle></h2>
 <button class="btn ghost" id=dclose></button></div>
 <p class=hint id=dsub></p><div id=dchart></div><div class=evs id=devs></div></dialog>
"""

_MAIN_JS = """
const TARGETS=__TARGETS__, ATTMIN=__ATTMIN__, ROBOT=__ROBOT__;
const COMPS=["communication_clarity","engagement_presence","emotional_intelligence",
 "reasoning_quality","technique_application","adaptability"];
// Pokamind chart palette: one brand blue, its light twin for the "before" ghost,
// and the status colours reserved for pass/fail moments.
const C={pri:"#5C83BF",pri2:"#7FA1D4",ok:"#10B981",warn:"#F59E0B",bad:"#EF4444",
 web:"#e8e8e8",slate:"#646E78",mut:"#6B7280"};
// Which direction is GOOD for each compare metric: an abandonment that goes up is
// red even though the number went up. 0 = neither (duration is a design choice).
const DIRS={abandoned:-1,robot_ended:-1,avg_duration_s:0};
const PCTM={completion_pct:1,attention_pct:1,recovery_pct:1,uptime_pct:1};
// The metrics that are TOTALS: they scale with the length of the window, so a
// shorter B period drives every one of them down whatever the sessions did.
// Every other compare metric is a rate or a mean and stays comparable.
const COUNTM={n_sessions:1,unique_staff:1,completed:1,abandoned:1,robot_ended:1};
const CMPT=["n_sessions","unique_staff","completed","abandoned","robot_ended",
 "completion_pct","avg_engagement","attention_pct","recovery_pct","competency_mean",
 "avg_duration_s"];
const CMPK={n_sessions:"kpi_sessions",unique_staff:"kpi_staff",completed:"kpi_completed",
 abandoned:"kpi_abandoned",robot_ended:"kpi_robot_ended",
 completion_pct:"kpi_completion",avg_engagement:"kpi_engagement",
 attention_pct:"kpi_attention",recovery_pct:"kpi_recovery",
 competency_mean:"kpi_competency",avg_duration_s:"kpi_duration"};
const PRESETS=[{id:"p30",days:30},{id:"p7",days:7},{id:"p90",days:90},{id:"pmonth",month:1}];
const LOWN=5;

let lang=(localStorage.getItem('emori_dash_lang')==='ca')?'ca':__DEFLANG__;
let days=parseInt(localStorage.getItem('emori_dash_days')||'7',10);
if([7,30,90].indexOf(days)<0) days=7;
let cmpOn=localStorage.getItem('emori_dash_cmp')==='1';
let preset=localStorage.getItem('emori_dash_preset')||'p30';
if(!PRESETS.some(p=>p.id===preset)) preset='p30';
let data={}, slow=0;
const $=id=>document.getElementById(id);
const t=k=>(T[lang]&&T[lang][k])||T.en[k]||k;
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>(
 {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ok=v=>v!=null&&!isNaN(v);
const pct=v=>ok(v)?Math.round(v)+'%':'\\u2014';
const num=v=>ok(v)?String(Math.round(v)):'\\u2014';
const one=v=>ok(v)?(Math.round(v*10)/10).toFixed(1):'\\u2014';
// A span in days reads as "30", not "30.0" -- but half a day must not round
// away, or a 12-hour window would claim to be the same length as a full day.
const spanDays=v=>ok(v)?String(Math.round(v*10)/10):'\\u2014';
// The Pokamind scorecard rule. Counts are never coloured by it: a session count
// is not a score, and a red "3 sessions" would be an invented judgement.
const mcol=v=>!ok(v)?C.mut:(v>=75?C.ok:(v>=50?C.warn:C.bad));
function ts(sec){ if(!sec) return '\\u2014';
 const d=new Date(sec*1000);
 return d.toISOString().slice(0,16).replace('T',' '); }
function dur(s){ if(!ok(s)||!s) return '\\u2014';
 const m=Math.floor(s/60); return m+'m '+Math.round(s-m*60)+'s'; }
function pad2(n){ return (n<10?'0':'')+n; }

// ---- charts: hand-rolled SVG, no library, no canvas repaint cost ----------
const W=600,H=170,P=28;
function axis(y){ return '<line x1="'+P+'" y1="'+y+'" x2="'+(W-P)+'" y2="'+y+
 '" stroke="rgba(76,110,162,.18)"/>'; }
function edgeLabels(labels,y){
 if(!labels.length) return '';
 return '<text x="'+P+'" y="'+y+'" fill="'+C.slate+'" font-size="11">'+esc(labels[0])+'</text>'+
  '<text x="'+(W-P)+'" y="'+y+'" fill="'+C.slate+'" font-size="11" text-anchor="end">'+
  esc(labels[labels.length-1])+'</text>';
}
function bars(vals,labels,opts){
 opts=opts||{};
 const n=Math.max(1,vals.length), bw=(W-P*2)/n;
 // `opts.max` pins the scale. A percentage series MUST pass 100: auto-scaling to
 // the tallest bar would put a 17% month level with the 70% target line.
 const max=opts.max||Math.max(1,...vals.map(v=>ok(v)?v:0));
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img">'+axis(H-24);
 if(ok(opts.target)){ const ty=(H-24-(H-48)*Math.min(1,opts.target/max)).toFixed(1);
  s+='<line x1="'+P+'" y1="'+ty+'" x2="'+(W-P)+'" y2="'+ty+'" stroke="'+C.ok+
     '" stroke-dasharray="4 4" opacity=".8"/><text x="'+(W-P)+'" y="'+(+ty-5)+
     '" fill="'+C.ok+'" font-size="11" text-anchor="end">'+esc(opts.targetLabel||'')+'</text>'; }
 // Only the DRAWING is clamped (a 150% participation month drew a bar through
 // the caption above it); the tooltip below still carries the true number.
 vals.forEach((v,i)=>{ const val=ok(v)?Math.min(v,max):0, h=(H-48)*(val/max),
   x=P+i*bw+bw*0.15, y=H-24-h;
  const fill=opts.colors?opts.colors(v,i):C.pri, mid=(P+i*bw+bw/2).toFixed(1);
  s+='<rect x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+Math.max(1,bw*0.7).toFixed(1)+
     '" height="'+Math.max(0,h).toFixed(1)+'" rx="4" fill="'+fill+'"><title>'+
     esc(labels[i])+': '+(ok(v)?v:'\\u2014')+'</title></rect>';
  if(opts.cap) s+='<text x="'+mid+'" y="'+Math.max(11,y-5).toFixed(1)+'" fill="'+C.slate+
     '" font-size="10" font-weight="600" text-anchor="middle">'+esc(opts.cap[i]||'')+'</text>';
  if(opts.tick) s+='<text x="'+mid+'" y="'+(H-8)+'" fill="'+C.slate+
     '" font-size="10" text-anchor="middle">'+esc(labels[i]||'')+'</text>'; });
 s+='<text x="'+P+'" y="14" fill="'+C.slate+'" font-size="11">'+esc(opts.hiLabel||max)+'</text>';
 return s+(opts.tick?'':edgeLabels(labels,H-6))+'</svg>';
}
// `labels` only supplies the two edge captions, so it may be shorter than the
// series (a 900-cycle drill-in labels its first and last sample, not all 900).
function lines(series,labels,opts){
 opts=opts||{};
 const lo=opts.lo!=null?opts.lo:0, hi=opts.hi!=null?opts.hi:1;
 const n=Math.max(1,...series.map(ser=>ser.values.length));
 const X=i=>P+(n<2?(W-P*2)/2:i*(W-P*2)/(n-1));
 const Y=v=>H-24-(H-44)*Math.max(0,Math.min(1,(v-lo)/((hi-lo)||1)));
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img">'+axis(H-24);
 if(opts.target!=null){ const y=Y(opts.target).toFixed(1);
  s+='<line x1="'+P+'" y1="'+y+'" x2="'+(W-P)+'" y2="'+y+
     '" stroke="'+C.ok+'" stroke-dasharray="4 4" opacity="0.8"/>'; }
 // One polyline per RUN of measured days: a single line through the nulls
 // would join two measured days weeks apart and read as steady days in between.
 series.forEach(ser=>{ let run=[];
  const flush=()=>{
   if(run.length===1){ const xy=run[0].split(',');
    s+='<circle cx="'+xy[0]+'" cy="'+xy[1]+'" r="3.5" fill="'+ser.color+
       '" stroke="#fff" stroke-width="1.5"/>'; }
   else if(run.length) s+='<polyline fill="none" stroke="'+ser.color+'" stroke-width="2" '+
    'stroke-linejoin="round" points="'+run.join(' ')+'"/>';
   run=[]; };
  ser.values.forEach((v,i)=>{ if(!ok(v)){ flush(); return; }
   run.push(X(i).toFixed(1)+','+Y(v).toFixed(1)); });
  flush(); });
 s+='<text x="'+P+'" y="14" fill="'+C.slate+'" font-size="11">'+esc(opts.hiLabel||hi)+'</text>';
 return s+edgeLabels(labels,H-6)+'</svg>';
}
// The radar follows the product's chart style: 0..100 scale, alternating
// #fafafa/#fff rings on a #e8e8e8 web, series filled at 12%, 2px stroke, r3.5
// markers stroked white. An axis whose value is null is drawn at the centre and
// its label greyed -- a missing competency must not read as a measured zero.
function radar(sets,labels,opts){
 opts=opts||{};
 const S=opts.size||300, R=S*0.29, cx=S/2, cy=S*0.47, n=labels.length||1;
 const ang=i=>-Math.PI/2+i*2*Math.PI/n;
 const at=(i,f)=>[(cx+Math.cos(ang(i))*R*f).toFixed(1),(cy+Math.sin(ang(i))*R*f).toFixed(1)];
 let s='<svg viewBox="0 0 '+S+' '+Math.round(S*0.94)+'" role="img" class="radar">';
 for(let r=5;r>=1;r--){ const p=[];
  for(let i=0;i<n;i++) p.push(at(i,r/5).join(','));
  s+='<polygon points="'+p.join(' ')+'" fill="'+(r%2?'#fff':'#fafafa')+'" stroke="'+
     C.web+'" stroke-width="1"/>'; }
 for(let i=0;i<n;i++){ const e=at(i,1);
  s+='<line x1="'+cx+'" y1="'+cy+'" x2="'+e[0]+'" y2="'+e[1]+'" stroke="'+C.web+'"/>'; }
 // A set with nothing measured is DROPPED: plotted at the centre it reads as
 // six measured zeros. The caller says so in words instead (radarHas).
 sets=sets.filter(set=>(set.values||[]).some(ok));
 sets.forEach(set=>{ const vals=set.values||[], p=[];
  for(let i=0;i<n;i++){ const v=vals[i];
   p.push(at(i,Math.max(0,Math.min(100,ok(v)?v:0))/100)); }
  s+='<polygon points="'+p.map(q=>q.join(',')).join(' ')+'" fill="'+set.color+
     '" fill-opacity="'+(set.fill||0.12)+'" stroke="'+set.color+'" stroke-width="2" '+
     'stroke-linejoin="round"'+(set.dash?' stroke-dasharray="5 4"':'')+'/>';
  p.forEach((q,i)=>{ if(!ok(vals[i])) return;
   s+='<circle cx="'+q[0]+'" cy="'+q[1]+'" r="3.5" fill="'+set.color+
      '" stroke="#fff" stroke-width="1.5"><title>'+esc(labels[i])+' \\u00b7 '+
      esc(set.name||'')+': '+one(vals[i])+'</title></circle>'; }); });
 for(let i=0;i<n;i++){ const a=ang(i), lx=cx+Math.cos(a)*(R+16), ly=cy+Math.sin(a)*(R+14);
  const anchor=Math.abs(Math.cos(a))<0.3?'middle':(Math.cos(a)>0?'start':'end');
  const has=sets.some(set=>ok((set.values||[])[i]));
  s+='<text x="'+lx.toFixed(1)+'" y="'+(ly+3).toFixed(1)+'" text-anchor="'+anchor+
     '" font-size="10" font-weight="600" fill="'+(has?C.slate:'#c9ced4')+'">'+
     esc(labels[i])+'</text>'; }
 return s+'</svg>';
}
const shortComps=()=>COMPS.map(k=>t('s_'+k));
// Does this series carry a single measurement? (radar() drops the ones that do not)
const radarHas=vals=>(vals||[]).some(ok);

// ---- KPI tiles -----------------------------------------------------------
// A rate with almost no evidence must not be painted like a result: "Recovery
// 0%" over ONE attention drop was target-missed red. Every rate tile carries its
// own n, and below its floor the number goes grey and says so.
function tile(key,value,opts){
 opts=opts||{}; const tg=opts.target;
 const few=opts.n!=null&&opts.floor!=null&&(ok(opts.n)?opts.n:0)<opts.floor;
 const colour=opts.plain?'var(--ink)':(few?C.mut:mcol(value));
 let s='<div class="tile"'+(opts.help?' title="'+esc(opts.help)+'"':'')+'>'+
  '<span class="k">'+esc(t(key))+'</span><span class="v" style="color:'+colour+'">'+
  esc(opts.text!=null?opts.text:pct(value))+'</span>';
 if(tg!=null){ const w=ok(value)?Math.max(0,Math.min(100,value)):0;
  s+='<div class="bar"><i style="width:'+w+'%;background:'+colour+'"></i>'+
     '<u style="left:'+tg+'%"></u></div>'; }
 const foot=[];
 if(tg!=null) foot.push(t('target')+' '+tg+'%');
 if(opts.n!=null) foot.push('n=' + num(opts.n) + (opts.nlabel?' '+t(opts.nlabel):''));
 if(opts.hint) foot.push(opts.hint);
 if(foot.length) s+='<span class="t'+(few?' few':'')+'">'+esc(foot.join(' \u00b7 '))+
   (few?' \u00b7 '+esc(t('toofew')):'')+'</span>';
 return s+'</div>';
}
// The two or three commonest robot-side endings, named in the reader's language:
// "197" alone is a number, "197 - walked away 140, turn stalled 42" is the finding.
// An unknown slug (a watchdog trigger added after this page shipped) prints as
// itself rather than as the missing translation key.
const erName=k=>(T[lang]&&T[lang]['er_'+k])||T.en['er_'+k]||k;
function endReasonHint(s){
 const r=s.end_reasons||{};
 const top=Object.keys(r).sort((a,b)=>r[b]-r[a]).slice(0,3);
 if(!top.length) return t('robot_ended_help');
 return top.map(k=>erName(k)+' '+num(r[k])).join(' \\u00b7 ');
}
// How much evidence a rate needs before it is coloured like a result.
const FLOOR={cycles:30,drops:5,finished:5};
function renderTiles(){
 const s=data.summary||{}, st=data.status||{}, ax=s.addon_events||{};
 // One tile for whatever add-on modes were used, named from the usage kind itself; it
 // does not exist on a robot with no add-on.
 const axKinds=Object.keys(ax).sort((a,b)=>ax[b]-ax[a]);
 const axTotal=axKinds.reduce((n,k)=>n+(ax[k]||0),0);
 const finished=(s.completed||0)+Math.max(0,(s.abandoned||0)-(s.not_started||0));
 $('tiles').innerHTML=
  tile('kpi_attention',s.attention_pct,{target:TARGETS.attention,help:t('attention_help'),
   n:s.n_cycles,nlabel:'samples',floor:FLOOR.cycles})+
  tile('kpi_recovery',s.recovery_pct,{target:TARGETS.recovery,help:t('recovery_help'),
   n:s.attention_drops,nlabel:'drops',floor:FLOOR.drops})+
  tile('kpi_uptime',s.uptime_pct,{target:TARGETS.uptime,help:t('uptime_help')})+
  tile('kpi_completion',s.completion_pct,{target:TARGETS.completion,help:t('completion_help'),
   n:finished,nlabel:'finished',floor:FLOOR.finished})+
  // Deliberately next to Completion: a robot-driven ending IS a completion here
  // (the conversation produced its outcome), so the completion rate on its own
  // cannot say that the person walked out mid-conversation. This tile says it.
  tile('kpi_robot_ended',null,{text:num(s.robot_ended),hint:endReasonHint(s),
   help:t('robot_ended_help'),plain:1})+
  // Sessions that never became a conversation: counted in Sessions, excluded
  // from Completion, and until now explaining neither.
  tile('kpi_notstarted',null,{text:num(s.not_started),hint:t('notstarted_help'),
   help:t('notstarted_help'),plain:1})+
  tile('kpi_sessions',null,{text:num(s.sessions),
   hint:t('d'+days)+' \u00b7 '+num(s.n_conversations)+' '+t('kpi_conversations').toLowerCase(),
   plain:1})+
  tile('kpi_staff',null,{text:num(s.unique_staff),hint:t('kpi_walkups')+': '+num(s.walkups),plain:1})+
  tile('kpi_engagement',ok(s.avg_engagement)?s.avg_engagement*100:null,
   {n:s.n_cycles,nlabel:'samples',floor:FLOOR.cycles})+
  ((st.addon||axKinds.length)
   ? tile('kpi_addon',null,{text:num(axTotal),plain:1,help:t('addon_help'),
      hint:axKinds.map(k=>k+' '+num(ax[k])).join(' \u00b7 ')})
   : '');
}
function renderTrend(){
 const tr=data.trend||{};
 const lab=(tr.day_list||[]).map(d=>d.slice(5));
 $('chart_sessions').innerHTML=bars(tr.sessions||[],tr.day_list||[]);
 const eng=tr.engagement||[], comp=(tr.competency||[]).map(v=>ok(v)?v/100:null);
 // A pair of empty axes reads as a chart that failed, not as a quiet fortnight.
 $('chart_engagement').innerHTML=(eng.some(ok)||comp.some(ok))
  ? lines([{values:eng,color:C.pri2},{values:comp,color:C.pri}],lab,
          {lo:0,hi:1,hiLabel:'100%'})
  : '<p class=empt>'+esc(t('nochart'))+'</p>';
}
function renderComp(){
 const s=data.summary||{}, avg=s.competency||{};
 const vals=COMPS.map(k=>avg[k]);
 // Six empty rails and a radar collapsed to a dot read as broken, not as "no
 // scores yet". The web stays; the sentence says why it is bare.
 if(!radarHas(vals)){
  $('compradar').innerHTML=radar([],shortComps())+'<p class=empt>'+esc(t('nocomp'))+'</p>';
  $('comp').innerHTML=''; $('compn').innerHTML=''; return; }
 $('compradar').innerHTML=radar([{values:vals,color:C.pri,name:t('competency')}],shortComps());
 $('comp').innerHTML=COMPS.map(k=>{ const v=avg[k];
  return '<div class=row><span>'+esc(t('c_'+k))+'</span>'+
   '<span class=track><i style="width:'+(ok(v)?Math.max(0,Math.min(100,v)):0)+
   '%;background:'+mcol(v)+'"></i></span>'+
   '<span class=n>'+num(v)+'</span></div>'; }).join('');
 // The n these six averages stand on: 2 scored of 70 is not 70.
 let line='<span>n='+num(s.n_scored_sessions)+' '+esc(t('nocompn'))+' '+
  num(s.sessions)+' '+esc(t('kpi_sessions').toLowerCase())+'</span>';
 if((s.n_scored_sessions||0)<LOWN)
  line+='<span class="chip warn">'+esc(t('lowsample'))+'</span>';
 $('compn').innerHTML=line;
}

// ---- compare (the consultancy question) ----------------------------------
function fmtM(key,v){
 if(!ok(v)) return '\\u2014';
 if(key==='avg_engagement') return pct(v*100);
 if(PCTM[key]) return pct(v);
 if(key==='avg_duration_s') return dur(v);
 if(key==='competency_mean') return one(v);
 return num(v);
}
// A delta on a metric that is ITSELF a percentage is in POINTS; pct_change is the
// relative move. They are labelled differently because they are different claims.
function fmtD(key,d){
 if(!ok(d)) return '';
 const sign=d>0?'+':'';
 if(key==='avg_engagement') return sign+one(d*100)+' '+t('pts');
 if(PCTM[key]||key==='competency_mean') return sign+one(d)+' '+t('pts');
 if(key==='avg_duration_s') return sign+Math.round(d)+'s';
 return sign+num(d);
}
function cmpTile(key,c){
 const a=(c.a||{})[key], b=(c.b||{})[key];
 const d=(c.delta||{})[key], rel=(c.pct_change||{})[key];
 const dir=DIRS[key]===undefined?1:DIRS[key];
 // The API flags a comparison whose two periods are not the same length
 // (`equal_spans:false`) precisely so the page can refuse to read a TOTAL as a
 // result: "this month vs last month" is month-to-DATE on the B side, so on the
 // 3rd of the month three days of sessions would otherwise render as a red
 // -89% collapse against a full previous month. The arrow and the relative
 // change are dropped for totals there -- the rates beside them still stand.
 const bare=c.equal_spans===false&&!!COUNTM[key];
 const good=(bare||!ok(d)||d===0||dir===0)?null:((d>0)===(dir>0));
 const cls=good===true?'up':(good===false?'down':'flatc');
 const arrow=bare||!ok(d)||d===0?'\\u2192':(d>0?'\\u25b2':'\\u25bc');
 let foot=t('was')+' '+fmtM(key,a);
 if(bare) foot+=' \\u00b7 '+t('ncomp');
 else{
  if(ok(d)&&d!==0) foot+=' \\u00b7 '+fmtD(key,d);
  if(ok(rel)&&rel!==0) foot+=' ('+(rel>0?'+':'')+one(rel)+'% '+t('rel')+')';
 }
 return '<div class="tile"><span class="k">'+esc(t(CMPK[key]||key))+'</span>'+
  '<span class="v">'+esc(fmtM(key,b))+' <span class="'+cls+
  '" style="font-size:.8rem">'+arrow+'</span></span>'+
  '<span class="t">'+esc(foot)+'</span></div>';
}
// The n under a COMPETENCY chart is the SCORED session count, not the session
// count: "n=2 before / n=70 after" captioned a chart drawn from 0 and 2.
function nlineScored(c){
 const a=(c.a||{}).n_scored_sessions, b=(c.b||{}).n_scored_sessions;
 let s='<span>n='+num(a)+' '+t('cmp_a').toLowerCase()+' \u00b7 n='+num(b)+' '+
  t('cmp_b').toLowerCase()+' \u00b7 '+esc(t('scored'))+'</span>';
 if((ok(a)?a:0)<LOWN||(ok(b)?b:0)<LOWN)
  s+='<span class="chip warn">'+esc(t('lowsample'))+'</span>';
 return s;
}
function nline(c,extra){
 const a=(c.a||{}).n_sessions, b=(c.b||{}).n_sessions;
 let s='<span>n='+num(a)+' '+t('cmp_a').toLowerCase()+' \\u00b7 n='+num(b)+' '+
  t('cmp_b').toLowerCase()+' \\u00b7 '+esc(t('kpi_sessions').toLowerCase())+'</span>';
 if((ok(a)?a:0)<LOWN||(ok(b)?b:0)<LOWN)
  s+='<span class="chip warn">'+esc(t('lowsample'))+'</span>';
 // Says WHICH lengths, because "different lengths" without the two numbers
 // leaves the reader to assume the small one is the shortfall.
 if(c.equal_spans===false)
  s+='<span class="chip warn">'+esc(t('spandiff')+' ('+spanDays(c.span_a_days)+
   ' vs '+spanDays(c.span_b_days)+' '+t('daysu')+') \\u2014 '+t('cmprates'))+'</span>';
 return s+(extra||'');
}
// Why the six bars are not there, and WHICH period is missing its scores.
function deltaWhy(c){
 const na=(c.a||{}).n_scored_sessions||0, nb=(c.b||{}).n_scored_sessions||0;
 if(!na&&!nb) return t('nodelta_none');
 if(!na) return t('nodelta_before');
 if(!nb) return t('nodelta_after');
 return t('nodelta_none');
}
function deltaBars(c){
 const dc=(c.delta&&c.delta.competencies)||{};
 if(!COMPS.some(k=>ok(dc[k]))) return '<p class=empt>'+esc(deltaWhy(c))+'</p>';
 let max=5;
 COMPS.forEach(k=>{ if(ok(dc[k])) max=Math.max(max,Math.abs(dc[k])); });
 return COMPS.map(k=>{ const d=dc[k];
  if(!ok(d)) return '<div class=dbar><span class=lb>'+esc(t('c_'+k))+
   '</span><span class=tr><b class=flatc style="left:calc(50% + 6px)">\\u2014</b></span></div>';
  const w=Math.min(50,Math.abs(d)/max*50), colour=d>=0?C.ok:C.bad;
  const side=d>=0?'left:50%':'right:50%', inside=w>11;
  const label=(d>0?'+':'')+one(d);
  // The value rides INSIDE the bar (Pokamind's bar style) when there is room,
  // and just past its end otherwise -- never pinned to the track, which would
  // float a small delta's label half a chart away from its bar.
  const tag=inside
   ? '<b class=inside style="'+(d>=0?'right:8px':'left:8px')+'">'+esc(label)+'</b>'
   : '';
  const outer=inside?'':'<b style="'+(d>=0?'left':'right')+':calc(50% + '+
   (w+1).toFixed(1)+'%);color:'+colour+'">'+esc(label)+'</b>';
  return '<div class=dbar><span class=lb>'+esc(t('c_'+k))+'</span><span class=tr>'+
   '<i style="'+side+';width:'+w.toFixed(1)+'%;background:'+colour+'">'+tag+'</i>'+
   outer+'</span></div>'; }).join('');
}
function renderCompare(){
 const c=data.compare;
 $('cmpframing').textContent=t('cmp_frame')+' '+t('framing');
 $('presets').innerHTML=PRESETS.map(p=>'<button data-p="'+p.id+'" aria-pressed="'+
  (p.id===preset)+'">'+esc(t(p.id))+'</button>').join('');
 Array.prototype.forEach.call($('presets').querySelectorAll('button'),b=>
  b.addEventListener('click',()=>{ preset=b.getAttribute('data-p');
   localStorage.setItem('emori_dash_preset',preset); loadCompare(); }));
 if(!c||c.error){
  const msg=c&&c.error?t('badrange'):t('loading');
  $('cmptiles').innerHTML='<p class=hint>'+esc(msg)+'</p>';
  $('cmpradar').innerHTML=''; $('cmpbars').innerHTML='';
  $('cmplegend').innerHTML='';
  $('cmpn1').innerHTML=''; $('cmpn2').innerHTML=''; $('cmpn3').innerHTML='';
  return; }
 $('cmptiles').innerHTML=CMPT.map(k=>cmpTile(k,c)).join('');
 const ca=(c.a||{}).competencies||{}, cb=(c.b||{}).competencies||{};
 const va=COMPS.map(k=>ca[k]), vb=COMPS.map(k=>cb[k]);
 if(!radarHas(va)&&!radarHas(vb)) $('cmpradar').innerHTML=radar([],shortComps())+
   '<p class=empt>'+esc(t('nocomp'))+'</p>';
 else $('cmpradar').innerHTML=radar([
  {values:va,color:C.pri2,fill:0.08,dash:1,name:t('cmp_a')},
  {values:vb,color:C.pri,fill:0.12,name:t('cmp_b')}],shortComps());
 // Which of the two is actually on the chart: with no before-scores, two blue
 // keys under one shape invited comparing a profile with itself.
 const key=(colour,label,has)=>'<span><i style="background:'+colour+'"></i>'+esc(label)+
  (has?'':' \u2014 '+esc(t('nolegend')))+'</span>';
 $('cmplegend').innerHTML=key(C.pri2,t('cmp_a'),radarHas(va))+key(C.pri,t('cmp_b'),radarHas(vb));
 $('cmpbars').innerHTML=deltaBars(c);
 const line=nline(c), scoredLine=nlineScored(c);
 $('cmpn1').innerHTML=line; $('cmpn2').innerHTML=scoredLine; $('cmpn3').innerHTML=scoredLine;
}

// ---- participation funnel ------------------------------------------------
function renderFunnel(){
 const f=data.funnel||{}, rows=f.funnel||[];
 const roster=ok(f.roster_size)&&f.roster_size>0;
 const target=ok(f.target_pct)?f.target_pct:(TARGETS.participation||70);
 const labels=rows.map(r=>String(r.month||'').slice(5)+'/'+String(r.month||'').slice(2,4));
 const cap=rows.map(r=>num(r.active_staff));
 if(!rows.length){ $('funnel').innerHTML='<p class=hint>'+esc(t('empty'))+'</p>';
  $('funnelnote').textContent=''; return; }
 // Participation is scored against ITS target, not the 75/50 scorecard rule: a
 // month that hits the ARISE 70% must not be painted amber for being under 75.
 if(roster) $('funnel').innerHTML=bars(rows.map(r=>r.participation_pct),labels,
  {target:target,targetLabel:target+'%',hiLabel:'100%',max:100,cap:cap,tick:1,
   colors:v=>ok(v)&&v>=target?C.ok:C.pri});
 else $('funnel').innerHTML=bars(rows.map(r=>r.active_staff),labels,{cap:cap,tick:1});
 let note=roster?(t('funnel_roster')+': '+f.roster_size+' \\u00b7 '+t('funnel_bars'))
  :t('funnel_noroster');
 // More badges than the roster lists is a stale roster, not 150% participation
 // -- and the bar is clamped, so without this the reader sees no reason.
 if(f.roster_exceeded) note+=' \\u2014 '+t('rosterover');
 $('funnelnote').textContent=note;
}

// ---- usage heatmap -------------------------------------------------------
function renderHeat(){
 const h=data.heatmap||{}, grid=h.grid||[], max=Math.max(1,h.max||0);
 if(!grid.length){ $('heat').innerHTML='<p class=hint>'+esc(t('heat_none'))+'</p>';
  $('heatnote').textContent=''; return; }
 let s='<span></span>';
 for(let x=0;x<24;x++) s+='<span class=hr>'+(x%3===0?pad2(x):'')+'</span>';
 for(let d=0;d<7;d++){ s+='<span>'+esc(t('wd'+d))+'</span>';
  for(let x=0;x<24;x++){ const v=(grid[d]||[])[x]||0;
   const a=v?(0.12+0.78*(v/max)).toFixed(2):'0';
   s+='<i style="--a:'+a+'" title="'+esc(t('wd'+d))+' '+pad2(x)+':00 \\u00b7 '+v+'"></i>'; } }
 $('heat').innerHTML=s;
 $('heatnote').textContent=(h.tz||'')+' \\u00b7 '+num(h.total)+' '+
  t('kpi_sessions').toLowerCase();
}

// ---- scenario difficulty -------------------------------------------------
// Two rows can share a title and be different modules (a dev id and the prod
// one for the same scenario - what the robot has): without the id fragment they
// read as one row duplicated. A row with no module says so instead of a dash.
function modLabel(m,dupes){
 const title=String(m.module_title||'').trim();
 if(!title) return t('nomodule');
 return dupes[title]>1&&m.module_id
  ? title+' \\u00b7 '+String(m.module_id).slice(0,6) : title;
}
function renderMods(){
 const rows=(data.modules&&data.modules.modules)||[];
 const dupes={};
 rows.forEach(m=>{ const k=String(m.module_title||'').trim();
  if(k) dupes[k]=(dupes[k]||0)+1; });
 $('modshead').innerHTML=
  ['m_module','m_n','m_completion','m_robot','m_notstarted','m_eng','m_comp']
  .map(k=>'<th>'+esc(t(k))+'</th>').join('');
 $('mods').innerHTML=rows.length?rows.map(m=>'<tr><td>'+
   esc(modLabel(m,dupes))+'</td><td>'+num(m.n_sessions)+'</td><td>'+
   pct(m.completion_pct)+'</td><td>'+num(m.robot_ended)+'</td><td>'+
   num(m.not_started)+'</td><td>'+
   pct(ok(m.avg_engagement)?m.avg_engagement*100:null)+
   '</td><td style="color:'+mcol(m.competency_mean)+';font-weight:600">'+
   one(m.competency_mean)+'</td></tr>').join('')
  :'<tr><td colspan=7 class=muted>'+esc(t('empty'))+'</td></tr>';
}

// ---- the robot's motion, sentence by sentence ----------------------------
// An outcome added after this page shipped prints as itself, like an end reason.
const poName=k=>(T[lang]&&T[lang]['po_'+k])||T.en['po_'+k]||k;
const familyName=k=>k||t('plan_no_family');
function countRows(counts,name){
 const keys=Object.keys(counts||{}).sort((a,b)=>counts[b]-counts[a]);
 return keys.length?keys.map(k=>'<div class=imp><span>'+esc(name(k))+
   '</span><span class=muted>'+num(counts[k])+'</span></div>').join('')
  :'<p class=hint>'+esc(t('empty'))+'</p>';
}
function renderMotion(){
 const pc=(data.summary&&data.summary.plan_counts)||{};
 $('motionfamily').innerHTML=countRows(pc.families,familyName);
 $('motionoutcome').innerHTML=countRows(pc.outcomes,poName);
 $('motionnote').textContent=[pc.not_moved?t('motion_not_moved')+': '+num(pc.not_moved):'',
  pc.lost_events?t('motion_lost')+': '+num(pc.lost_events):''].filter(Boolean).join(' \\u00b7 ');
}
// One session's share of the same counts, for the drill-in.
function motionLines(pc){
 if(!pc||!pc.sentences) return '';
 const line=(counts,name)=>Object.keys(counts).sort((a,b)=>counts[b]-counts[a])
  .map(k=>esc(name(k))+' '+num(counts[k])).join(' \\u00b7 ');
 return '<b>'+esc(t('motion'))+'</b><div>'+line(pc.families,familyName)+'</div><div>'+
  line(pc.outcomes,poName)+'</div>'+(pc.not_moved?'<div>'+esc(t('motion_not_moved'))+': '+
  num(pc.not_moved)+'</div>':'')+(pc.lost_events?'<div>'+esc(t('motion_lost'))+': '+
  num(pc.lost_events)+'</div>':'');
}

// ---- most improved + coaching -------------------------------------------
function renderImproved(){
 const list=(data.persons&&data.persons.most_improved)||[];
 $('improved').innerHTML=list.length?list.map(p=>'<div class=imp><span><b>'+
   esc(p.person_code)+'</b> <span class=muted>'+num(p.scored_sessions)+' '+
   esc(t('scored'))+'</span></span><span class=up>+'+one(p.competency_slope)+' '+
   esc(t('persession'))+'</span></div>').join('')
  :'<p class=hint>'+esc(t('improved_none'))+'</p>';
 const flagged=((data.persons&&data.persons.persons)||[]).filter(p=>p.coaching_flag);
 $('coach').innerHTML=flagged.length?flagged.map(p=>'<div class=imp><span><b>'+
   esc(p.person_code)+'</b> <span class=muted>'+num(p.scored_sessions)+' '+
   esc(t('scored'))+'</span></span><span class=chip>'+esc(t('coaching'))+
   '</span></div>').join('')
  :'<p class=hint>'+esc(t('coach_none'))+'</p>';
}

// ---- staff + sessions ----------------------------------------------------
function trendMark(d){
 if(d==='improving') return '<span class=up>\\u2197 '+esc(t('improving'))+'</span>';
 if(d==='declining') return '<span class=down>\\u2198 '+esc(t('declining'))+'</span>';
 return '<span class=flatc>\\u2192 '+esc(t('flat'))+'</span>';
}
function renderStaff(){
 const rows=(data.persons&&data.persons.persons)||[];
 // `Scored` sits between Sessions and Avg score: the average is over those
 // alone, and 2-of-4 scored must not look identical to 4-of-4.
 $('staffhead').innerHTML=
  ['s_code','s_sessions','s_scored','s_score','s_engagement','s_trend','s_last','s_flag']
  .map(k=>'<th>'+esc(t(k))+'</th>').join('');
 $('staff').innerHTML=rows.length?rows.map(p=>'<tr class=click data-code="'+
   esc(p.person_code)+'"><td>'+esc(p.person_code)+
   (p.kind==='walkup'?' <span class=pill title="'+esc(t('walkup_note'))+'">\\u2726</span>':'')+
   '</td><td>'+num(p.sessions)+'</td><td>'+num(p.scored_sessions)+'</td><td>'+
   num(p.avg_competency)+'</td><td>'+
   pct(ok(p.avg_engagement)?p.avg_engagement*100:null)+'</td><td>'+trendMark(p.trend)+
   '</td><td>'+ts(p.last_seen)+'</td><td>'+
   (p.coaching_flag?'<span class=chip>'+esc(t('coaching'))+'</span>':'')+'</td></tr>').join('')
  :'<tr><td colspan=8 class=muted>'+esc(t('empty'))+'</td></tr>';
 Array.prototype.forEach.call($('staff').querySelectorAll('tr.click'),
  tr=>tr.addEventListener('click',()=>openPerson(tr.getAttribute('data-code'))));
}
function renderRecent(){
 const rows=(data.sessions&&data.sessions.sessions)||[];
 $('recenthead').innerHTML=['r_when','r_person','r_module','r_lang','r_dur','r_eng','r_scores','r_status']
  .map(k=>'<th>'+esc(t(k))+'</th>').join('');
 $('recent').innerHTML=rows.length?rows.map(r=>'<tr class=click data-id="'+esc(r.session_id)+'">'+
   '<td>'+ts(r.started_at)+'</td><td>'+esc(r.person_code)+'</td><td>'+
   esc(r.module||'\\u2014')+'</td><td>'+esc(r.lang||'\\u2014')+'</td><td>'+dur(r.duration_s)+
   '</td><td>'+pct(ok(r.avg_engagement)?r.avg_engagement*100:null)+'</td><td>'+
   (r.has_scores?t('yes'):t('no'))+'</td><td><span class=pill>'+esc(r.status||'')+
   '</span>'+(r.end_reason?' <span class="pill mut" title="'+esc(t('robot_ended_help'))+
   '">'+esc(erName(r.end_reason))+'</span>':'')+'</td></tr>').join('')
  :'<tr><td colspan=8 class=muted>'+esc(t('empty'))+'</td></tr>';
 // A whole column of dashes is the reader's question, so answer it once under
 // the table instead of thirty times inside it.
 $('recentnote').textContent=(rows.length&&!rows.some(r=>ok(r.avg_engagement)))
  ? t('noeng_all') : '';
 Array.prototype.forEach.call($('recent').querySelectorAll('tr.click'),
  tr=>tr.addEventListener('click',()=>openDetail(tr.getAttribute('data-id'))));
}
function renderStatus(){
 const s=data.status||{};
 $('robotname').textContent=s.robot||ROBOT;
 $('live').textContent=s.session_active?t('status_active'):t('status_idle');
 const bits=[s.fiware_enabled?t('fiware_on'):t('fiware_off')];
 if(s.last_fiware_skipped) bits.push(t('fiware_skipped')+' ('+ts(s.last_fiware_skipped)+')');
 if(s.heartbeat&&s.heartbeat.ts) bits.push(t('heartbeat')+' '+ts(s.heartbeat.ts));
 if(s.addon&&s.addon.mode) bits.push(t('addon')+': '+s.addon.mode);
 $('feed').textContent=bits.join(' \\u00b7 ');
}
function renderChrome(){
 document.documentElement.lang=lang;
 $('eyebrow').textContent=t('eyebrow');
 $('h1').textContent=t('title');
 $('d7').textContent=t('d7'); $('d30').textContent=t('d30'); $('d90').textContent=t('d90');
 [7,30,90].forEach(d=>$('d'+d).setAttribute('aria-pressed',String(d===days)));
 $('langbtn').textContent=(lang==='en')?'CA':'EN';
 $('logout').textContent=t('logout');
 $('printbtn').textContent=t('print');
 $('exportbtn').textContent=t('export');
 $('exportbtn').setAttribute('href','/api/dashboard/export.csv?days='+days);
 $('cmpbtn').textContent=cmpOn?t('compare_off'):t('compare');
 $('cmpbtn').setAttribute('aria-pressed',String(cmpOn));
 document.body.className=cmpOn?'cmp':'';
 $('e_cmp').textContent=t('eyebrow');
 $('h_cmp').textContent=t('cmp_title'); $('h_cmp_sub').textContent=t('cmp_sub');
 $('e_cmpradar').textContent=t('cmp_radar'); $('e_cmpbars').textContent=t('cmp_bars');
 $('h_trends').textContent=t('trends');
 $('h_tsessions').textContent=t('t_sessions');
 $('h_tengagement').textContent=t('t_engagement');
 $('h_comp').textContent=t('competency'); $('h_comp_src').textContent=t('competency_src');
 $('h_funnel').textContent=t('funnel'); $('h_funnel_help').textContent=t('funnel_help');
 $('h_heat').textContent=t('heat'); $('h_heat_help').textContent=t('heat_help');
 $('h_mods').textContent=t('mods'); $('h_mods_help').textContent=t('mods_help');
 $('h_motion').textContent=t('motion'); $('h_motion_help').textContent=t('motion_help');
 $('e_motionfamily').textContent=t('motion_family');
 $('e_motionoutcome').textContent=t('motion_outcome');
 $('h_improved').textContent=t('improved');
 $('h_improved_help').textContent=t('improved_help');
 $('h_coach').textContent=t('coaching'); $('h_coach_help').textContent=t('coach_help');
 $('h_staff').textContent=t('staff'); $('framing').textContent=t('framing');
 $('h_recent').textContent=t('recent'); $('foot').textContent=t('footer');
 $('dclose').textContent=t('close');
}
function renderAll(){ renderChrome(); renderTiles(); renderTrend(); renderComp();
 renderCompare(); renderFunnel(); renderHeat(); renderMotion(); renderMods(); renderImproved();
 renderStaff(); renderRecent(); renderStatus(); }

// ---- drill-in ------------------------------------------------------------
function openPerson(code){
 const rows=(data.persons&&data.persons.persons)||[];
 const p=rows.filter(r=>r.person_code===code)[0];
 if(!p) return;
 const comp=p.competencies||{};
 $('dtitle').textContent=p.person_code;
 $('dsub').textContent=t('s_sessions')+': '+num(p.sessions)+' \\u00b7 '+
  t('scored')+': '+num(p.scored_sessions)+' \\u00b7 '+t('s_score')+': '+
  num(p.avg_competency)+' \\u00b7 '+t('s_last')+': '+ts(p.last_seen);
 const vals=COMPS.map(k=>comp[k]);
 $('dchart').innerHTML='<p class="hint">'+esc(t('profile'))+'</p>'+
  radar(radarHas(vals)?[{values:vals,color:C.pri,name:p.person_code}]:[],shortComps())+
  (radarHas(vals)?'':'<p class=empt>'+esc(t('nocomp'))+'</p>');
 $('devs').innerHTML=COMPS.map(k=>'<div>'+esc(t('c_'+k))+' \\u00b7 '+
  num(comp[k])+'</div>').join('');
 $('dlg').showModal();
}
async function openDetail(id){
 const d=await api('/api/dashboard/session/'+encodeURIComponent(id));
 if(!d||d.error) return;
 $('dtitle').textContent=t('detail');
 $('dsub').textContent=(d.session&&d.session.module||'')+' \\u00b7 '+
  (d.session&&d.session.person_code||'')+' \\u00b7 '+ts(d.session&&d.session.started_at);
 const cy=d.cycles||[];
 const lab=cy.length?[ts(cy[0].ts).slice(11),ts(cy[cy.length-1].ts).slice(11)]:[];
 // An empty grid with a target line across it is not a curve, and reads like a
 // session where nobody was engaged.
 $('dchart').innerHTML='<p class="hint">'+esc(t('curve'))+'</p>'+
  (cy.length?lines(
   [{values:cy.map(c=>c.engagement_score),color:C.pri},
    {values:cy.map(c=>c.attention),color:C.pri2}],
   lab,{lo:0,hi:1,hiLabel:'100%',target:ATTMIN})
  :'<p class=empt>'+esc(t('nocurve'))+'</p>');
 $('devs').innerHTML=motionLines(d.plan_counts)+'<b>'+esc(t('events'))+'</b>'+(d.events||[]).map(e=>
  '<div>'+ts(e.ts)+' \\u00b7 '+esc(e.kind)+'</div>').join('');
 $('dlg').showModal();
}

// ---- data ----------------------------------------------------------------
async function api(path){
 try{ const r=await fetch(path,{credentials:'same-origin',cache:'no-store'});
  if(r.status===401){ location.href='/dashboard'; return null; }
  // 400 is only ever a range the compare route refuses to answer about; it is a
  // message for the operator, not a network failure.
  if(r.status===400) return {error:'bad_range'};
  if(!r.ok) throw new Error('http '+r.status);
  $('neterr').textContent=''; return await r.json();
 }catch(e){ $('neterr').textContent=t('offline'); return null; }
}
function cmpQuery(){
 const p=PRESETS.filter(x=>x.id===preset)[0]||PRESETS[0];
 if(!p.month) return '?days='+p.days;
 // "this month vs last month" is the one preset the API cannot express as a span,
 // so it is sent as four UTC dates. The end date is INCLUSIVE server-side.
 const n=new Date(), y=n.getUTCFullYear(), m=n.getUTCMonth();
 const d=(yy,mm,dd)=>yy+'-'+pad2(mm+1)+'-'+pad2(dd);
 const py=m?y:y-1, pm=m?m-1:11;
 const last=new Date(Date.UTC(y,m,0)).getUTCDate();
 return '?a_start='+d(py,pm,1)+'&a_end='+d(py,pm,last)+
  '&b_start='+d(y,m,1)+'&b_end='+d(y,m,n.getUTCDate());
}
async function loadCompare(){
 if(!cmpOn) return;
 const c=await api('/api/dashboard/compare'+cmpQuery());
 if(c) data.compare=c;
 renderCompare();
}
// The three window-wide views (heatmap, modules, funnel) each walk a whole
// window of `sessions`; on ~0.9 of a free CM4 core they do not need to move
// every minute. Fetched on load, on a window change, and every tenth tick of
// the ONE existing timer -- no second timer is added.
async function loadSlow(){
 const q='?days='+days;
 const [he,mo,fu]=await Promise.all([
  api('/api/dashboard/heatmap'+q), api('/api/dashboard/modules'+q),
  api('/api/dashboard/funnel?months=6')]);
 if(he) data.heatmap=he; if(mo) data.modules=mo; if(fu) data.funnel=fu;
}
async function refresh(full){
 const q='?days='+days;
 const [su,tr,pe,se,st]=await Promise.all([
  api('/api/dashboard/summary'+q), api('/api/dashboard/trend'+q),
  api('/api/dashboard/persons'+q), api('/api/dashboard/sessions'+q+'&limit=25'),
  api('/api/dashboard/status')]);
 if(su) data.summary=su; if(tr) data.trend=tr; if(pe) data.persons=pe;
 if(se) data.sessions=se; if(st) data.status=st;
 if(full) await loadSlow();
 if(cmpOn) await loadCompare();
 $('stamp').textContent=t('refreshed')+' '+new Date().toTimeString().slice(0,5);
 renderAll();
}
[7,30,90].forEach(d=>$('d'+d).addEventListener('click',()=>{
 days=d; localStorage.setItem('emori_dash_days',String(d)); refresh(true); }));
$('langbtn').addEventListener('click',()=>{
 lang=(lang==='en')?'ca':'en'; localStorage.setItem('emori_dash_lang',lang); renderAll(); });
$('cmpbtn').addEventListener('click',()=>{
 cmpOn=!cmpOn; localStorage.setItem('emori_dash_cmp',cmpOn?'1':'0');
 renderChrome(); if(cmpOn&&!data.compare) loadCompare(); else renderCompare(); });
$('printbtn').addEventListener('click',()=>window.print());
$('dclose').addEventListener('click',()=>$('dlg').close());
$('stamp').textContent=t('loading');
renderChrome(); refresh(true);

// Pull-only and never faster than 60 s: the CM4 has ~0.9 of a core free and a
// live session owns it. Hidden tabs do not poll at all; showing one again
// refreshes immediately if the data on screen is already stale.
let last=Date.now();
setInterval(()=>{ if(document.visibilityState==='visible'){ last=Date.now();
 slow=(slow+1)%10; refresh(slow===0); } },60000);
document.addEventListener('visibilitychange',()=>{
 if(document.visibilityState==='visible'&&Date.now()-last>60000){
  last=Date.now(); refresh(false); } });
"""

# Exported for the headless render test: the page's JavaScript on its own, with
# the same `__PLACEHOLDER__` tokens the HTML carries.
DASHBOARD_JS = _STRINGS_JS + _MAIN_JS

DASHBOARD_HTML = ("""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>EMORI ARISE dashboard</title><style>""" + _BASE_CSS + _DASH_CSS +
                  "</style></head><body>" + _DASH_BODY + "<script>" +
                  DASHBOARD_JS + "</script></body></html>")
