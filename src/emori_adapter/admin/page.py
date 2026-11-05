"""HTML for the superadmin service (routes live in ``admin/server.py``).

Same convention as ``hostapp/dashboard_page.py`` and ``hostapp/pages.py``: pure
Python string constants with ``__PLACEHOLDER__`` tokens the caller substitutes.
No Jinja2, no StaticFiles, no CDN, no web fonts, **no external origin of any
kind** — the single most likely moment this page is loaded is with the phone
joined to the robot's own access point, where there is no internet at all. Every
byte the browser needs (CSS, JS, icons, favicon) is in this file.

Visually it uses the same visual palette as the dashboard
(dusty cornflower ``#5C83BF``, warm off-white canvas, status colours used only
where something passes or fails), the same 14px flat cards on a hairline border.
It differs in two deliberate ways, both because of where it is used: it is
phone-first (single column from 360px up, every touch target >= 44px) and it
carries a dark theme, because robots are often commissioned in dim corridors.

Placeholders (substituted as **plain text into text nodes** — the caller should
HTML-escape; nothing here interpolates them into an attribute or a JS literal,
so a stray quote cannot break the page or inject script):

  ``__ROBOT_NAME__``  the robot's display name (login + app)
  ``__ERROR__``       login error message, empty string for "no error" (login)

The app reads the robot name back out of the DOM rather than taking a second
JS-side placeholder — that keeps the "type the robot name to confirm" flow on
the reboot button honest with what the operator can actually see on screen.

Every fetch in ``_APP_JS`` targets a route of the admin service (see
``docs/robot-admin.md``). Response
readers are deliberately forgiving about *shape* (list vs ``{"gates":[...]}``,
epoch seconds vs ISO timestamps) and strict about *names*: no field is invented,
missing fields render as an em dash, and every card has a skeleton, an empty and
an error state, because the one guaranteed truth about this page is that it gets
opened when something is already broken.
"""

from __future__ import annotations

# An inline SVG data URI: a robot head with two antennae in the brand blue.
# '#' MUST be percent-encoded inside a data URI or everything after it is read
# as a fragment and the icon silently disappears; the spaces are encoded at the
# bottom of this expression for the same class of reason (a raw space inside an
# unquoted-ish URI is tolerated by most parsers and not by all of them).
_FAVICON = (
    "data:image/svg+xml,"
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>"
    "<rect width='64' height='64' rx='15' fill='%235C83BF'/>"
    "<path d='M21 15l6 7M43 15l-6 7' stroke='%23ffffff' stroke-width='4' stroke-linecap='round'/>"
    "<rect x='16' y='22' width='32' height='27' rx='11' fill='%23ffffff'/>"
    "<circle cx='26' cy='34' r='3.6' fill='%235C83BF'/>"
    "<circle cx='38' cy='34' r='3.6' fill='%235C83BF'/>"
    "<path d='M27 42h10' stroke='%235C83BF' stroke-width='3' stroke-linecap='round'/></svg>"
).replace(" ", "%20")

_HEAD = (
    """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name=color-scheme content="light dark">
<meta name=theme-color media="(prefers-color-scheme: light)" content="#FFFBF8">
<meta name=theme-color media="(prefers-color-scheme: dark)" content="#14171B">
<meta name=apple-mobile-web-app-capable content=yes>
<meta name=mobile-web-app-capable content=yes>
<meta name=robots content=noindex>
<link rel=icon href=\""""
    + _FAVICON
    + """\">
<link rel=apple-touch-icon href=\""""
    + _FAVICON
    + """\">"""
)

# ---------------------------------------------------------------------------
# design tokens — one variable set, two themes
# ---------------------------------------------------------------------------
# The light values ARE the dashboard's (dashboard_page.py::_BASE_CSS) so the two
# surfaces read as one product. The dark set keeps the same hues and only moves
# lightness: the brand blue lightens to #7FA1D4 (its existing --pri2) because
# #5C83BF on a near-black card fails contrast for small text.
_BASE_CSS = """
*{box-sizing:border-box}
[hidden]{display:none!important}
:root{
 --bg:#FFFBF8;--card:#fff;--card2:#FFF6EF;--bd:rgba(76,110,162,.14);--bd2:rgba(76,110,162,.28);
 --pri:#5C83BF;--pri2:#7FA1D4;--prid:#4A6A9B;--priw:rgba(92,131,191,.09);
 --ink:#2C3539;--mut:#6B7280;--slate:#646E78;
 --ok:#0F9D6E;--warn:#B45309;--bad:#DC2626;--okd:#10B981;--warnd:#F59E0B;--badd:#EF4444;
 --okw:rgba(16,185,129,.12);--warnw:rgba(245,158,11,.14);--badw:rgba(239,68,68,.10);
 --sh:0 1px 2px rgba(44,53,57,.06);--sh2:0 10px 30px rgba(44,53,57,.14);
 --r:14px;--rs:11px;--fg-on-pri:#fff}
@media (prefers-color-scheme:dark){:root{
 --bg:#14171B;--card:#1B1F25;--card2:#232830;--bd:rgba(127,161,212,.17);--bd2:rgba(127,161,212,.30);
 --pri:#7FA1D4;--pri2:#9FBBE4;--prid:#6C90C6;--priw:rgba(127,161,212,.12);
 --ink:#E8EBF0;--mut:#9AA4B2;--slate:#B4BCC8;
 --ok:#34D399;--warn:#FBBF24;--bad:#F87171;--okd:#34D399;--warnd:#FBBF24;--badd:#F87171;
 --okw:rgba(52,211,153,.14);--warnw:rgba(251,191,36,.15);--badw:rgba(248,113,113,.14);
 --sh:0 1px 2px rgba(0,0,0,.4);--sh2:0 12px 34px rgba(0,0,0,.55);
 --fg-on-pri:#10151C}}
body{font-family:Nunito,'Segoe UI',Roboto,system-ui,-apple-system,sans-serif;margin:0;
 background:var(--bg);color:var(--ink);font-size:15px;line-height:1.45;
 -webkit-text-size-adjust:100%;-webkit-tap-highlight-color:transparent;
 overflow-wrap:break-word}
.wrap{max-width:680px;margin:0 auto;padding:0 14px}
h1{font-size:1.35rem;font-weight:800;letter-spacing:-.02em;margin:0}
h2{font-size:1rem;font-weight:800;letter-spacing:-.01em;margin:0}
p{margin:0}
b,strong{font-weight:800}
.eyebrow{font-size:.7rem;font-weight:800;text-transform:uppercase;letter-spacing:.1em;
 color:var(--slate);margin:0 0 3px}
.sub{font-size:12.5px;color:var(--mut);margin:6px 0 0}
.lead{font-size:13.5px;color:var(--mut);margin:6px 0 0}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 font-variant-numeric:tabular-nums}
.mark{width:34px;height:34px;border-radius:11px;background:var(--priw);display:grid;
 place-items:center;flex:0 0 auto}
.mark i{width:13px;height:13px;border-radius:50%;background:var(--pri);display:block}
button{font:inherit;cursor:pointer;color:inherit}
.btn{min-height:44px;display:inline-flex;align-items:center;justify-content:center;gap:8px;
 border:1px solid var(--pri);background:var(--pri);color:var(--fg-on-pri);border-radius:12px;
 padding:0 16px;font-weight:800;font-size:14px;text-decoration:none;
 transition:background .18s,border-color .18s,opacity .18s,transform .06s}
.btn:active{transform:translateY(1px)}
.btn[disabled]{opacity:.5;cursor:default;transform:none}
.btn.ghost{background:var(--card);border-color:var(--bd2);color:var(--ink)}
.btn.ghost:active{background:var(--priw)}
.btn.danger{background:var(--bad);border-color:var(--bad);color:#fff}
.btn.dghost{background:transparent;border-color:var(--bad);color:var(--bad)}
.btn.sm{min-height:44px;padding:0 12px;font-size:13px;border-radius:11px}
.btn.wide{width:100%;margin-top:10px}
.btn.busy::after{content:'';width:14px;height:14px;border-radius:50%;flex:0 0 auto;
 border:2px solid currentColor;border-top-color:transparent;animation:sp .7s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
input,select{font:inherit;color:var(--ink)}
input[type=text],input[type=password],select{width:100%;min-height:46px;padding:0 13px;
 border-radius:12px;border:1px solid var(--bd2);background:var(--card);color:var(--ink)}
input:focus-visible,select:focus-visible,button:focus-visible,summary:focus-visible{
 outline:2px solid var(--pri);outline-offset:2px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:var(--r);padding:16px;
 box-shadow:var(--sh);margin:0 0 12px}
.chead{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 11px;
 font-size:11.5px;font-weight:800;background:var(--priw);color:var(--pri);white-space:nowrap;
 max-width:100%;overflow:hidden;text-overflow:ellipsis}
.chip.ok{background:var(--okw);color:var(--ok)}
.chip.warn{background:var(--warnw);color:var(--warn)}
.chip.bad{background:var(--badw);color:var(--bad)}
.chip.mut{background:var(--priw);color:var(--mut)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut);display:inline-block;
 flex:0 0 auto}
.dot.ok{background:var(--okd)}.dot.warn{background:var(--warnd)}.dot.bad{background:var(--badd)}
.dot.idle{background:var(--mut);opacity:.6}
.msg{font-size:13px;margin:9px 0 0;min-height:1.15em;color:var(--mut)}
.msg.bad{color:var(--bad);font-weight:700}
.msg.ok{color:var(--ok);font-weight:700}
.empty{font-size:13px;color:var(--mut);padding:12px 0}
.skel{height:13px;border-radius:7px;margin:11px 0;background:linear-gradient(90deg,
 var(--priw),rgba(127,161,212,.03),var(--priw));background-size:220% 100%;
 animation:sk 1.3s linear infinite}
@keyframes sk{to{background-position:-220% 0}}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01ms!important;
 animation-iteration-count:1!important;transition:none!important;scroll-behavior:auto!important}}
"""

# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
_LOGIN_CSS = """
body{display:grid;place-items:center;min-height:100dvh;padding:20px 14px}
.box{width:min(96vw,360px)}
.card{padding:22px 20px 20px}
h1{font-size:1.15rem;margin:12px 0 2px}
.who{font-size:13px;color:var(--mut);margin:0 0 20px}
.dots{display:flex;gap:10px;justify-content:center;margin:2px 0 18px}
.dots i{width:13px;height:13px;border-radius:50%;border:2px solid var(--bd2);display:block;
 transition:transform .12s,background .12s,border-color .12s}
.dots i.on{background:var(--pri);border-color:var(--pri);transform:scale(1.12)}
#pin{position:absolute;opacity:0;pointer-events:none;width:1px;height:1px}
.pad{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
.key{min-height:56px;border-radius:14px;border:1px solid var(--bd);background:var(--card2);
 font-size:1.25rem;font-weight:800;font-variant-numeric:tabular-nums;color:var(--ink)}
.key:active{background:var(--priw);border-color:var(--pri)}
.key.aux{font-size:.8rem;font-weight:800;text-transform:uppercase;letter-spacing:.06em;
 color:var(--mut);background:transparent;border-color:transparent}
.msg{text-align:center;margin:14px 0 0}
.foot{font-size:11.5px;color:var(--mut);text-align:center;margin:16px 0 0;line-height:1.5}
.shake{animation:shk .35s}
@keyframes shk{10%,90%{transform:translateX(-2px)}30%,70%{transform:translateX(4px)}
 50%{transform:translateX(-4px)}}
"""

_LOGIN_JS = """
(function(){
'use strict';
var $=function(id){return document.getElementById(id);};
var pin='', busy=false;
var inp=$('pin'), err=$('err'), go=$('go'), card=$('card');
if((err.textContent||'').trim()) err.className='msg bad';
function paint(){
 var d=$('dots').children;
 for(var i=0;i<d.length;i++) d[i].className=(i<pin.length)?'on':'';
 inp.value=pin;
 go.disabled=busy||pin.length!==6;
}
function set(v){ if(busy) return; pin=String(v).replace(/[^0-9]/g,'').slice(0,6); paint();
 if(pin.length===6) submit(); }
function fail(m){
 pin=''; paint(); err.textContent=m; err.className='msg bad';
 card.classList.remove('shake'); void card.offsetWidth; card.classList.add('shake');
}
async function submit(){
 if(busy||pin.length!==6) return;
 busy=true; go.classList.add('busy'); go.disabled=true;
 err.textContent=''; err.className='msg';
 var r=null;
 try{
  r=await fetch('/api/login',{method:'POST',credentials:'same-origin',cache:'no-store',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({pin:pin})});
 }catch(e){
  busy=false; go.classList.remove('busy');
  fail('No answer from the robot. Check you are still on its network.'); return;
 }
 busy=false; go.classList.remove('busy');
 if(r.ok||r.status===204){ location.href='/'; return; }
 if(r.status===429) fail('Too many attempts \\u2014 wait a minute and try again.');
 else if(r.status===503) fail('No admin PIN is set on this robot. Set EMORI_ADMIN_PIN and restart emori-admin.');
 else if(r.status===400) fail('The PIN is six digits.');
 else if(r.status===401) fail('Wrong PIN.');
 else fail('The robot refused the PIN (HTTP '+r.status+').');
}
$('pad').addEventListener('click',function(ev){
 var b=ev.target.closest('button[data-k]'); if(!b) return;
 var k=b.getAttribute('data-k');
 if(k==='del') set(pin.slice(0,-1));
 else if(k==='clr') set('');
 else set(pin+k);
});
// A physical/Bluetooth keyboard must work too: the visible field is readonly on
// purpose so the phone keyboard never fights the on-screen keypad for the
// bottom half of a 360px screen.
document.addEventListener('keydown',function(ev){
 if(ev.metaKey||ev.ctrlKey||ev.altKey) return;
 if(ev.key>='0'&&ev.key<='9'){ set(pin+ev.key); ev.preventDefault(); }
 else if(ev.key==='Backspace'){ set(pin.slice(0,-1)); ev.preventDefault(); }
 else if(ev.key==='Enter'){ submit(); ev.preventDefault(); }
});
go.addEventListener('click',submit);
paint();
})();
"""

LOGIN_HTML = (
    _HEAD
    + """<title>EMORI robot admin</title><style>"""
    + _BASE_CSS
    + _LOGIN_CSS
    + """</style></head><body>
<div class=box>
<div class=card id=card>
 <span class=mark aria-hidden=true><i></i></span>
 <h1>Robot admin</h1>
 <p class=who id=robotname>__ROBOT_NAME__</p>
 <p class=eyebrow style="text-align:center">Enter the 6-digit PIN</p>
 <div class=dots id=dots aria-hidden=true><i></i><i></i><i></i><i></i><i></i><i></i></div>
 <input id=pin type=password inputmode=numeric pattern="[0-9]*" maxlength=6 readonly
  autocomplete=one-time-code aria-label="Admin PIN">
 <div class=pad id=pad>
  <button class=key type=button data-k=1>1</button>
  <button class=key type=button data-k=2>2</button>
  <button class=key type=button data-k=3>3</button>
  <button class=key type=button data-k=4>4</button>
  <button class=key type=button data-k=5>5</button>
  <button class=key type=button data-k=6>6</button>
  <button class=key type=button data-k=7>7</button>
  <button class=key type=button data-k=8>8</button>
  <button class=key type=button data-k=9>9</button>
  <button class="key aux" type=button data-k=clr>Clear</button>
  <button class=key type=button data-k=0>0</button>
  <button class="key aux" type=button data-k=del>Delete</button>
 </div>
 <p class=msg id=err aria-live=polite>__ERROR__</p>
 <button class="btn wide" id=go type=button disabled>Unlock</button>
 <p class=foot>Access lasts 12 hours on this phone.<br>
  L&rsquo;acc&eacute;s dura 12&nbsp;hores en aquest tel&egrave;fon.</p>
</div>
</div>
<script>"""
    + _LOGIN_JS
    + """</script></body></html>"""
)


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------
_APP_CSS = """
body{padding:0 0 40px}
#chrome{position:sticky;top:0;z-index:30;background:var(--bg);
 border-bottom:1px solid var(--bd);padding-top:env(safe-area-inset-top)}
header.top{display:flex;gap:11px;align-items:center;padding:11px 0 9px}
.idbox{flex:1 1 auto;min-width:0}
.idbox .eyebrow{margin:0}
.idbox strong{display:block;font-size:15px;line-height:1.2;white-space:nowrap;
 overflow:hidden;text-overflow:ellipsis}
.iconbtn{width:44px;height:44px;border-radius:12px;border:1px solid var(--bd);
 background:var(--card);display:grid;place-items:center;flex:0 0 auto;color:var(--mut)}
.iconbtn:active{background:var(--priw)}
nav.tabs{display:flex;gap:7px;overflow-x:auto;padding:0 0 10px;scrollbar-width:none;
 -webkit-overflow-scrolling:touch}
nav.tabs::-webkit-scrollbar{display:none}
.tab{flex:0 0 auto;min-height:44px;padding:0 15px;border-radius:999px;border:1px solid var(--bd);
 background:var(--card);color:var(--mut);font-weight:800;font-size:13.5px;
 display:inline-flex;align-items:center;gap:7px;transition:background .18s,color .18s}
.tab[aria-selected=true]{background:var(--pri);border-color:var(--pri);color:var(--fg-on-pri)}
.tab .bdg{width:7px;height:7px;border-radius:50%;background:var(--badd);display:none}
.tab.alert .bdg{display:block}
.tab[aria-selected=true].alert .bdg{background:#fff}
main{padding-top:16px}
.view{display:none}
.view.on{display:block;animation:fade .2s ease-out}
@keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.shead{margin:0 2px 13px}
.rows{display:block}
.row{display:flex;justify-content:space-between;gap:14px;align-items:baseline;
 padding:9px 0;border-bottom:1px solid var(--bd);font-size:14px}
.row:last-child{border-bottom:0}
.row:first-child{padding-top:2px}
.row .k{color:var(--mut);flex:0 1 auto}
.row .v{font-weight:700;text-align:right;font-variant-numeric:tabular-nums;
 flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.row .v.ok{color:var(--ok)}.row .v.bad{color:var(--bad)}.row .v.warn{color:var(--warn)}
.row .v.mut{color:var(--mut);font-weight:600}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(96px,1fr));gap:9px;margin-top:12px}
.tile{background:var(--card2);border-radius:var(--rs);padding:11px 12px}
.tile .k{display:block;font-size:11px;color:var(--mut);font-weight:700;
 text-transform:uppercase;letter-spacing:.05em}
.tile .v{display:block;font-size:1.15rem;font-weight:800;font-variant-numeric:tabular-nums;
 margin-top:3px;line-height:1.15}
.tile .t{display:block;font-size:11px;color:var(--mut);margin-top:2px}
.tile.ok .v{color:var(--ok)}.tile.warn .v{color:var(--warn)}.tile.bad .v{color:var(--bad)}
.bar{height:6px;border-radius:4px;background:var(--priw);margin-top:7px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:4px;background:var(--pri)}
.bar i.ok{background:var(--okd)}.bar i.warn{background:var(--warnd)}.bar i.bad{background:var(--badd)}
.ban{border-radius:0 12px 12px 0;border-left:3px solid var(--pri);background:var(--priw);
 padding:11px 13px;font-size:13.5px;margin:0 0 12px;color:var(--ink)}
.ban b{display:block;margin-bottom:2px}
.ban.bad{border-left-color:var(--badd);background:var(--badw)}
.ban.warn{border-left-color:var(--warnd);background:var(--warnw)}
.ban .mono{font-size:12px;color:var(--mut);display:block;margin-top:4px}
.ban ol{margin:8px 0 0;padding-left:20px}
.ban li{margin-top:5px}
.ban code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;
 background:var(--card2);border-radius:5px;padding:1px 5px;overflow-wrap:anywhere}
.note{font-size:12.5px;color:var(--mut);background:var(--card2);border-radius:10px;
 padding:9px 11px;margin-top:10px}
.steps{list-style:none;margin:12px 0 0;padding:0;display:grid;gap:8px}
.steps li{display:flex;gap:11px;align-items:center;padding:9px 10px;border-radius:12px;
 background:var(--card2)}
.steps .n{width:26px;height:26px;border-radius:50%;flex:0 0 auto;display:grid;place-items:center;
 font-size:12px;font-weight:800;background:var(--card);border:1px solid var(--bd2);color:var(--mut)}
.steps li.done .n{background:var(--okd);border-color:var(--okd);color:#0B2620}
.steps .lb{flex:1 1 auto;font-size:13.5px;font-weight:700}
.steps .lb small{display:block;font-weight:600;color:var(--mut);font-size:11.5px}
.steps li.done .lb{color:var(--mut)}
.list{display:grid;gap:8px;margin-top:10px}
.item{display:flex;gap:11px;align-items:center;padding:10px 11px;border-radius:12px;
 background:var(--card2);flex-wrap:wrap}
.item .grow{flex:1 1 130px;min-width:0}
.item .t1{font-weight:800;font-size:14px;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis}
.item .t2{font-size:11.5px;color:var(--mut);margin-top:1px}
.item .t2.bad{color:var(--bad);font-weight:700}
.item .acts{display:flex;gap:7px;flex:0 0 auto}
.sig{display:inline-flex;gap:2px;align-items:flex-end;height:13px;vertical-align:-2px;
 margin-right:5px}
.sig i{width:3px;background:var(--bd2);border-radius:1px;display:block}
.sig i:nth-child(1){height:4px}.sig i:nth-child(2){height:7px}
.sig i:nth-child(3){height:10px}.sig i:nth-child(4){height:13px}
.sig i.on{background:var(--pri)}
.big{font-size:1.5rem;font-weight:800;font-variant-numeric:tabular-nums}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:44px;
 background:transparent;margin:4px 0 0;display:block}
input[type=range]::-webkit-slider-runnable-track{height:10px;border-radius:6px;
 background:var(--priw);border:1px solid var(--bd)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:28px;height:28px;
 margin-top:-10px;border-radius:50%;background:var(--pri);border:3px solid var(--card);
 box-shadow:0 1px 5px rgba(0,0,0,.3)}
input[type=range]::-moz-range-track{height:10px;border-radius:6px;background:var(--priw);
 border:1px solid var(--bd)}
input[type=range]::-moz-range-thumb{width:24px;height:24px;border-radius:50%;
 background:var(--pri);border:3px solid var(--card);box-shadow:0 1px 5px rgba(0,0,0,.3)}
input[type=range][disabled]{opacity:.5}
.scale{display:flex;justify-content:space-between;font-size:11px;color:var(--mut);
 margin-top:-2px}
.btnrow{display:flex;gap:9px;flex-wrap:wrap;margin-top:12px}
details.card>summary{list-style:none;min-height:44px;display:flex;align-items:center;
 justify-content:space-between;font-weight:800;font-size:.95rem;cursor:pointer}
details.card>summary::-webkit-details-marker{display:none}
details.card>summary::after{content:'+';font-size:1.2rem;color:var(--mut);font-weight:700}
details.card[open]>summary::after{content:'\\2212'}
.zone{border-color:var(--bd2);background:linear-gradient(var(--badw),var(--badw)),var(--card)}
.zone h2{color:var(--bad)}
.langgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.langbtn{min-height:88px;border-radius:14px;border:1px solid var(--bd2);background:var(--card2);
 display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
 font-weight:800;font-size:1.4rem;color:var(--ink);transition:background .18s,border-color .18s}
.langbtn small{font-size:11.5px;font-weight:700;color:var(--mut);text-transform:uppercase;
 letter-spacing:.06em}
.langbtn[aria-pressed=true]{background:var(--pri);border-color:var(--pri);color:var(--fg-on-pri)}
.langbtn[aria-pressed=true] small{color:var(--fg-on-pri);opacity:.8}
.langbtn[disabled]{opacity:.55}
.gates{display:grid;gap:7px;margin-top:12px}
.gate{border-radius:11px;background:var(--card2);padding:9px 11px;
 border-left:3px solid var(--bd2)}
.gate .gh{display:flex;gap:9px;align-items:center}
.gate .gs{font-size:10.5px;font-weight:800;letter-spacing:.07em;width:44px;flex:0 0 auto}
.gate .gn{font-weight:800;font-size:13.5px;flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.gate .gc{font-size:11px;color:var(--mut);font-family:ui-monospace,Menlo,Consolas,monospace}
.gate .gt{font-size:12.5px;color:var(--mut);margin-top:5px;padding-left:53px}
.gate.pass{border-left-color:var(--okd)}.gate.pass .gs{color:var(--ok)}
.gate.warn{border-left-color:var(--warnd)}.gate.warn .gs{color:var(--warn)}
.gate.fail{border-left-color:var(--badd);background:var(--badw)}.gate.fail .gs{color:var(--bad)}
.gate.skip .gs{color:var(--mut)}
.seg{display:flex;gap:0;border:1px solid var(--bd2);border-radius:11px;overflow:hidden}
.seg button{flex:1 1 0;min-height:44px;border:0;background:var(--card);color:var(--mut);
 font-weight:800;font-size:13px;border-right:1px solid var(--bd)}
.seg button:last-child{border-right:0}
.seg button[aria-pressed=true]{background:var(--pri);color:var(--fg-on-pri)}
.field{margin-top:12px}
.field label{display:block;font-size:12px;font-weight:800;color:var(--slate);margin:0 0 6px;
 text-transform:uppercase;letter-spacing:.05em}
.pwrow{display:flex;gap:8px}
.pwrow input{flex:1 1 auto;min-width:0}
.pwrow button{flex:0 0 auto}
.logs{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px;
 line-height:1.55;white-space:pre-wrap;overflow-wrap:anywhere;max-height:58vh;overflow:auto;
 background:var(--card2);border:1px solid var(--bd);border-radius:12px;padding:10px;
 margin-top:12px;-webkit-overflow-scrolling:touch}
.logs div{padding:1px 0}
.logs .e{color:var(--bad)}
.logs .w{color:var(--warn)}
.busyhead{display:flex;gap:10px;align-items:center}
.spin2{width:18px;height:18px;border-radius:50%;flex:0 0 auto;border:2px solid var(--bd2);
 border-top-color:var(--pri);animation:sp .8s linear infinite}
dialog{border:0;padding:0;background:transparent;max-width:min(94vw,420px);width:100%;
 color:var(--ink)}
/* Only engines that implement <dialog> ship the UA rule that hides a closed one
   (iOS Safari < 15.4 does not), and there it is an unknown element with no
   display:none -- so the confirm card and the whole join-network form would sit
   at the bottom of every tab from first paint. dlgOpen() already sets [open]. */
dialog:not([open]){display:none}
dialog::backdrop{background:rgba(10,12,16,.55)}
dialog .dcard{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:18px;
 box-shadow:var(--sh2)}
dialog h2{margin-bottom:8px}
dialog .dbody{font-size:13.5px;color:var(--mut)}
dialog .dbody ul{margin:8px 0 0;padding-left:18px}
dialog .dbody li{margin:5px 0}
dialog .dbody code{background:var(--priw);color:var(--pri);border-radius:6px;padding:1px 5px;
 font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
dialog .dacts{display:flex;gap:9px;margin-top:16px}
dialog .dacts .btn{flex:1 1 0}
dialog.fb{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:80;
 max-height:92vh;overflow:auto}
.fbback{position:fixed;inset:0;background:rgba(10,12,16,.55);z-index:70;display:none}
.fbback.on{display:block}
.toast{position:fixed;left:50%;bottom:calc(18px + env(safe-area-inset-bottom));
 transform:translateX(-50%) translateY(10px);opacity:0;pointer-events:none;z-index:90;
 background:var(--ink);color:var(--bg);font-size:13px;font-weight:700;padding:11px 16px;
 border-radius:999px;box-shadow:var(--sh2);max-width:90vw;text-align:center;
 transition:opacity .2s,transform .2s}
.toast.on{opacity:1;transform:translateX(-50%)}
.toast.bad{background:var(--badd);color:#fff}
.foot{font-size:12px;color:var(--mut);margin:16px 2px 0;text-align:center}
input[type=email]{width:100%;min-height:46px;padding:0 13px;border-radius:12px;
 border:1px solid var(--bd2);background:var(--card);color:var(--ink)}
textarea{font:inherit;color:var(--ink);width:100%;padding:10px 13px;border-radius:12px;
 border:1px solid var(--bd2);background:var(--card);resize:vertical;min-height:72px}
textarea.mono{font-size:12.5px}
textarea:focus-visible{outline:2px solid var(--pri);outline-offset:2px}
.steps li.warn .n{background:var(--warnd);border-color:var(--warnd);color:#2B1B00}
.steps li.opt .n{background:transparent;border-style:dashed}
.src{display:inline-flex;align-items:center;border-radius:999px;padding:2px 8px;margin-left:6px;
 font-size:10.5px;font-weight:800;letter-spacing:.03em;text-transform:none;vertical-align:1px;
 background:var(--priw);color:var(--mut)}
.src.admin{background:var(--okw);color:var(--ok)}
.src.pokamind{background:var(--priw);color:var(--pri)}
.src.pending{background:var(--warnw);color:var(--warn)}
.src.none{background:transparent;color:var(--mut);border:1px dashed var(--bd2)}
.hint{font-size:12px;color:var(--mut);margin:5px 0 0}
.hint.bad{color:var(--bad);font-weight:700}
.chk{display:flex;gap:10px;align-items:flex-start;margin-top:12px;font-size:13.5px}
.chk input{width:22px;height:22px;flex:0 0 auto;margin:1px 0 0}
.opt{display:grid;gap:8px;padding:11px;border-radius:12px;background:var(--card2)}
.opt .oh{display:flex;gap:9px;align-items:center}
.opt .on{width:28px;height:28px;border-radius:50%;flex:0 0 auto;display:grid;place-items:center;
 font-weight:800;font-size:13px;background:var(--pri);color:var(--fg-on-pri)}
.opt .ot{flex:1 1 auto;min-width:0;font-size:12px;color:var(--mut);overflow-wrap:anywhere}
.opt .acts{display:flex;gap:6px;flex:0 0 auto}
.opt .acts .btn{min-width:44px;padding:0 10px}
.secret{background:var(--card2);border:1px solid var(--bd2);border-radius:12px;padding:10px 12px;
 margin-top:12px}
.badgeimg{background:#fff;border-radius:12px;border:1px solid var(--bd);padding:8px;
 margin-top:10px;display:grid;place-items:center}
.badgeimg img{display:block;width:100%;max-width:300px;height:auto;min-height:120px}
@media (min-width:600px){
 .tiles{grid-template-columns:repeat(auto-fit,minmax(120px,1fr))}
 h1{font-size:1.5rem}
}
"""

_APP_BODY = """
<div class=fbback id=fbback></div>
<div id=chrome><div class=wrap>
 <header class=top>
  <span class=mark aria-hidden=true><i></i></span>
  <div class=idbox>
   <p class=eyebrow>EMORI robot admin</p>
   <strong id=robotname>__ROBOT_NAME__</strong>
  </div>
  <span class="chip mut" id=hchip><i class="dot idle" id=hchipDot></i><span id=hchipText>checking</span></span>
  <button class=iconbtn id=signout type=button aria-label="Sign out" title="Sign out">
   <svg viewBox="0 0 24 24" width=19 height=19 aria-hidden=true fill=none stroke=currentColor
    stroke-width=2 stroke-linecap=round stroke-linejoin=round><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/></svg>
  </button>
 </header>
 <nav class=tabs id=tabs role=tablist aria-label=Sections>
  <button class=tab role=tab type=button data-tab=home aria-controls=v-home>Home<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=network aria-controls=v-network>Network<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=audio aria-controls=v-audio>Audio<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=lang aria-controls=v-lang>Language<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=people aria-controls=v-people>People<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=pokamind aria-controls=v-pokamind>Pokamind<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=menu aria-controls=v-menu>Menu<i class=bdg></i></button>
  __ADDON_TAB__
  <button class=tab role=tab type=button data-tab=settings aria-controls=v-settings>Settings<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=services aria-controls=v-services>Services<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=health aria-controls=v-health>Health<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=logs aria-controls=v-logs>Logs<i class=bdg></i></button>
  <button class=tab role=tab type=button data-tab=system aria-controls=v-system>System<i class=bdg></i></button>
 </nav>
</div></div>

<main class=wrap id=main>

<section class="view on" id=v-home role=tabpanel tabindex=0 aria-label=Home>
 <div class=shead><p class=eyebrow>Status</p><h1>Home</h1>
  <p class=lead id=homeStamp>Reading the robot&hellip;</p></div>
 <div id=homeBan></div>
 <div id=homeApply></div>
 <div class=card>
  <div class=chead><h2>Site setup</h2><span class="chip mut" id=setupChip>&mdash;</span></div>
  <p class=sub>Read live from the robot: its network, its Pokamind token and record, the
   spoken menu, people and the last readycheck.</p>
  <ol class=steps id=steps><li><div class=skel style="flex:1 1 auto"></div></li></ol>
 </div>
 <div class=card>
  <div class=chead><h2>Network</h2>
   <button class="btn ghost sm" type=button data-go=network>Manage</button></div>
  <div class=rows id=homeNet><div class=skel></div><div class=skel></div><div class=skel></div></div>
 </div>
 <div class=card>
  <div class=chead><h2>Services</h2>
   <button class="btn ghost sm" type=button data-go=services>Manage</button></div>
  <div class=rows id=homeSvc><div class=skel></div><div class=skel></div><div class=skel></div></div>
 </div>
 <div class=card>
  <div class=chead><h2>Robot &amp; cloud</h2>
   <button class="btn ghost sm" type=button data-go=health>Check</button></div>
  <div class=rows id=homeLink><div class=skel></div><div class=skel></div></div>
 </div>
 <div class=card>
  <div class=chead><h2>Session settings</h2>
   <button class="btn ghost sm" type=button data-go=audio>Adjust</button></div>
  <div class=rows id=homeSess><div class=skel></div><div class=skel></div></div>
 </div>
 <div class=card>
  <div class=chead><h2>System</h2>
   <button class="btn ghost sm" type=button data-go=system>Details</button></div>
  <div class=tiles id=homeSys></div>
 </div>
</section>

<section class=view id=v-network role=tabpanel tabindex=0 aria-label=Network>
 <div class=shead><p class=eyebrow>Wi-Fi</p><h1>Network</h1>
  <p class=lead>Sessions need internet. Without it only this admin page keeps working.</p></div>
 <div id=netErrBan></div>
 <div id=netFailBan aria-live=polite></div>
 <div class=card id=netBusy hidden>
  <div class=busyhead><span class=spin2></span><h2 id=netBusyTitle>Working&hellip;</h2></div>
  <p class=sub id=netBusyText></p>
  <p class="sub mono" id=netBusyElapsed></p>
 </div>
 <div class=card>
  <div class=chead><h2>Current connection</h2><span class="chip mut" id=netChip>&mdash;</span></div>
  <div class=rows id=netRows><div class=skel></div><div class=skel></div><div class=skel></div></div>
 </div>
 <div class=card>
  <div class=chead><h2>Available networks</h2>
   <button class="btn sm" id=scanBtn type=button>Scan</button></div>
  <p class=sub>Scanning takes about ten seconds.</p>
  <div id=scanList><p class=empty>Tap <b>Scan</b> to look for Wi-Fi near the robot.</p></div>
  <p class=msg id=scanMsg aria-live=polite></p>
  <button class="btn ghost wide" id=otherBtn type=button>Join a hidden network&hellip;</button>
 </div>
 <div class=card>
  <div class=chead><h2>Saved networks</h2></div>
  <p class=sub>The robot rejoins these on its own after a reboot.</p>
  <div id=savedList><div class=skel></div><div class=skel></div></div>
  <p class=msg id=savedMsg aria-live=polite></p>
 </div>
 <div class="card zone">
  <div class=chead><h2>Hotspot mode</h2></div>
  <p class=sub>The robot stops using the site Wi-Fi and broadcasts its own network instead.
   Use this only when you have no other way to reach it.</p>
  <button class="btn danger wide" id=hotspotBtn type=button>Start hotspot now</button>
  <p class=msg id=hotspotMsg aria-live=polite></p>
 </div>
</section>

<section class=view id=v-audio role=tabpanel tabindex=0 aria-label=Audio>
 <div class=shead><p class=eyebrow>Levels</p><h1>Audio</h1>
  <p class=lead>Set these once per room. They are saved on the robot and re-applied
   after every reboot.</p></div>
 <div id=audFailBan aria-live=polite></div>
 <div class=card>
  <div class=chead><h2>Speaker</h2><span class=big id=spkVal>&mdash;</span></div>
  <input type=range id=spk min=0 max=100 step=1 value=0 aria-label="Speaker volume" disabled>
  <div class=scale><span>quiet</span><span>loud</span></div>
  <div class=btnrow><button class="btn ghost" id=testBtn type=button>Play test sound</button></div>
  <p class=msg id=spkMsg aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>Microphone</h2><span class=big id=micVal>&mdash;</span></div>
  <input type=range id=mic min=0 max=100 step=1 value=0 aria-label="Microphone volume" disabled>
  <div class=scale><span>low</span><span>high</span></div>
  <p class=note>There is no live level meter on purpose: the host app owns the microphone and
   opening it a second time can take the session down. Verify the microphone with
   <b>Health &rarr; Run readycheck</b>.</p>
  <p class=msg id=micMsg aria-live=polite></p>
 </div>
 <details class=card id=advCard>
  <summary>Advanced &middot; mic profile multipliers</summary>
  <p class=sub>The host app raises the microphone while the visitor speaks and lowers it
   while the robot speaks. These multiply the level above; 1.00 leaves it alone. Raise the
   session value in a noisy room, lower the idle value if the robot hears itself.</p>
  <div class=rows style="margin-top:10px">
   <div class=row><span class=k>Applied profile now</span><span class="v mut" id=advApplied>&mdash;</span></div>
  </div>
  <div class=field>
   <label for=msess>While the visitor speaks <span id=msessVal class=mono>&mdash;</span></label>
   <input type=range id=msess min=0 max=4 step=0.05 value=1 aria-label="Session mic multiplier">
  </div>
  <div class=field>
   <label for=midle>While the robot speaks <span id=midleVal class=mono>&mdash;</span></label>
   <input type=range id=midle min=0 max=4 step=0.05 value=1 aria-label="Idle mic multiplier">
  </div>
  <div class=btnrow>
   <button class=btn id=advSave type=button>Save multipliers</button>
   <button class="btn ghost" id=advReset type=button>Back to defaults</button>
  </div>
  <p class=msg id=advMsg aria-live=polite></p>
 </details>
</section>

<section class=view id=v-lang role=tabpanel tabindex=0 aria-label=Language>
 <div class=shead><p class=eyebrow>Kiosk</p><h1>Language</h1>
  <p class=lead>The language the robot speaks and shows on its screen.</p></div>
 <div class=card>
  <div class=chead><h2>Current</h2><span class="chip mut" id=langChip>&mdash;</span></div>
  <div class=langgrid>
   <button class=langbtn id=langEn type=button aria-pressed=false>EN<small>English</small></button>
   <button class=langbtn id=langCa type=button aria-pressed=false>CA<small>Catal&agrave;</small></button>
  </div>
  <p class=sub id=langScope></p>
  <p class=note>The robot says the change out loud and reloads its screen. It refuses while a
   visitor session is running &mdash; wait for the robot to go idle.</p>
  <p class=msg id=langMsg aria-live=polite></p>
 </div>
</section>

<section class=view id=v-people role=tabpanel tabindex=0 aria-label=People>
 <div class=shead><p class=eyebrow>Badges</p><h1>People</h1>
  <p class=lead>Who can start their own training on this robot. Each person gets a printed QR
   badge; they show it to the robot's camera to begin.</p></div>
 <div id=ppBan></div>
 <div class=card>
  <div class=chead><h2>Add a person</h2></div>
  <div class=field><label for=ppName>Name</label>
   <input type=text id=ppName maxlength=80 autocomplete=off autocapitalize=words spellcheck=false></div>
  <div class=field><label for=ppEmail>Work email</label>
   <input type=email id=ppEmail maxlength=254 inputmode=email autocomplete=off autocapitalize=none
    autocorrect=off spellcheck=false></div>
  <div class=field id=ppPathBox hidden><label for=ppPath>Pathway</label>
   <select id=ppPath></select></div>
  <p class=sub id=ppDomains></p>
  <p class=sub id=ppCourse></p>
  <button class="btn wide" id=ppAdd type=button>Add and make a badge</button>
  <p class=msg id=ppMsg aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>On the roster</h2><span class="chip mut" id=ppChip>&mdash;</span></div>
  <div class=btnrow><a class="btn ghost" id=ppPrintAll href="/api/people/sheet" target=_blank
   rel=noopener hidden>Print all badges</a></div>
  <div class=list id=ppList><div class=skel></div><div class=skel></div></div>
  <p class=msg id=ppListMsg aria-live=polite></p>
 </div>
</section>

<section class=view id=v-pokamind role=tabpanel tabindex=0 aria-label=Pokamind>
 <div class=shead><p class=eyebrow>Cloud account</p><h1>Pokamind</h1>
  <p class=lead>This robot's own Pokamind token: who the robot is to Pokamind, and which
   training it may start.</p></div>
 <div id=pkBan></div>
 <div class=card>
  <div class=chead><h2>Token</h2><span class="chip mut" id=pkChip>&mdash;</span></div>
  <div class=rows id=pkRows><div class=skel></div><div class=skel></div><div class=skel></div></div>
  <div class=btnrow>
   <button class=btn id=pkTest type=button>Test connection</button>
   <button class="btn ghost" id=pkReplace type=button>Replace token</button>
   <button class="btn ghost" id=pkReveal type=button>Reveal</button>
  </div>
  <div class=field id=pkRevealBox hidden>
   <label for=pkRevealed>Full token <span class=mono id=pkRevealLeft></span></label>
   <input type=text id=pkRevealed class=mono readonly autocomplete=off autocapitalize=none
    autocorrect=off spellcheck=false>
  </div>
  <p class=msg id=pkMsg aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>Scope</h2><span class="chip mut" id=pkScopeChip>&mdash;</span></div>
  <p class=sub>What Pokamind lets this robot start, as of the last Test connection.</p>
  <div class=rows id=pkScope style="margin-top:8px"></div>
  <div class=list id=pkModules></div>
 </div>
 <div class=card>
  <div class=chead><h2>Applied from Pokamind</h2><span class="chip mut" id=pkAppChip>&mdash;</span></div>
  <p class=sub>What this robot's Pokamind record sets on the robot. A value in
   <code>.env</code> or saved on this page wins over it.</p>
  <div class=rows id=pkApplied style="margin-top:8px"><div class=skel></div><div class=skel></div></div>
  <div class=btnrow><button class=btn id=pkSync type=button>Sync now</button></div>
  <p class=msg id=pkSyncMsg aria-live=polite></p>
 </div>
</section>

<section class=view id=v-menu role=tabpanel tabindex=0 aria-label=Menu>
 <div class=shead><p class=eyebrow>Voice</p><h1>Spoken menu</h1>
  <p class=lead>What the robot offers a visitor: &ldquo;say one for &hellip;, two for &hellip;&rdquo;. It is
   built from this robot's Pokamind record; rename the options here if the drafted names are
   hard to say.</p></div>
 <div class=card>
  <div class=seg id=mnLang>
   <button type=button data-lang=ca aria-pressed=false>Catal&agrave;</button>
   <button type=button data-lang=en aria-pressed=false>English</button>
  </div>
  <div class=chead style="margin-top:12px"><h2>Served now</h2><span class="chip mut" id=mnChip>&mdash;</span></div>
  <p class=sub id=mnWhere></p>
  <div id=mnBan></div>
  <p class=msg id=mnLoadMsg aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>Options</h2><span class="chip mut" id=mnCount>&mdash;</span></div>
  <div class=field><label for=mnPrompt>What the robot says first</label>
   <textarea id=mnPrompt rows=3 maxlength=600 autocomplete=off spellcheck=false></textarea></div>
  <button class="btn ghost sm" id=mnPromptGen type=button style="margin-top:8px">Write it from the names</button>
  <div class=list id=mnOpts></div>
  <div class=field id=mnAddBox hidden><label for=mnAdd>Offer another module</label>
   <div class=pwrow><select id=mnAdd></select>
    <button class="btn ghost sm" id=mnAddBtn type=button>Add</button></div></div>
  <div class=btnrow>
   <button class=btn id=mnSave type=button>Save</button>
   <button class="btn ghost" id=mnPreview type=button>Preview</button>
   <button class="btn ghost" id=mnReset type=button>Reset to Pokamind</button>
  </div>
  <p class=sub>Keywords are extra words a visitor may say instead of the number, separated by
   commas. A number always works.</p>
  <p class=msg id=mnMsg aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>This robot's modules</h2><span class="chip mut" id=mnModChip>&mdash;</span></div>
  <p class=sub>From this robot's Pokamind record, in its order. The menu offers the available
   modules of this language, six at most.</p>
  <div class=list id=mnMods></div>
 </div>
</section>

__ADDON_VIEW__

<section class=view id=v-settings role=tabpanel tabindex=0 aria-label=Settings>
 <div class=shead><p class=eyebrow>This robot</p><h1>Settings</h1>
  <p class=lead>Optional, robot-local settings. Each shows where its value comes from; a value
   saved here wins over <code>.env</code> and over Pokamind.</p></div>
 <div id=stApply></div>
 <div id=stBan></div>
 <div class=card>
  <div class=chead><h2>Results e-mail</h2></div>
  <p class=sub>Only for a site that wants the robot to e-mail its own engagement report.
   Pokamind e-mails learners their results without this.</p>
  <div id=stGroup-mail></div>
  <div class=btnrow><button class=btn type=button data-stsave=mail>Save</button></div>
  <p class=msg id=stMsg-mail aria-live=polite></p>
  <div class=field><label for=stTestTo>Send a test e-mail to</label>
   <div class=pwrow><input type=email id=stTestTo maxlength=320 inputmode=email autocomplete=off
    autocapitalize=none autocorrect=off spellcheck=false>
    <button class="btn ghost sm" id=stTest type=button>Send test</button></div></div>
  <p class=hint>Uses the settings as saved, before they are applied.</p>
  <p class=msg id=stTestMsg aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>Analytics</h2></div>
  <p class=sub>Sends anonymous engagement data to a FIWARE context broker the site runs.</p>
  <div id=stGroup-fiware></div>
  <div class=btnrow><button class=btn type=button data-stsave=fiware>Save</button></div>
  <p class=msg id=stMsg-fiware aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>Data</h2></div>
  <div id=stGroup-data></div>
  <div class=btnrow><button class=btn type=button data-stsave=data>Save</button></div>
  <p class=msg id=stMsg-data aria-live=polite></p>
 </div>
 <details class=card id=stTuningCard>
  <summary>Hardware tuning (advanced)</summary>
  <p class=sub>Measured on this kind of robot: an empty box uses the value its hardware supplies.
   Change one only to correct this robot or this room.</p>
  <div id=stGroup-tuning></div>
  <div class=btnrow><button class=btn type=button data-stsave=tuning>Save</button></div>
  <p class=msg id=stMsg-tuning aria-live=polite></p>
 </details>
 <details class=card id=stServerCard>
  <summary>Server (advanced)</summary>
  <p class=sub>The Pokamind address this robot signs in to. A new one is saved only if
   Pokamind answers there with this robot's own token. Asks for the PIN.</p>
  <div id=stGroup-server></div>
  <div class=btnrow><button class=btn type=button data-stsave=server>Save</button></div>
  <p class=msg id=stMsg-server aria-live=polite></p>
 </details>
 <div class=card>
  <div class=chead><h2>Credentials</h2></div>
  <p class=sub>Generated on this robot when it was installed. Each action asks for the PIN.</p>
  <div class=list id=stCreds></div>
  <div class=secret id=stSecret hidden>
   <p class=eyebrow id=stSecretTitle>Shown</p>
   <div class=rows id=stSecretRows></div>
   <p class=hint id=stSecretLeft></p>
  </div>
  <p class=msg id=stCredMsg aria-live=polite></p>
 </div>
 <div class=card>
  <div class=chead><h2>Applying changes</h2><span class="chip mut" id=stAutoChip>&mdash;</span></div>
  <div class=rows id=stAuto></div>
 </div>
</section>

<section class=view id=v-services role=tabpanel tabindex=0 aria-label=Services>
 <div class=shead><p class=eyebrow>systemd</p><h1>Services</h1>
  <p class=lead>Restarting is safe between visitors. Nothing here is lost on restart.</p></div>
 <div class=card>
  <div class=chead><h2>Units</h2>
   <button class="btn ghost sm" id=svcRefresh type=button>Refresh</button></div>
  <div class=list id=svcList><div class=skel></div><div class=skel></div><div class=skel></div></div>
  <p class=msg id=svcMsg aria-live=polite></p>
 </div>
 <div class=card id=hubBack hidden>
  <div class=chead><h2>Back to the host app</h2></div>
  <p class=note id=hubBackNote></p>
  <button class="btn sm" id=hubBackGo type=button>Back to the host app</button>
  <p class=msg id=hubBackMsg aria-live=polite></p>
 </div>
</section>

<section class=view id=v-health role=tabpanel tabindex=0 aria-label=Health>
 <div class=shead><p class=eyebrow>Readycheck</p><h1>Health</h1>
  <p class=lead>The full pre-flight check: audio, motors, cloud and the voice menu.
   It takes up to two minutes and speaks nothing.</p></div>
 <div class=card>
  <div class=chead><h2>Run</h2><span class="chip mut" id=readyChip>never run</span></div>
  <button class="btn wide" id=readyRun type=button>Run readycheck</button>
  <p class=msg id=readyMsg aria-live=polite></p>
  <div class=tiles id=readyTiles hidden></div>
  <div class=gates id=readyGates></div>
 </div>
</section>

<section class=view id=v-logs role=tabpanel tabindex=0 aria-label=Logs>
 <div class=shead><p class=eyebrow>journalctl</p><h1>Logs</h1>
  <p class=lead>The newest lines first-hand from the robot journal.</p></div>
 <div class=card>
  <div class=field><label for=logUnit>Service</label>
   <select id=logUnit></select></div>
  <div class=field><label for=logLines>Lines</label>
   <div class=seg id=logLines>
    <button type=button data-n=100 aria-pressed=false>100</button>
    <button type=button data-n=200 aria-pressed=true>200</button>
    <button type=button data-n=500 aria-pressed=false>500</button>
   </div></div>
  <div class=btnrow><button class="btn wide" id=logRefresh type=button>Show logs</button></div>
  <p class=msg id=logMsg aria-live=polite></p>
  <div class=logs id=logOut><p class=empty>Pick a service and tap <b>Show logs</b>.</p></div>
 </div>
</section>

<section class=view id=v-system role=tabpanel tabindex=0 aria-label=System>
 <div class=shead><p class=eyebrow>Hardware</p><h1>System</h1>
  <p class=lead>What the robot's own operating system reports.</p></div>
 <div class=card>
  <div class=chead><h2>Now</h2>
   <button class="btn ghost sm" id=sysRefresh type=button>Refresh</button></div>
  <div class=tiles id=sysTiles></div>
  <div class=rows id=sysRows style="margin-top:14px"><div class=skel></div><div class=skel></div></div>
  <p class=msg id=sysMsg aria-live=polite></p>
 </div>
 <div class="card zone">
  <div class=chead><h2>Danger zone</h2></div>
  <p class=sub>A reboot takes about ninety seconds and the robot comes back on its own.
   A shutdown needs someone to press the power button on site.</p>
  <div class=btnrow>
   <button class="btn dghost" id=rebootBtn type=button>Reboot</button>
   <button class="btn dghost" id=offBtn type=button>Shut down</button>
  </div>
  <p class=msg id=dangerMsg aria-live=polite></p>
 </div>
 <p class=foot>EMORI robot admin</p>
</section>

</main>

<div class=toast id=toast role=status aria-live=polite></div>

<dialog id=cdlg aria-labelledby=cdTitle><div class=dcard>
 <h2 id=cdTitle>Confirm</h2>
 <div class=dbody id=cdBody></div>
 <div class=field id=cdWordBox hidden>
  <label for=cdWord id=cdWordLabel>Type to confirm</label>
  <input type=text id=cdWord autocomplete=off autocapitalize=none autocorrect=off spellcheck=false>
 </div>
 <div class=dacts>
  <button class="btn ghost" id=cdNo type=button>Cancel</button>
  <button class=btn id=cdGo type=button>Confirm</button>
 </div>
</div></dialog>

<dialog id=tkdlg aria-labelledby=tkTitle><div class=dcard>
 <h2 id=tkTitle>Replace the Pokamind token</h2>
 <div class=dbody><p>Paste this robot's own token, as Pokamind issued it. It starts with
  <code>pkr1.</code> and is one line.</p></div>
 <div class=field><label for=tkIn>Token</label>
  <div class=pwrow>
   <input type=password id=tkIn autocomplete=off autocapitalize=none autocorrect=off spellcheck=false>
   <button class="btn ghost sm" id=tkEye type=button aria-pressed=false>Show</button>
  </div></div>
 <p class=msg id=tkMsg aria-live=polite></p>
 <div class=dacts>
  <button class="btn ghost" id=tkNo type=button>Cancel</button>
  <button class=btn id=tkGo type=button>Continue</button>
 </div>
</div></dialog>

<dialog id=pindlg aria-labelledby=pinTitle><div class=dcard>
 <h2 id=pinTitle>Reveal the token</h2>
 <div class=dbody><p>Enter the admin PIN again. The token stays on screen for 30 seconds;
  anyone holding it can log learners into Pokamind as this robot.</p></div>
 <div class=field><label for=pinIn>Admin PIN</label>
  <input type=password id=pinIn inputmode=numeric pattern="[0-9]*" maxlength=6 autocomplete=off></div>
 <p class=msg id=pinMsg aria-live=polite></p>
 <div class=dacts>
  <button class="btn ghost" id=pinNo type=button>Cancel</button>
  <button class="btn danger" id=pinGo type=button>Reveal</button>
 </div>
</div></dialog>

<dialog id=ppdlg aria-labelledby=ppdTitle><div class=dcard>
 <h2 id=ppdTitle>Edit person</h2>
 <div class=dbody><p>The badge stays the same: no need to print it again unless the name changed.</p></div>
 <div class=field><label for=ppdName>Name</label>
  <input type=text id=ppdName maxlength=80 autocomplete=off autocapitalize=words spellcheck=false></div>
 <div class=field><label for=ppdEmail>Work email</label>
  <input type=email id=ppdEmail maxlength=254 inputmode=email autocomplete=off autocapitalize=none
   autocorrect=off spellcheck=false></div>
 <div class=field id=ppdPathBox hidden><label for=ppdPath>Pathway</label>
  <select id=ppdPath></select></div>
 <p class=msg id=ppdMsg aria-live=polite></p>
 <div class=dacts>
  <button class="btn ghost" id=ppdNo type=button>Cancel</button>
  <button class=btn id=ppdGo type=button>Save</button>
 </div>
</div></dialog>

<dialog id=bgdlg aria-labelledby=bgTitle><div class=dcard>
 <h2 id=bgTitle>Badge</h2>
 <div class=badgeimg><img id=bgImg alt=""></div>
 <p class=sub>Print at 100% on white paper and keep the white margin around the code. It works
  as soon as it is printed.</p>
 <div class=btnrow>
  <a class=btn id=bgPrint href="/api/people/sheet" target=_blank rel=noopener>Print</a>
  <a class="btn ghost" id=bgSvg href="/api/people/sheet" download>SVG</a>
  <a class="btn ghost" id=bgPng href="/api/people/sheet" download>PNG (code only)</a>
 </div>
 <div class=dacts><button class="btn ghost" id=bgClose type=button>Close</button></div>
</div></dialog>

<dialog id=fmdlg aria-labelledby=fmTitle><div class=dcard>
 <h2 id=fmTitle>Confirm</h2>
 <div class=dbody id=fmBody></div>
 <div id=fmFields></div>
 <p class=msg id=fmMsg aria-live=polite></p>
 <div class=dacts>
  <button class="btn ghost" id=fmNo type=button>Cancel</button>
  <button class=btn id=fmGo type=button>Confirm</button>
 </div>
</div></dialog>

<dialog id=jdlg aria-labelledby=jdTitle><div class=dcard>
 <h2 id=jdTitle>Join network</h2>
 <div id=jdStep1>
  <div class=field><label for=jdSsid>Network name</label>
   <input type=text id=jdSsid autocomplete=off autocapitalize=none autocorrect=off spellcheck=false></div>
  <div class=field><label for=jdPw>Password</label>
   <div class=pwrow>
    <input type=password id=jdPw autocomplete=off autocapitalize=none autocorrect=off spellcheck=false>
    <button class="btn ghost sm" id=jdEye type=button aria-pressed=false>Show</button>
   </div></div>
  <p class=sub>Leave the password empty for an open network.</p>
  <p class=msg id=jdMsg aria-live=polite></p>
  <div class=dacts>
   <button class="btn ghost" id=jdCancel1 type=button>Cancel</button>
   <button class=btn id=jdNext type=button>Continue</button>
  </div>
 </div>
 <div id=jdStep2 hidden>
  <div class=dbody id=jdWhat></div>
  <div class=dacts>
   <button class="btn ghost" id=jdBack type=button>Back</button>
   <button class=btn id=jdGo type=button>Switch now</button>
  </div>
 </div>
</div></dialog>
"""

_APP_JS = """
'use strict';
var $=function(id){return document.getElementById(id);};
var DASH='\\u2014', MID='\\u00b7';
var ROBOT=(($('robotname')||{}).textContent||'').trim();
// The reboot/shutdown double-confirm asks for the robot's name. If the service
// could not resolve one, fall back to a word that is still deliberate to type --
// an empty confirmation phrase would make the guard a no-op.
var CONFIRM_WORD=ROBOT.length>=2?ROBOT:'REBOOT';

// The five units the service allowlists. Rendered in this order, with the
// field notes an operator needs on site.
var UNIT_LABEL={
 'emori-hostapp.service':'Host app',
 'emori-hub.service':'Hub',
 'emori-kiosk.service':'Kiosk screen',
 'emori-camera-bridge.service':'Camera bridge',
 'reachy-mini-daemon.service':'Robot daemon',
 'emori-admin.service':'Admin (this page)'};
var UNIT_WHAT={
 'emori-hostapp.service':'runs the session, the voice and the robot motion',
 'emori-hub.service':'runs the session, the voice and the robot motion, in a container',
 'emori-kiosk.service':'the browser on the robot screen',
 'emori-camera-bridge.service':'feeds the camera to the screen and the QR reader',
 'reachy-mini-daemon.service':'motors, Wi-Fi and volume, from the vendor',
 'emori-admin.service':'serves this page'};
var UNIT_NOTE={
 'emori-hostapp.service':'Restarting this restarts the kiosk screen with it (systemd PartOf): the robot screen goes blank for a few seconds.',
 'emori-hub.service':'Restarting this restarts the kiosk screen with it (systemd PartOf). The hub takes up to about a minute to come back, and the screen is blank until it does.',
 'emori-camera-bridge.service':'Restart the kiosk screen straight after, or the camera stays black.',
 'emori-admin.service':'Restart only. This page drops for a few seconds while it comes back.'};
var UNIT_ORDER=['emori-hostapp.service','emori-hub.service','emori-kiosk.service','emori-camera-bridge.service',
 'reachy-mini-daemon.service','emori-admin.service'];
var LANGS={en:'English',ca:'Catal\\u00e0'};
var AP_URL='http://10.42.0.1:8070';

var S={hub:{mode:false,graph:false},overview:null,net:null,neterr:null,audio:null,services:null,ready:null,sys:null,
 scan:null,logs:null,lang:null,pk:null,pp:null,setup:null,st:null,
 langAllowed:null};
// What an installed add-on's script registers: checklist steps and Home rows.
var ADDON={setup:[],link:[]};
var active='home', reachable=true, tickN=0, netWatch=0, readyTimer=null;

// ---- tiny helpers ---------------------------------------------------------
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){
 return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function isNum(v){return typeof v==='number'&&isFinite(v);}
function num(v,d){return isNum(v)?(Math.round(v*Math.pow(10,d||0))/Math.pow(10,d||0)):null;}
function pct(v){return isNum(v)?Math.round(v)+'%':DASH;}
function txt(v){v=(v==null?'':String(v)).trim();return v?v:DASH;}
function sleep(ms){return new Promise(function(r){setTimeout(r,ms);});}
function store(k,v){try{if(v===undefined) return localStorage.getItem('emori_admin_'+k);
 localStorage.setItem('emori_admin_'+k,v);}catch(e){}return null;}
// Timestamps arrive as epoch seconds, epoch ms, systemd microseconds or a
// human string; every one of those shapes has shown up in a systemctl/journal
// answer at some point, so normalise rather than guess.
function toMs(v){
 if(v==null||v==='') return null;
 if(typeof v==='number'){ if(!isFinite(v)||v<=0) return null;
  if(v>1e14) return v/1000; if(v>1e11) return v; return v*1000; }
 var p=Date.parse(String(v)); return isNaN(p)?null:p;
}
function ago(v){
 var ms=toMs(v);
 if(ms==null) return v?String(v):DASH;
 var d=(Date.now()-ms)/1000; if(d<0) d=0;
 if(d<60) return Math.round(d)+'s ago';
 if(d<3600) return Math.round(d/60)+'m ago';
 if(d<86400) return Math.round(d/3600)+'h ago';
 return Math.round(d/86400)+'d ago';
}
function uptime(s){
 if(!isNum(s)||s<0) return DASH;
 s=Math.floor(s);
 var d=Math.floor(s/86400), h=Math.floor((s%86400)/3600), m=Math.floor((s%3600)/60);
 if(d) return d+'d '+h+'h';
 if(h) return h+'h '+m+'m';
 return m+'m';
}
function lvl(v,warnAt,badAt){ // usage percentages: higher is worse
 if(!isNum(v)) return '';
 if(v>=badAt) return 'bad';
 if(v>=warnAt) return 'warn';
 return 'ok';
}
function setMsg(el,text,kind){
 if(!el) return;
 el.textContent=text||'';
 el.className='msg'+(text&&kind?' '+kind:'');
}
var toastT=null;
function toast(text,kind){
 var t=$('toast');
 t.textContent=text; t.className='toast on'+(kind?' '+kind:'');
 if(toastT) clearTimeout(toastT);
 toastT=setTimeout(function(){t.className='toast'+(kind?' '+kind:'');},2600);
}

// ---- API ------------------------------------------------------------------
function ApiError(message,status,data){
 this.name='ApiError'; this.message=message; this.status=status||0; this.data=data||null;
}
ApiError.prototype=Object.create(Error.prototype);
// Error keys the service is contracted to send back, in operator language.
var ERRMSG={
 busy:'A session is running on the robot \\u2014 try again when it is idle.',
 hostapp_down:'The host app is not answering. Restart it under Services.',
 not_configured:'This robot has no admin PIN configured.',
 unknown_unit:'That service is not one this page is allowed to touch.',
 // Both of these mean "one of the two processes is running without the shared
 // secret", which is a restart away and points at a DIFFERENT unit than the
 // slug reads like. Raw, they render as "hostapp admin disabled", which sends
 // an operator hunting for a setting that does not exist.
 hostapp_admin_disabled:'The host app started without the admin token. Restart '+
  'Host app under Services (that also restarts the kiosk).',
 admin_token_unset:'EMORI_ADMIN_TOKEN is missing from this robot\\'s .env. '+
  'Re-run install.sh, then restart Host app and Admin service.',
 running:'A readycheck is already running.',
 no_robot_token:'This robot has no Pokamind token of its own yet.',
 token_invalid:'That is not a valid robot token.',
 token_not_verified:'Pokamind did not accept that token, so nothing was changed.',
 token_save_failed:'The robot could not save the token. Nothing was changed.',
 bad_pin:'Wrong PIN.',
 rate_limited:'Too many wrong PINs. Wait a minute and try again.',
 // People tab (hostapp roster_admin): what the robot refused, and why.
 name_required:'Enter the person\\'s name.',
 name_too_long:'That name is too long (80 characters at most).',
 name_invalid:'That name contains a character that cannot be printed (a line break or a hidden control character).',
 email_required:'Enter the person\\'s work email.',
 email_invalid:'That is not an email address the robot can use (like name@example.org: no spaces, no accents).',
 email_reserved:'An @emori.local address belongs to a robot, never to a person.',
 email_not_in_scope:'Pokamind does not let this robot log in people with that email domain.',
 duplicate_email:'Somebody on the roster already has that email.',
 person_not_found:'That person is no longer on the roster (it was changed meanwhile). The list has been refreshed.',
 roster_full:'The roster is full. Remove people who no longer need a badge first.',
 roster_unreadable:'The roster file on the robot cannot be read (a hand edit broke it?), so nothing was changed. Fix configs/roster.yaml over SSH.',
 roster_write_failed:'The robot could not save the roster. Nothing was changed.',
 qr_unavailable:'This robot cannot draw QR badges yet. Deploy it again (a full deploy, with internet).',
 pathway_invalid:'That pathway name cannot be used (too long, or a line break in it).',
 pathway_not_in_scope:'That pathway is not one of this robot\\'s pathways in Pokamind. Pick one from the list.',
 bad_request:'The robot did not understand that request. Reload this page.',
 bad_lang:'The robot speaks Catalan and English only.',
 unsupported_lang:'This robot\\'s Pokamind record does not offer that language, so every session in it would be refused.',
 token_rejected:'The host app refused this admin service\\'s token. Restart both Host app and Admin service under Services.',
 lang_switch_failed:'The robot could not switch its language. Try again, or restart Host app under Services.',
 // Settings (the hostapp's /api/admin/site and this service's own checks).
 invalid_body:'Nothing to save.',
 key_not_editable:'That setting cannot be changed here.',
 invalid_value:'That value is not accepted.',
 base_not_verified:'Pokamind did not answer at that address with this robot\\'s token, so nothing was changed.',
 save_failed:'The robot could not save that. Nothing was changed.',
 restart_failed:'The host app could not be restarted. Try Services \\u2192 Host app \\u2192 Restart.',
 setup_failed:'The robot could not put the setup checklist together. Try again in a moment.',
 // Credentials.
 pin_invalid:'The new PIN must be exactly six digits.',
 pin_unchanged:'That is already the PIN.',
 pin_save_failed:'The robot could not save the new PIN. The old one still works.',
 credential_unset:'That credential is not set on this robot.',
 bad_ssid:'The network name must be 1 to 32 characters, with no line break.',
 bad_password:'The password must be 8 to 63 plain characters (letters, digits, punctuation).',
 nothing_to_set:'Enter a new name or a new password.',
 // The spoken menu.
 menu_invalid:'The menu has a problem, listed below. Nothing was saved.',
 module_not_in_scope:'An option offers a module this robot may not start (not in its Pokamind record, or not available). Nothing was saved.',
 scope_unavailable:'The robot has no verified Pokamind record yet, so it cannot check the menu\\'s modules. Sync it on the Pokamind tab first.',
 reset_failed:'The saved menu could not be removed. Nothing was changed.',
 speech_unavailable:'The robot cannot speak right now (its voice is not running).',
 menu_unreadable:'The menu file on the robot cannot be read.',
 // Results e-mail (the hostapp answers the test with a code).
 recipient_invalid:'Enter one e-mail address to send the test to.',
 smtp_host_unset:'Set the SMTP server first (and save).',
 smtp_from_unset:'Set a sender address (From), or a user name that is an e-mail address, and save.',
 smtp_auth_failed:'The mail server refused the user name or password.',
 smtp_recipient_refused:'The mail server refused that recipient.',
 smtp_sender_refused:'The mail server refused the sender address.',
 smtp_unreachable:'The robot could not reach the mail server (address, port, or the site firewall).',
 smtp_failed:'The mail server did not accept the message.',
 test_busy:'A test e-mail is already being sent. Wait a moment.',
 test_rate_limited:'Too many test e-mails in a short time. Wait ten minutes.'};
// The detail behind invalid_value / key_not_editable / base_not_verified, in words.
var CODEMSG={
 key_unknown:'This robot does not have that setting.',
 key_wrong_owner:'It is changed elsewhere on this page (Credentials).',
 value_not_https:'It must be an https:// address (http only for this robot itself), with no path.',
 value_not_email:'It must be an e-mail address (name@example.org), or a comma-separated list of them.',
 value_not_port:'It must be a port number from 1 to 65535.',
 value_not_tt_patch:'It must be a JSON object keyed by en or ca, each an object of settings whose values are true/false, numbers or one short word, at most four levels deep.',
 value_not_bool:'It must be on or off.',
 value_breaks_deadline_order:'The session-start deadlines must stay in order: the robot\\'s (s) at least the screen\\'s (ms / 1000).',
 value_out_of_range:'It is out of range (too long, too short, or too large).',
 value_has_newline:'It contains a line break or another hidden control character.',
 value_has_markup:'It contains one of < > " ` (not allowed in a name).',
 value_not_origin:'It must be an address only (https://host), with no path after it.',
 value_not_choice:'Pick one of the listed values.',
 value_not_url:'It must be an http:// or https:// address with a host name.',
 value_not_text:'It must be text.'};
// Setting names as the page shows them.
var KEY_LABEL={
 POKAMIND_BASE:'Pokamind address',
 EMORI_ROBOT_NAME:'Robot name',
 EMORI_COURSE_ID:'Course',
 EMORI_PATHWAYS:'Pathways',
 EMORI_LANG:'Start-up language',
 EMORI_SESSION_START_MS:'Session start deadline, screen (ms)',
 EMORI_SESSION_START_S:'Session start deadline, robot (s)',
 EMORI_CUE_BODY_YAW:'Body turn in expression motion',
 EMORI_CUE_Z_MM:'Head sink in expression motion (mm)',
 EMORI_CUE_AMP:'Expression motion amplitude',
 EMORI_TT_OVERRIDES:'Turn-taking patch (JSON)',
 EMORI_SMTP_HOST:'SMTP server',
 EMORI_SMTP_PORT:'SMTP port',
 EMORI_SMTP_USER:'SMTP user name',
 EMORI_SMTP_FROM:'Sender address (From)',
 EMORI_SMTP_PASS:'SMTP password',
 EMORI_SMTP_TLS_VERIFY:'Check the server certificate',
 EMORI_RESULTS_TO:'Send reports to',
 EMORI_RESULTS_BCC:'Blind copy to',
 EMORI_FIWARE:'Send analytics',
 ORION_URL:'Context broker address',
 NGSILD_TENANT:'Tenant',
 QL_NOTIFY_URL:'Time-series notify address',
 EMORI_RECORD:'Record sessions on the robot',
 EMORI_DASH_RETENTION_DAYS:'Keep records for (days)',
 EMORI_ADMIN_PIN:'Admin PIN',
 EMORI_AP_SSID:'Hotspot name',
 EMORI_AP_PASSWORD:'Hotspot password',
 EMORI_DASH_PASSWORD:'Dashboard password'};
function keyLabel(k){ return KEY_LABEL[k]||String(k||''); }
// What a whoami / token code means, in operator language. Shown after the error above,
// or on its own after Test connection.
var PKCODE={
 ok:'Pokamind knows this robot and the token works.',
 robot_revoked:'This robot\\'s token has been revoked in Pokamind.',
 robot_unknown:'Pokamind has no robot with this id (wrong token, or wrong Pokamind server).',
 whoami_auth_failed:'Pokamind refused the token: it was replaced since, the robot clock is wrong, or the credential Pokamind holds for this robot does not match it: ask Pokamind to check the credential of this robot.',
 whoami_not_supported:'The Pokamind server is too old for robot tokens.',
 whoami_unreachable:'Pokamind did not answer: no connection, or something on the way (a Wi-Fi login page, a proxy, a firewall) answered instead. Check the robot\\'s internet: this says nothing about the token.',
 whoami_timeout:'Pokamind took too long to answer. Try again.',
 whoami_unavailable:'Pokamind is temporarily unavailable. Try again in a minute.',
 whoami_server_error:'Pokamind answered with an error. Try again later.',
 whoami_rate_limited:'Too many checks. Wait a minute.',
 whoami_rejected:'Pokamind rejected the check.',
 invalid_whoami_response:'Pokamind answered in a format this robot does not know (update one of them).',
 whoami_robot_id_mismatch:'Pokamind answered about a different robot. Tell Pokamind.',
 whoami_key_mismatch:'Pokamind holds a newer key for this robot: install the current token.',
 whoami_base_url_invalid:'POKAMIND_BASE on this robot is not a valid https address.',
 whoami_signing_unavailable:'This robot cannot sign with its token (software missing: re-run the installer).',
 robot_not_active:'The robot is not active in Pokamind.',
 sync_throttled:'Synced a moment ago. Wait half a minute before syncing again.',
 token_empty:'The token is empty.',
 token_malformed:'That is not a robot token (it starts with pkr1. and is one line).',
 token_version_unsupported:'That token is a newer version than this robot understands.',
 robot_id_invalid:'The robot id inside the token is not valid.',
 seed_invalid:'The key part of the token is damaged (one character too many or too few?).',
 token_file_unreadable:'The token file on the robot cannot be read.',
 token_file_too_large:'The token file on the robot holds something that is not a token.'};
// Wi-Fi and volume failures: what broke, in a sentence, and the way back in. The
// Network and Audio tabs never show a status code: on a v2 robot :8000 answers 404 for a
// vendor route it does not serve, and a bare "HTTP 404" tells nobody on site how to put
// the robot back on a network. Keys: every word a Wi-Fi/volume call can come back with
// (host_backend.error_slug, netman, mixer; tests/test_admin_page.py holds them together).
var BACKEND_WHY={
 route_missing:'This admin service asked the robot for a Wi-Fi or volume function the robot '+
  'does not have (a v2 robot has no vendor daemon). The admin service is out of date or '+
  'set to the wrong backend.',
 daemon_down:'Nothing answered for Wi-Fi and volume: the robot daemon is not running, or is '+
  'restarting. On a v1 robot, restart Robot daemon under Services.',
 timeout:'The robot took too long to answer.',
 nmcli_missing:'NetworkManager (nmcli) is not installed on this robot, so Wi-Fi cannot be '+
  'read or changed from here.',
 nmcli_failed:'NetworkManager refused the request.',
 nmcli_timeout:'NetworkManager did not answer in time.',
 worker_failed:'The admin service could not start the Wi-Fi change.',
 alsa_missing:'The sound tools (ALSA) are not installed on this robot, so the volume cannot '+
  'be read or set.',
 mixer_failed:'The sound card did not answer. It may still be starting up.',
 test_sound_missing:'The robot has no test sound to play.',
 test_sound_failed:'The test sound could not be played.',
 pw_play_missing:'The robot cannot play sounds from here (pw-play is missing).',
 host_error:'The admin service hit an internal error while talking to Wi-Fi or volume.'};
BACKEND_WHY.http_404=BACKEND_WHY.route_missing;
BACKEND_WHY.unreachable=BACKEND_WHY.daemon_down;
// Answers that are not breakage: they get a sentence, never the recovery steps.
var BACKEND_SAID={
 not_saved:'That network is not saved on the robot any more.',
 http_409:'The robot is busy with another change. Wait a moment and try again.'};
function backendWords(code){
 code=String(code||'');
 if(Object.prototype.hasOwnProperty.call(BACKEND_WHY,code)) return {why:BACKEND_WHY[code],broken:true};
 if(Object.prototype.hasOwnProperty.call(BACKEND_SAID,code)) return {why:BACKEND_SAID[code],broken:false};
 if(/^http_5[0-9][0-9]$/.test(code)) return {why:'The robot could not do that: it reported an internal error.',broken:true};
 if(/^http_[0-9]+$/.test(code)) return {why:'The robot refused that request.',broken:false};
 return null;
}
function backendCode(data,status){
 return (data&&typeof data.error==='string'&&data.error)?data.error:'http_'+(status||0);
}
function backendArea(path){
 path=String(path||'');
 if(path.indexOf('/api/network/')===0) return 'network';
 if(path.indexOf('/api/audio')===0) return 'audio';
 return '';
}
// The exact way back in. {t: what to do, c: the command, verbatim}.
function recoverySteps(area,code){
 var ap=(S.net&&S.net.ap_ssid)?String(S.net.ap_ssid):'', steps=[];
 if(area==='network'){
  steps.push({t:ap?'If your phone can see the robot\\'s own Wi-Fi "'+ap+'", join it and open':
   'If your phone can see the robot\\'s own Wi-Fi (its hotspot), join it and open',c:AP_URL});
  steps.push({t:'Or put the robot on the site Wi-Fi by hand over SSH (Ethernet, or any network '+
   'it is still on):',c:'sudo nmcli device wifi connect "<SSID>" password "<password>"'});
 }
 if(code==='route_missing'||code==='http_404'){
  // The one setting that sends this service to routes a v2 robot does not have.
  steps.push({t:'Then, over SSH, set EMORI_ADMIN_HOST_BACKEND=auto in /opt/emori/.env (it may '+
   'say daemon), and restart this admin service:',c:'sudo systemctl restart emori-admin'});
  return steps;
 }
 steps.push({t:'Then restart this admin service (Services \\u2192 Admin, or over SSH):',
  c:'sudo systemctl restart emori-admin'});
 return steps;
}
var BAN_ID={network:'netFailBan',audio:'audFailBan'};
var BAN_TITLE={network:'Wi-Fi cannot be managed from this page right now',
 audio:'The volume cannot be managed from this page right now'};
// Draws the banner for a broken backend; any other word (or none) clears it.
function renderBackendBan(area,code){
 var el=$(BAN_ID[area]); if(!el) return;
 var w=backendWords(code);
 if(!w||!w.broken){ el.innerHTML=''; return; }
 el.innerHTML='<div class="ban bad"><b>'+esc(BAN_TITLE[area])+'</b>'+esc(w.why)+'<ol>'+
  recoverySteps(area,String(code||'')).map(function(s){
   return '<li>'+esc(s.t)+' <code>'+esc(s.c)+'</code></li>';
  }).join('')+'</ol></div>';
}
function apiMessage(data,status,logId){
 var e=data&&(data.error||data.detail||data.message);
 if(e&&typeof e==='object') e=e.error||e.message||'';
 if(typeof e==='string'&&ERRMSG[e]){
  var code=data&&typeof data.code==='string'?data.code:'';
  var why=code&&(PKCODE[code]||CODEMSG[code]||ERRMSG[code]);
  var who=data&&typeof data.key==='string'&&KEY_LABEL[data.key]?KEY_LABEL[data.key]+': ':'';
  return who+ERRMSG[e]+(why&&why!==ERRMSG[e]?' '+why:'');
 }
 var w=typeof e==='string'?backendWords(e):null;
 if(w) return w.why;
 // Never a bare status code: a 404 here is a route this build does not have.
 if(status===404) return 'The robot does not offer that here (it may be running an older build).';
 if(typeof e==='string'&&e) return e.replace(/_/g,' ');
 if(status===409) return 'The robot is busy with that already.';
 if(status===503) return 'That part of the robot is not configured.';
 // Never "HTTP 500": a sentence, and the id the admin service logged it under.
 logId=String(logId||'');
 return 'The robot could not do that.'+(/^[0-9a-f]{8}$/.test(logId)?
  ' Its log id is '+logId+' (Logs tab, Admin).':'');
}
async function api(path,opts){
 var o=Object.assign({credentials:'same-origin',cache:'no-store'},opts||{});
 if(o.json!==undefined){
  o.method=o.method||'POST';
  o.headers=Object.assign({'Content-Type':'application/json'},o.headers||{});
  o.body=JSON.stringify(o.json); delete o.json;
 }
 var r;
 try{ r=await fetch(path,o); }
 catch(e){ setReachable(false);
  throw new ApiError('No answer from the robot. Check your phone is still on its network.',0); }
 if(r.status===401){ location.href='/'; throw new ApiError('Signed out',401); }
 var data=null;
 if(r.status!==204){ try{ data=await r.json(); }catch(e){ data=null; } }
 setReachable(true);
 if(!r.ok){
  // A broken Wi-Fi/volume backend gets its recovery banner whichever control hit it.
  var area=backendArea(path), w=area?backendWords(backendCode(data,r.status)):null;
  if(w&&w.broken) renderBackendBan(area,backendCode(data,r.status));
  throw new ApiError(apiMessage(data,r.status,r.headers.get('X-EMORI-Log-Id')),r.status,data);
 }
 return data===null?{}:data;
}
// Every mutating control runs through here: disabled + spinner while in flight,
// the failure rendered inline next to the control that caused it (never alert()).
async function run(btn,fn,msgEl){
 if(btn&&btn.disabled) return false;
 if(btn){ btn.disabled=true; btn.classList.add('busy'); }
 if(msgEl) setMsg(msgEl,'','');
 try{ await fn(); return true; }
 catch(e){
  if(e&&e.status===401) return false;
  var m=(e&&e.message)||'Something went wrong.';
  if(msgEl) setMsg(msgEl,m,'bad'); else toast(m,'bad');
  return false;
 }
 finally{ if(btn){ btn.disabled=false; btn.classList.remove('busy'); } }
}

// ---- chrome ---------------------------------------------------------------
function setReachable(v){ if(v===reachable) return; reachable=v; renderHeader(); }
function renderHeader(){
 var chip=$('hchip'), dot=$('hchipDot'), t=$('hchipText');
 var n=(S.overview&&S.overview.network)||null;
 if(!reachable){ chip.className='chip bad'; dot.className='dot bad'; t.textContent='no answer'; return; }
 if(!n){ chip.className='chip mut'; dot.className='dot idle'; t.textContent='checking'; return; }
 var m=String(n.mode||'');
 if(m==='wlan'){
  chip.className=n.internet?'chip ok':'chip warn';
  dot.className=n.internet?'dot ok':'dot warn';
  t.textContent=txt(n.ssid)+(n.internet?'':' '+MID+' no internet');
 }else if(m==='hotspot'){
  chip.className='chip warn'; dot.className='dot warn'; t.textContent='hotspot';
 }else if(m==='busy'){
  chip.className='chip mut'; dot.className='dot idle'; t.textContent='switching';
 }else{
  chip.className='chip bad'; dot.className='dot bad'; t.textContent='no Wi-Fi';
 }
}
function badge(tab,on){
 var b=document.querySelector('.tab[data-tab="'+tab+'"]');
 if(b) b.classList.toggle('alert',!!on);
}
var VIEWS=['home','network','audio','lang','people','pokamind','menu','settings',
 'services','health','logs','system'];
var LOADERS={};
function go(tab){
 if(VIEWS.indexOf(tab)<0) tab='home';
 // A revealed token never outlives the tab it was revealed on.
 if(tab!=='pokamind') hideReveal();
 if(tab!=='settings') hideSecret();
 active=tab;
 VIEWS.forEach(function(v){ $('v-'+v).classList.toggle('on',v===tab); });
 Array.prototype.forEach.call(document.querySelectorAll('.tab'),function(b){
  var on=b.getAttribute('data-tab')===tab;
  b.setAttribute('aria-selected',on?'true':'false');
  if(on) try{ b.scrollIntoView({inline:'center',block:'nearest',behavior:'smooth'}); }catch(e){}
 });
 store('tab',tab);
 try{ if(location.hash!=='#'+tab) history.replaceState(null,'','#'+tab); }catch(e){}
 window.scrollTo(0,0);
 if(LOADERS[tab]) LOADERS[tab]();
}

// ---- overview / home ------------------------------------------------------
function row(k,v,cls){
 return '<div class=row><span class=k>'+esc(k)+'</span><span class="v '+(cls||'')+'">'+v+'</span></div>';
}
function tile(k,v,cls,t){
 return '<div class="tile '+(cls||'')+'"><span class=k>'+esc(k)+'</span><span class=v>'+
  esc(v)+'</span>'+(t?'<span class=t>'+esc(t)+'</span>':'')+'</div>';
}
function okbad(v,good,bad){
 if(v===true) return {t:good||'yes',c:'ok'};
 if(v===false) return {t:bad||'no',c:'bad'};
 return {t:DASH,c:'mut'};
}
function sigbars(v){
 if(!isNum(v)) return '';
 var n=v>=75?4:(v>=50?3:(v>=25?2:1)), s='<span class=sig aria-hidden=true>';
 for(var i=1;i<=4;i++) s+='<i class="'+(i<=n?'on':'')+'"></i>';
 return s+'</span>';
}
function svcKind(s){
 var a=String((s&&s.active)||''), sub=String((s&&s.sub)||'');
 if(a==='active'&&(sub==='running'||sub==='exited'||!sub)) return 'ok';
 if(a==='failed'||sub==='failed') return 'bad';
 if(a==='activating'||a==='deactivating'||a==='reloading') return 'warn';
 if(a==='') return 'idle';
 return 'bad';
}
function svcText(s){
 var a=txt(s&&s.active), sub=(s&&s.sub)?String(s.sub):'';
 return a+(sub&&sub!==a?' ('+sub+')':'');
}
function renderHome(){
 var o=S.overview;
 if(!o) return;
 var n=o.network||{}, sys=o.system||{}, svc=Array.isArray(o.services)?o.services:[];
 $('homeStamp').textContent='Updated '+new Date().toTimeString().slice(0,5);

 var probs=[];
 if(!n.mode||n.mode==='disconnected') probs.push('The robot is not on any Wi-Fi network.');
 else if(n.mode==='hotspot') probs.push('The robot is in hotspot mode, so it has no internet and cannot run sessions.');
 else if(n.internet===false) probs.push('The robot is on Wi-Fi but has no internet.');
 if(o.hostapp&&o.hostapp.ok===false) probs.push('The host app is down \\u2014 the robot cannot run a session.');
 if(o.daemon&&o.daemon.ok===false) probs.push('The robot daemon is down \\u2014 no motion, no volume control.');
 var dead=svc.filter(function(s){return svcKind(s)==='bad';});
 if(dead.length) probs.push(dead.length+' service'+(dead.length>1?'s are':' is')+' not running.');
 $('homeBan').innerHTML=probs.length
  ? '<div class="ban bad"><b>Needs attention</b>'+probs.map(esc).join('<br>')+'</div>' : '';
 badge('network',n.mode!=='wlan'||n.internet===false);
 badge('services',dead.length>0);

 var inet=okbad(n.internet,'reachable','none');
 $('homeNet').innerHTML=
  row('Connection',esc(n.mode==='wlan'?'Wi-Fi':(n.mode?String(n.mode):DASH)),
      n.mode==='wlan'?'ok':(n.mode==='hotspot'?'warn':'bad'))+
  row('Network',esc(txt(n.ssid)))+
  row('IP address','<span class=mono>'+esc(txt(n.ip))+'</span>')+
  row('Signal',sigbars(n.signal)+esc(isNum(n.signal)?pct(n.signal):DASH))+
  row('Internet',esc(inet.t),inet.c);

 $('homeSvc').innerHTML=svc.length
  ? svc.map(function(s){
     var k=svcKind(s);
     return row(UNIT_LABEL[s.unit]||txt(s.unit),
      '<i class="dot '+k+'"></i> '+esc(svcText(s)),k==='ok'?'ok':(k==='bad'?'bad':'mut'));
    }).join('')
  : '<p class=empty>No services reported.</p>';

 var pk=o.pokamind||{};
 var ha=okbad(o.hostapp&&o.hostapp.ok,'up','down');
 var dm=okbad(o.daemon&&o.daemon.ok,'up','down');
 var pa=okbad(pk.api_ok,'reachable','unreachable');
 var tk=tokenState(pk.token);
 badge('pokamind',tk.c==='bad');
 $('homeLink').innerHTML=
  row('Host app',esc(ha.t),ha.c)+row('Robot daemon',esc(dm.t),dm.c)+
  row('Pokamind API',esc(pa.t),pa.c)+
  ADDON.link.map(function(f){ return f(o)||''; }).join('')+
  row('Pokamind token','<i class="dot '+(tk.c==='mut'?'idle':tk.c)+'"></i> '+esc(tk.t),tk.c);

 var au=o.audio||{};
 $('homeSess').innerHTML=
  row('Language',esc(o.lang?(LANGS[o.lang]||o.lang):DASH),o.lang?'':'mut')+
  row('Speaker',esc(isNum(au.speaker)?pct(au.speaker):DASH),isNum(au.speaker)&&au.speaker<=0?'bad':'')+
  row('Microphone',esc(isNum(au.mic)?pct(au.mic):DASH));

 $('homeSys').innerHTML=
  tile('Uptime',uptime(sys.uptime_s))+
  tile('CPU',pct(sys.cpu_pct),lvl(sys.cpu_pct,80,95))+
  tile('Memory',pct(sys.mem_pct),lvl(sys.mem_pct,80,92))+
  tile('Disk',pct(sys.disk_pct),lvl(sys.disk_pct,85,94))+
  tile('Temp',isNum(sys.temp_c)?Math.round(sys.temp_c)+'\\u00b0C':DASH,lvl(sys.temp_c,70,80));

 renderSetup();
 renderHeader();
}
// The checklist is the reason a first-time installer opens this page at all. Every
// step is read live from the robot (GET /api/setup, assembled by the admin service from
// the host app's own routes): nothing is remembered on the phone, so a second phone, a
// reinstall or a robot changed over SSH shows the same truth.
var MENU_SRC={env:'.env override',site:'saved here',prod:'fork menu',auto:'from Pokamind',
 dev:'development menu',builtin:'built-in placeholder'};
var MENU_AUTHORED={pokamind:'from Pokamind',title_draft:'title draft',mixed:'Pokamind + title draft'};
function menuLabel(m){
 if(!m||!m.source) return DASH;
 if(m.source==='auto'&&m.authored&&MENU_AUTHORED[m.authored]) return MENU_AUTHORED[m.authored];
 return MENU_SRC[m.source]||String(m.source);
}
// ok / warn / bad for one language's served menu.
function menuState(m){
 if(!m||!m.source) return 'bad';
 if(m.errors>0||m.source==='dev'||m.source==='builtin') return 'bad';
 if(m.source==='auto'&&(m.authored==='title_draft'||m.authored==='mixed')) return 'warn';
 return 'ok';
}
var REASON_TEXT={scope_changed:'Pokamind changed a setting',
 settings_saved:'settings were saved on this page',
 dashboard_password:'the dashboard password was reset',
 unspecified:'something changed'};
function reasonText(list){
 return (Array.isArray(list)?list:[]).map(function(r){return REASON_TEXT[r]||String(r).replace(/_/g,' ');})
  .join(', ');
}
var AUTO_REASONS={scope_changed:1,dashboard_password:1};
function allAuto(list){
 return Array.isArray(list)&&list.length>0&&list.every(function(r){return AUTO_REASONS[r]===1;});
}
function setupSteps(s){
 var w=s.wifi||{}, tk=s.token, sc=s.scope, mn=s.menus||{}, pp=s.people;
 var rc=s.readycheck||{};
 var langs=(Array.isArray(s.langs)&&s.langs.length)?s.langs:['ca','en'];
 var steps=[];
 steps.push({go:'network',lb:'Put the robot on the site Wi-Fi',
  sub:w.mode==='wlan'?(txt(w.ssid)+(w.internet?'':' '+MID+' no internet')):'internet is needed for every session',
  st:w.mode==='wlan'&&w.internet===true?'done':'todo'});
 var tkOk=!!(tk&&tk.credential==='robot_token'&&tk.verified===true);
 steps.push({go:'pokamind',lb:'Install the robot\\'s Pokamind token',
  sub:!tk?'the host app did not answer':(tk.credential!=='robot_token'?'no token of its own yet':
   (tk.verified===true?'verified with Pokamind':(tk.verified===false?'Pokamind refused it':'installed, not tested yet'))),
  st:tkOk?'done':(tk&&tk.credential==='robot_token'&&tk.verified!==false?'warn':'todo')});
 var scOk=!!(sc&&sc.status==='ok');
 steps.push({go:'pokamind',lb:'Read its Pokamind record',
  sub:!sc?DASH:(sc.auto===false?'switched off on this robot (EMORI_SITE_AUTO=0)':
   (scOk?('synced '+ago(sc.fetched_at)+(sc.stale?' '+MID+' over a week ago':'')):'not synced yet')),
  st:sc&&sc.auto===false?'opt':(scOk&&!sc.stale?'done':(scOk?'warn':'todo'))});
 var mstates=langs.map(function(l){return menuState(mn[l]);});
 steps.push({go:'menu',lb:'Check the spoken menu',
  sub:langs.map(function(l){return (LANGS[l]||l)+': '+menuLabel(mn[l]);}).join(' '+MID+' '),
  st:mstates.indexOf('bad')>=0?'todo':(mstates.indexOf('warn')>=0?'warn':'done')});
 steps.push({go:'people',lb:'Add people and print badges',
  sub:pp?(pp.count+' '+(pp.count===1?'person':'people')+' '+MID+' optional'):'optional',
  st:pp&&pp.count>0?'done':'opt'});
 ADDON.setup.forEach(function(f){ var x=f(s); if(x) steps.push(x); });
 steps.push({go:'health',lb:'Run the readycheck',
  sub:rc.running?'running now':(rc.ran?(rc.fail+' failed '+MID+' '+rc.warn+' warnings'):
   'not run since the admin service started'),
  st:rc.ran&&!rc.running&&rc.fail===0?'done':'todo'});
 return steps;
}
var setupSig='';
function renderSetup(){
 var s=S.setup;
 if(!s) return;
 var steps=setupSteps(s);
 var need=steps.filter(function(x){return x.st!=='opt';});
 var done=need.filter(function(x){return x.st==='done';}).length;
 // The Home poll runs through here: rewriting the list every tick would swallow a tap
 // that lands between mousedown and click, so nothing is touched unless it changed.
 var sig=JSON.stringify(steps);
 if(sig!==setupSig){
  setupSig=sig;
  var chip=$('setupChip');
  chip.textContent=done+' of '+need.length+' done';
  chip.className='chip '+(done===need.length?'ok':'mut');
  $('steps').innerHTML=steps.map(function(x,i){
   var mark=x.st==='done'?'\\u2713':(x.st==='warn'?'!':(x.st==='opt'?'\\u2013':String(i+1)));
   return '<li class="'+(x.st==='done'?'done':(x.st==='warn'?'warn':(x.st==='opt'?'opt':'')))+'">'+
    '<span class=n>'+mark+'</span><span class=lb>'+esc(x.lb)+'<small>'+esc(x.sub)+'</small></span>'+
    '<button class="btn ghost sm" type=button data-go="'+x.go+'">'+(x.st==='done'?'Review':'Go')+'</button></li>';
  }).join('');
 }
 var sc=s.scope;
 $('homeApply').innerHTML=(sc&&sc.restart_pending)?applyBanner(sc.restart_reasons,s.auto_apply):'';
 badge('menu',(s.langs||['ca','en']).some(function(l){return menuState((s.menus||{})[l])==='bad';}));
}
// The "changes waiting" banner (Home and Settings): what is pending, and who applies it.
function applyBanner(reasons,auto){
 var who=allAuto(reasons)&&auto&&auto.enabled
  ? 'The robot applies this by itself after '+Math.round((auto.idle_s||600)/60)+' quiet minutes, or now with Apply.'
  : 'Tap Apply now when nobody is using the robot.';
 return '<div class="ban warn"><b>Changes waiting for a host app restart</b>'+
  esc('Because '+reasonText(reasons)+'. '+who)+
  '<div class=btnrow><button class="btn sm" type=button data-apply=1>Apply now</button></div></div>';
}
async function loadSetup(){
 try{ S.setup=await api('/api/setup'); renderSetup(); }
 catch(e){ if(e.status!==401&&!S.setup) $('steps').innerHTML='<li><span class=lb>'+esc(e.message)+'</span></li>'; }
}
async function applyNow(btn){
 var yes=await confirmDlg({title:'Apply now',confirmText:'Restart the host app',
  body:'<p>The host app restarts with the new settings.</p><ul>'+
   '<li>The robot screen goes blank for a few seconds.</li>'+
   '<li>The robot refuses while a visitor session is running.</li></ul>'});
 if(!yes) return;
 await run(btn,async function(){
  await api('/api/settings/apply',{method:'POST'});
  toast('The host app is restarting with the new settings.');
  S.setup=null; setupSig='';
  await sleep(4000);
  loadOverview(); loadSetup();
  if(active==='settings') loadSettings();
 });
}
async function loadOverview(){
 try{ S.overview=await api('/api/overview'); renderHome(); }
 catch(e){ if(e.status===401) return; renderHeader();
  if(!S.overview) $('homeBan').innerHTML='<div class="ban bad"><b>Cannot read the robot</b>'+
   esc(e.message)+'</div>'; }
}

// ---- network --------------------------------------------------------------
function netModeChip(mode,internet){
 var m=String(mode||'');
 if(m==='wlan') return {t:internet===false?'Wi-Fi, no internet':'On Wi-Fi',c:internet===false?'warn':'ok'};
 if(m==='hotspot') return {t:'Hotspot mode',c:'warn'};
 if(m==='busy') return {t:'Switching',c:'mut'};
 if(m==='disconnected') return {t:'Not connected',c:'bad'};
 return {t:m?m:DASH,c:'mut'};
}
function renderNetwork(){
 var n=S.net||{};
 var c=netModeChip(n.mode,n.internet);
 $('netChip').textContent=c.t; $('netChip').className='chip '+c.c;
 var inet=okbad(n.internet,'reachable','none');
 var ssid=n.ssid!=null?n.ssid:n.connected_network;
 $('netRows').innerHTML=
  row('Network',esc(txt(ssid)))+
  row('IP address','<span class=mono>'+esc(txt(n.ip))+'</span>')+
  row('Signal',sigbars(n.signal)+esc(isNum(n.signal)?pct(n.signal):DASH))+
  row('Internet',esc(inet.t),inet.c)+
  row('Hotspot name',esc(txt(n.ap_ssid)),'mut');
 var saved=Array.isArray(n.known_networks)?n.known_networks:[];
 $('savedList').innerHTML=saved.length
  ? '<div class=list>'+saved.map(function(s){
     var cur=ssid&&String(ssid)===String(s);
     return '<div class=item><i class="dot '+(cur?'ok':'idle')+'"></i>'+
      '<div class=grow><div class=t1>'+esc(s)+'</div>'+
      '<div class=t2>'+(cur?'connected now':'saved')+'</div></div>'+
      '<div class=acts><button class="btn ghost sm" type=button data-forget="'+esc(s)+'">Forget</button></div></div>';
    }).join('')+'</div>'
  : '<p class=empty>No saved networks yet.</p>';
 renderNetError();
}
function renderNetError(){
 var e=S.neterr;
 // `error_text` is the DAEMON's own last Wi-Fi error ("Secrets were required"
 // for a wrong passphrase -- the commonest field failure, and the only thing
 // that explains a join that silently reverted to the hotspot). A bare `error`
 // is this service's failure envelope (`daemon_down`), i.e. "we could not ask",
 // which is not a Wi-Fi error and must never be printed as one: three places on
 // this page point the operator at "the last error above".
 var msg=(e&&typeof e==='object')?(e.ok===false?'':e.error_text):e;
 $('netErrBan').innerHTML=(msg&&String(msg).trim())
  ? '<div class="ban bad"><b>Last Wi-Fi error from the robot</b>'+
    'The robot could not complete its last Wi-Fi change.<span class=mono>'+
    esc(String(msg))+'</span></div>' : '';
}
function renderScan(){
 var list=S.scan;
 if(!list){ return; }
 if(!list.length){
  $('scanList').innerHTML='<p class=empty>No networks found. Move the robot closer to the access point and scan again.</p>';
  return;
 }
 $('scanList').innerHTML='<div class=list>'+list.map(function(s){
  return '<div class=item><div class=grow><div class=t1>'+esc(s)+'</div></div>'+
   '<div class=acts><button class="btn sm" type=button data-join="'+esc(s)+'">Join</button></div></div>';
 }).join('')+'</div>';
}
async function loadNetwork(){
 try{ S.net=await api('/api/network/status'); renderBackendBan('network',''); }
 catch(e){
  if(e.status===401) return;
  // The failure envelope still carries ap_ssid: the banner names the hotspot to join.
  if(e.data&&typeof e.data==='object'&&('ap_ssid' in e.data)){
   S.net=e.data; renderBackendBan('network',backendCode(e.data,e.status));
  }
  setMsg($('scanMsg'),e.message,'bad');
 }
 try{ S.neterr=await api('/api/network/error'); }catch(e){}
 renderNetwork();
}
// The daemon's Wi-Fi mutations are fire-and-forget threads behind a busy lock:
// a 200 from connect means "started", never "connected". This panel is the
// honest version of that -- it polls until the mode leaves `busy` and keeps
// waiting through the window where the phone itself loses the robot.
async function watchNetwork(kind,target){
 var mine=++netWatch, t0=Date.now(), lost=false;
 var p=$('netBusy'); p.hidden=false;
 $('netBusyTitle').textContent=kind==='hotspot'?'Starting the hotspot':'Joining '+target;
 $('netBusyText').textContent=kind==='hotspot'
  ? 'The robot is bringing up its own network. Join '+(target||'the robot hotspot')+
    ' from your phone, then open '+AP_URL+'.'
  : 'The robot is switching networks. This takes up to a minute.';
 for(var i=0;i<40;i++){
  $('netBusyElapsed').textContent=Math.round((Date.now()-t0)/1000)+'s';
  await sleep(3000);
  if(netWatch!==mine){ return; }
  var st=null;
  try{ st=await api('/api/network/status'); }
  catch(e){
   if(e.status===401) return;
   lost=true;
   $('netBusyText').textContent='Waiting for the robot to come back \\u2014 your phone may have '+
    'left its network. Rejoin the network the robot is moving to, or its hotspot at '+AP_URL+'.';
   continue;
  }
  if(lost){ lost=false;
   $('netBusyText').textContent='Back in touch with the robot.'; }
  S.net=st;
  if(st.mode&&st.mode!=='busy'){ break; }
 }
 if(netWatch!==mine) return;
 p.hidden=true;
 try{ S.neterr=await api('/api/network/error'); }catch(e){}
 renderNetwork();
 var mode=(S.net&&S.net.mode)||'';
 var now=(S.net&&(S.net.ssid!=null?S.net.ssid:S.net.connected_network))||'';
 if(kind==='hotspot'){
  setMsg($('hotspotMsg'),mode==='hotspot'
   ? 'Hotspot is up. Join it from your phone and open '+AP_URL+'.'
   : 'The robot did not report hotspot mode. Check the last error above.',
   mode==='hotspot'?'ok':'bad');
 }else if(mode==='wlan'&&(!target||String(now)===String(target))){
  setMsg($('scanMsg'),'Connected to '+txt(now)+'.','ok');
  toast('Connected to '+txt(now));
 }else if(mode==='hotspot'){
  setMsg($('scanMsg'),'Could not join '+target+' \\u2014 the robot brought its hotspot back. '+
   'See the last error above.','bad');
 }else{
  setMsg($('scanMsg'),'The robot is '+(mode?mode:'in an unknown state')+
   ' after the attempt. See the last error above.','bad');
 }
 loadOverview();
}

// ---- audio ----------------------------------------------------------------
function renderAudio(){
 var a=S.audio||{};
 $('spk').disabled=false; $('mic').disabled=false;
 if(isNum(a.speaker)){ $('spk').value=a.speaker; $('spkVal').textContent=Math.round(a.speaker); }
 else { $('spkVal').textContent=DASH; }
 if(isNum(a.mic)){ $('mic').value=a.mic; $('micVal').textContent=Math.round(a.mic); }
 else { $('micVal').textContent=DASH; }
 $('advApplied').textContent=txt(a.applied_profile);
 var ss=isNum(a.mic_session_mult)?a.mic_session_mult:1;
 var ii=isNum(a.mic_idle_mult)?a.mic_idle_mult:1;
 $('msess').value=ss; $('msessVal').textContent=ss.toFixed(2);
 $('midle').value=ii; $('midleVal').textContent=ii.toFixed(2);
 if(isNum(a.speaker)&&a.speaker<=0)
  setMsg($('spkMsg'),'The speaker is at zero \\u2014 nobody will hear the robot.','bad');
 // A level the robot could not read says why, never an empty slider and a dash.
 var code=a.speaker_error||a.mic_error||'';
 renderBackendBan('audio',code);
 [['speaker_error','spkMsg'],['mic_error','micMsg']].forEach(function(f){
  var w=a[f[0]]?backendWords(a[f[0]]):null;
  if(w) setMsg($(f[1]),w.why,'bad');
 });
}
async function loadAudio(){
 try{ S.audio=await api('/api/audio'); renderAudio(); }
 catch(e){ if(e.status!==401) setMsg($('spkMsg'),e.message,'bad'); }
}
// Commit on `change` (finger lifted), never on `input`: the daemon plays a test
// sound on every speaker set, and a dragged slider would fire dozens of them.
function bindSlider(id,valId,fmt,commit,msgEl){
 var el=$(id);
 el.addEventListener('input',function(){ $(valId).textContent=fmt(parseFloat(el.value)); });
 if(commit) el.addEventListener('change',function(){
  var v=parseFloat(el.value);
  el.disabled=true;
  commit(v).then(function(){ setMsg(msgEl,'Saved.','ok'); })
   .catch(function(e){ if(e.status!==401) setMsg(msgEl,e.message,'bad'); })
   .then(function(){ el.disabled=false; });
 });
}

// ---- language -------------------------------------------------------------
function renderLang(){
 var cur=S.lang;
 // The languages this robot's Pokamind record offers (every shipped one without a record):
 // a session in any other would be refused by Pokamind, so the button is not offered.
 var allowed=(Array.isArray(S.langAllowed)&&S.langAllowed.length)?S.langAllowed:null;
 $('langChip').textContent=cur?(LANGS[cur]||cur):'unknown';
 $('langChip').className='chip '+(cur?'ok':'mut');
 $('langEn').setAttribute('aria-pressed',cur==='en'?'true':'false');
 $('langCa').setAttribute('aria-pressed',cur==='ca'?'true':'false');
 $('langEn').disabled=!!(allowed&&allowed.indexOf('en')<0);
 $('langCa').disabled=!!(allowed&&allowed.indexOf('ca')<0);
 $('langScope').textContent=(allowed&&allowed.length<2)?
  'This robot\\'s Pokamind record offers '+allowed.map(function(l){return LANGS[l]||l;}).join(' and ')+
  ' only.':'';
}
async function loadLang(){
 try{
  var j=await api('/api/lang');
  S.lang=(j&&j.lang)||(S.overview&&S.overview.lang)||null;
  S.langAllowed=(j&&Array.isArray(j.allowed_langs))?
   j.allowed_langs.filter(function(l){return !!LANGS[l];}):null;
 }catch(e){
  if(e.status===401) return;
  S.lang=(S.overview&&S.overview.lang)||null;
  setMsg($('langMsg'),e.message,'bad');
 }
 renderLang();
}
async function setLang(target,btn){
 await run(btn,async function(){
  var j=await api('/api/lang',{json:{lang:target}});
  S.lang=(j&&j.lang)||target;
  renderLang();
  setMsg($('langMsg'),'Set to '+(LANGS[target]||target)+
   '. The robot says it out loud and reloads its screen.','ok');
  loadOverview();
 },$('langMsg'));
}

// ---- pokamind -------------------------------------------------------------
// The token itself only ever exists here in two places, both short-lived: the paste
// box on its way to the robot, and the reveal box for 30 seconds. It is never put in
// S, in localStorage or in the URL.
var CRED={robot_token:'This robot\\'s own token',none:'None (demo mode)'};
var TOKEN_RE=/^pkr1\\.[a-z0-9][a-z0-9-]{1,61}[a-z0-9]\\.[A-Za-z0-9_-]{43}$/;
var revealTimer=null;
function tokenState(t){
 if(!t) return {t:DASH,c:'mut'};
 if(t.credential==='robot_token'){
  if(t.mismatch) return {t:'robot id mismatch',c:'bad'};
  if(t.restart_pending) return {t:'restart pending',c:'warn'};
  if(t.verified===false) return {t:'refused',c:'bad'};
  return {t:t.verified?'verified':'installed',c:'ok'};
 }
 return {t:'none',c:'bad'};
}
function pkCodeText(code){ return PKCODE[code]||(code?String(code).replace(/_/g,' '):'Unknown result.'); }
function renderPokamind(){
 var p=S.pk;
 if(!p) return;
 var w=p.last_whoami||null, who=(w&&w.whoami)||null;
 var tk=tokenState({credential:p.credential,mismatch:p.mismatch,restart_pending:p.restart_pending,
  verified:w?w.ok:null});
 $('pkChip').textContent=tk.t; $('pkChip').className='chip '+tk.c;
 badge('pokamind',tk.c==='bad');
 var ban=[];
 if(p.token_error) ban.push(['bad','The installed token is not usable',
  pkCodeText(p.token_error)+' The robot ignores it. Use Replace token.']);
 if(p.mismatch) ban.push(['bad','Robot id mismatch',
  'EMORI_ROBOT_ID on this robot is '+txt(p.env_robot_id)+' but the token is for '+txt(p.robot_id)+
  '. The robot logs in as the token\\'s robot. If that is not this robot, the wrong token is installed.']);
 if(p.restart_pending) ban.push(['warn','A new token is saved but not in use yet',
  'Restart the host app (Services) to use it.']);
 if(p.credential==='none'&&!p.token_error) ban.push(['bad','No Pokamind token',
  'Every session is a demo until this robot has its own token: Replace token below, or Deploy-to-Robot.']);
 if(w&&!w.ok) ban.push(['bad','The last connection test failed',pkCodeText(w.code)]);
 $('pkBan').innerHTML=ban.map(function(b){
  return '<div class="ban '+b[0]+'"><b>'+esc(b[1])+'</b>'+esc(b[2])+'</div>'; }).join('');
 var st=who?who.status:null;
 $('pkRows').innerHTML=
  row('Credential',esc(CRED[p.credential]||txt(p.credential)),p.configured?'ok':'warn')+
  row('Robot id','<span class=mono>'+esc(txt(p.robot_id))+'</span>')+
  row('Organisation',esc(txt(who&&who.org)),who?'':'mut')+
  row('Status',esc(txt(st)),st==='active'?'ok':(st?'bad':'mut'))+
  row('Fingerprint','<span class=mono>'+esc(txt(p.fingerprint))+'</span>')+
  row('Token','<span class=mono>'+esc(txt(p.masked))+'</span>')+
  row('Stored in',esc(txt(p.source)),'mut')+
  row('Last verified',esc(w?(ago(w.checked_at)+' '+MID+' '+(w.ok?'accepted':String(w.code||'failed'))):'never'),
   w?(w.ok?'ok':'bad'):'mut');
 $('pkTest').disabled=!p.configured;
 $('pkReveal').disabled=!p.configured;
 renderScope(who);
}
function renderScope(who){
 var chip=$('pkScopeChip');
 if(!who){
  chip.textContent='not checked'; chip.className='chip mut';
  $('pkScope').innerHTML='';
  $('pkModules').innerHTML='<p class=empty>Tap <b>Test connection</b> to ask Pokamind.</p>';
  return;
 }
 var mods=Array.isArray(who.modules)?who.modules:[];
 var bad=mods.filter(function(m){return !m.available||m.problem;}).length;
 chip.textContent=mods.length+' module'+(mods.length===1?'':'s')+(bad?' '+MID+' '+bad+' not available':'');
 chip.className='chip '+(bad?'warn':(mods.length?'ok':'mut'));
 function list(v){ return Array.isArray(v)&&v.length?v.join(', '):DASH; }
 $('pkScope').innerHTML=
  row('Course',esc(txt(who.course_id)))+
  row('Languages',esc(list(who.languages)))+
  row('Pathways',esc(list(who.pathways)));
 $('pkModules').innerHTML=mods.length?mods.map(function(m){
  var ok=m.available&&!m.problem;
  return '<div class=item><i class="dot '+(ok?'ok':'bad')+'"></i><div class=grow>'+
   '<div class=t1>'+esc(txt(m.title))+'</div>'+
   '<div class="t2 mono">'+esc(m.module_id)+(m.language?' '+MID+' '+esc(m.language):'')+'</div>'+
   (ok?'':'<div class="t2 bad">'+esc(m.problem?String(m.problem).replace(/_/g,' '):'not available')+'</div>')+
   '</div></div>';
 }).join(''):'<p class=empty>No modules in this robot\\'s scope.</p>';
}
async function loadPokamind(){
 try{ S.pk=await api('/api/pokamind'); renderPokamind(); }
 catch(e){ if(e.status!==401) setMsg($('pkMsg'),e.message,'bad'); }
}
async function pkTest(btn){
 await run(btn,async function(){
  var j=await api('/api/pokamind/verify',{method:'POST'});
  if(S.pk) S.pk.last_whoami=j.last_whoami;
  renderPokamind();
  var w=j.last_whoami||{};
  setMsg($('pkMsg'),pkCodeText(w.code),j.ok?'ok':'bad');
  loadOverview();
 },$('pkMsg'));
}
function openReplace(){
 $('tkIn').value=''; $('tkIn').type='password';
 $('tkEye').textContent='Show'; $('tkEye').setAttribute('aria-pressed','false');
 setMsg($('tkMsg'),'','');
 dlgOpen($('tkdlg'));
 setTimeout(function(){ try{ $('tkIn').focus(); }catch(e){} },60);
}
async function replaceStep(){
 var value=$('tkIn').value.trim();
 if(!TOKEN_RE.test(value)){
  setMsg($('tkMsg'),PKCODE.token_malformed,'bad'); return;
 }
 $('tkIn').value='';
 dlgClose($('tkdlg'));
 var rid=value.split('.')[1];
 var vals=await formDlg({title:'Replace the Pokamind token',danger:true,
  confirmText:'Replace and restart',
  body:'<p>The robot first checks the new token with Pokamind (robot <b>'+esc(rid)+
   '</b>). It is saved only if Pokamind accepts it, and the robot then takes its course, '+
   'languages and menus from that robot\\'s Pokamind record.</p><ul>'+
   '<li>The host app then <b>restarts</b>: the robot screen goes blank for a few seconds.</li>'+
   '<li>The robot refuses while a visitor session is running.</li>'+
   '<li>The old token is no longer used here. Revoke it in Pokamind if nothing else needs it.</li></ul>',
  fields:[{id:'pin',label:'Admin PIN',type:'pin'}]});
 if(!vals){ value=''; return; }
 await run($('pkReplace'),async function(){
  var j;
  try{ j=await api('/api/pokamind/token',{json:{token:value,pin:vals.pin}}); }
  finally{ value=''; }
  setMsg($('pkMsg'),'Saved the token for '+txt(j.robot_id)+(j.restarted
   ?'. The host app is restarting with it\\u2026':'. Restart the host app under Services to use it.'),
   j.restarted?'':'bad');
  if(j.restarted&&await waitForHostappToken()){
   setMsg($('pkMsg'),'The robot now uses its new token ('+txt(j.robot_id)+').','ok');
  }
  loadOverview();
 },$('pkMsg'));
}
// After a replace the hostapp restarts; poll its status until the token in force is
// the one on disk. A 502 while it is down is expected, not an error to show.
async function waitForHostappToken(){
 for(var i=0;i<30;i++){
  await sleep(1500);
  try{
   var j=await api('/api/pokamind');
   S.pk=j; renderPokamind();
   if(!j.restart_pending) return true;
  }catch(e){ if(e.status===401) return false; }
 }
 return false;
}
function hideReveal(){
 if(revealTimer){ clearInterval(revealTimer); revealTimer=null; }
 var box=$('pkRevealed'); if(box) box.value='';
 var wrap=$('pkRevealBox'); if(wrap) wrap.hidden=true;
}
function showReveal(token,secs){
 hideReveal();
 var left=Math.max(1,Math.min(120,parseInt(secs,10)||30));
 $('pkRevealed').value=String(token||'');
 $('pkRevealBox').hidden=false;
 $('pkRevealLeft').textContent='(hides in '+left+'s)';
 revealTimer=setInterval(function(){
  left--;
  if(left<=0){ hideReveal(); return; }
  $('pkRevealLeft').textContent='(hides in '+left+'s)';
 },1000);
}
function openReveal(){
 $('pinIn').value=''; setMsg($('pinMsg'),'','');
 dlgOpen($('pindlg'));
 setTimeout(function(){ try{ $('pinIn').focus(); }catch(e){} },60);
}
async function revealFire(btn){
 var pin=$('pinIn').value.replace(/[^0-9]/g,'');
 $('pinIn').value='';
 if(pin.length!==6){ setMsg($('pinMsg'),'The PIN is six digits.','bad'); return; }
 await run(btn,async function(){
  var j;
  try{ j=await api('/api/pokamind/reveal',{json:{pin:pin}}); }
  finally{ pin=''; }
  dlgClose($('pindlg'));
  showReveal(j.token,j.visible_s);
  setMsg($('pkMsg'),'Shown below for '+(j.visible_s||30)+' seconds.','');
 },$('pinMsg'));
}

// ---- applied from Pokamind (Pokamind tab) ---------------------------------
// What this robot's Pokamind record sets, and whether each value is the one in force:
// GET /api/settings (the host app's view of the next start, keys + scope).
var SRC_TEXT={admin:'saved here',env:'.env',pokamind:'Pokamind',hardware:'hardware',
 default:'default'};
var DERIVED=['EMORI_COURSE_ID','EMORI_PATHWAYS','EMORI_ROBOT_NAME','EMORI_LANG'];
function srcChip(source,pending){
 var cls=source?String(source):'none';
 return '<span class="src '+esc(cls)+'">'+esc(source?(SRC_TEXT[source]||source):'not set')+'</span>'+
  (pending?'<span class="src pending">restart pending</span>':'');
}
function keyRow(k){
 var keys=(S.st&&Array.isArray(S.st.keys))?S.st.keys:[];
 for(var i=0;i<keys.length;i++){ if(keys[i]&&keys[i].key===k) return keys[i]; }
 return null;
}
function renderApplied(){
 var st=S.st;
 if(!st){ return; }
 var sc=st.scope||{}, derived=sc.derived||{};
 var chip=$('pkAppChip');
 var status=String(sc.status||'');
 if(sc.auto===false){ chip.textContent='switched off'; chip.className='chip mut'; }
 else if(status==='ok'){ chip.textContent=sc.stale?'over a week old':'synced';
  chip.className='chip '+(sc.stale?'warn':'ok'); }
 else { chip.textContent=status?status.replace(/_/g,' '):'not synced'; chip.className='chip warn'; }
 var rows=DERIVED.map(function(k){
  var r=keyRow(k)||{}, from=derived[k];
  var inForce=r.source==='pokamind';
  var shown=from!==undefined?String(from):DASH;
  var note=inForce?'in use':(from===undefined?'not in the record':
   (r.source?'overridden ('+(SRC_TEXT[r.source]||r.source)+')':'not used'));
  return row(keyLabel(k),esc(shown)+' '+srcChip(inForce?'pokamind':r.source,!!r.running_differs)+
   '<span class=hint>'+esc(note)+'</span>',inForce?'':'mut');
 });
 rows.push(row('Contract',esc(sc.contract!=null?String(sc.contract):DASH),'mut'));
 rows.push(row('Last synced',esc(sc.fetched_at?ago(sc.fetched_at):'never'),sc.fetched_at?'':'mut'));
 rows.push(row('Last sync',esc(sc.last_sync_code?(pkCodeText(sc.last_sync_code)+
  (sc.last_sync_at?' ('+ago(sc.last_sync_at)+')':'')):'not since the host app started'),
  sc.last_sync_code&&sc.last_sync_code!=='ok'?'bad':'mut'));
 if(Array.isArray(sc.ignored_keys)&&sc.ignored_keys.length)
  rows.push(row('Ignored from the record',esc(sc.ignored_keys.join(', ')),'warn'));
 $('pkApplied').innerHTML=rows.join('');
 $('pkSync').disabled=sc.auto===false;
}
async function loadSettings(){
 try{ S.st=await api('/api/settings'); }
 catch(e){
  if(e.status===401) return;
  setMsg($('pkSyncMsg'),e.message,'bad');
  // The credentials are this service's own: they work with the host app down.
  renderSettings();
  $('stBan').innerHTML='<div class="ban bad"><b>Cannot read the settings</b>'+esc(e.message)+'</div>';
  return;
 }
 $('stBan').innerHTML='';
 renderApplied();
 renderSettings();
}
async function pkSync(btn){
 await run(btn,async function(){
  var j=await api('/api/scope/sync',{method:'POST'});
  var parts=[pkCodeText(j.code)];
  if(j.ok){
   var n=Array.isArray(j.changed_keys)?j.changed_keys.length:0;
   parts=['Synced.'];
   parts.push(n?(n+' setting'+(n===1?'':'s')+' changed: '+j.changed_keys.map(keyLabel).join(', ')+'.'):
    'Nothing changed.');
   if(j.menus_changed) parts.push('The spoken menu was rebuilt.');
   if(Array.isArray(j.restart_reasons)&&j.restart_reasons.length)
    parts.push('A host app restart is pending ('+reasonText(j.restart_reasons)+').');
  }
  setMsg($('pkSyncMsg'),parts.join(' '),j.ok?'ok':'bad');
  await loadSettings();
  S.setup=null; loadSetup();
 },$('pkSyncMsg'));
}

// ---- people ---------------------------------------------------------------
// The badge roster. Names and addresses live on the robot: here they are only drawn
// (always through esc) and typed. Nothing goes to localStorage, and a badge link
// carries the person's ref, never the code printed on the badge.
var ppEditRef='';
function ppUrl(path,ref,extra){
 return path+'?ref='+encodeURIComponent(ref)+(extra||'');
}
function ppFind(ref){
 var list=(S.pp&&Array.isArray(S.pp.people))?S.pp.people:[];
 for(var i=0;i<list.length;i++){ if(list[i].ref===ref) return list[i]; }
 return null;
}
function ppDomainList(sc){
 return (sc&&Array.isArray(sc.domains))?sc.domains:[];
}
// The pathway picker: this robot's Pokamind pathways (the roster GET's "pathways").
// A person's current pathway that is no longer on the list is kept as an option, so an
// edit that does not touch it does not drop it.
function fillPathways(sel,paths,current){
 var html='<option value="">No pathway</option>'+paths.map(function(x){
  return '<option value="'+esc(x)+'">'+esc(x)+'</option>'; }).join('');
 if(current&&paths.indexOf(current)<0)
  html+='<option value="'+esc(current)+'">'+esc(current)+' (not in Pokamind any more)</option>';
 sel.innerHTML=html;
 sel.value=current||'';
}
var ppEditPath='';
function ppScopeError(e){
 var d=e&&e.data;
 if(!d||d.error!=='email_not_in_scope') return e;
 var doms=Array.isArray(d.domains)?d.domains:[];
 return new ApiError(ERRMSG.email_not_in_scope+(doms.length?' Use an address at '+doms.join(' or ')+'.':
  ' Pokamind lists no staff email domain for this robot yet: ask Pokamind to add yours.'),
  e.status,d);
}
function renderPeople(){
 var p=S.pp;
 if(!p) return;
 var sc=p.scope||{}, doms=ppDomainList(sc);
 var list=Array.isArray(p.people)?p.people:[];
 var ban=[];
 if(p.qr_available===false) ban.push(['warn','Badges cannot be drawn yet',
  'The QR software is missing on this robot (it was updated with a quick deploy). Deploy it '+
  'again, a full deploy with internet. Adding and removing people already works.']);
 if(p.live===false) ban.push(['bad','The roster file on the robot has a problem',
  'configs/roster.yaml no longer reads (a hand edit?). The robot keeps the roster it had. '+
  'Fix the file over SSH; changes made here are refused until then.']);
 if(sc.checked===false) ban.push(['warn','Email domains not checked',
  'Pokamind could not be asked which email domains this robot may log in ('+pkCodeText(sc.code)+
  '). People are still added: check each address, because one outside this robot\\'s domains '+
  'is refused when the person scans their badge.']);
 else if(sc.checked&&!doms.length) ban.push(['bad','No staff email domain for this robot',
  'Pokamind lists no staff email domain for this robot, so nobody can be added yet. Ask '+
  'Pokamind to add your organisation\\'s domain to this robot.']);
 $('ppBan').innerHTML=ban.map(function(b){
  return '<div class="ban '+b[0]+'"><b>'+esc(b[1])+'</b>'+esc(b[2])+'</div>'; }).join('');
 $('ppDomains').textContent=sc.checked&&doms.length?
  'Allowed email domains: '+doms.join(', ')+'.':'';
 var paths=Array.isArray(p.pathways)?p.pathways:[];
 fillPathways($('ppPath'),paths,$('ppPath').value||'');
 $('ppPathBox').hidden=!paths.length;
 $('ppCourse').textContent=p.course_id?'Badges sign in to the course '+p.course_id+'.':'';
 var needs=list.filter(function(x){return !x.valid||x.in_scope===false;}).length;
 $('ppChip').textContent=list.length+' '+(list.length===1?'person':'people')+
  (needs?' '+MID+' '+needs+' to fix':'');
 $('ppChip').className='chip '+(needs?'warn':(list.length?'ok':'mut'));
 badge('people',needs>0);
 var printable=list.filter(function(x){return x.valid;}).length;
 $('ppPrintAll').hidden=!(p.qr_available&&printable);
 $('ppPrintAll').textContent='Print all badges ('+printable+')';
 $('ppList').innerHTML=list.length?list.map(function(x){
  var warn=!x.valid?'Incomplete entry: its badge cannot work. Remove it.':
   (x.in_scope===false?'Not one of this robot\\'s email domains: the badge will be refused.':'');
  return '<div class=item><div class=grow>'+
   '<div class=t1>'+esc(txt(x.name))+'</div>'+
   '<div class=t2>'+esc(txt(x.email))+(x.pathway?' '+MID+' '+esc(x.pathway):'')+'</div>'+
   (warn?'<div class="t2 bad">'+esc(warn)+'</div>':'')+
   '</div><div class=acts>'+
   (x.valid&&p.qr_available?'<button class="btn sm" type=button data-pbadge="'+esc(x.ref)+'">Badge</button>':'')+
   (x.valid?'<button class="btn ghost sm" type=button data-pedit="'+esc(x.ref)+'">Edit</button>':'')+
   '<button class="btn dghost sm" type=button data-pdel="'+esc(x.ref)+'">Remove</button>'+
   '</div></div>';
 }).join(''):'<p class=empty>Nobody yet. Add the first person above.</p>';
}
async function loadPeople(){
 try{ S.pp=await api('/api/people'); renderPeople(); setMsg($('ppListMsg'),'',''); }
 catch(e){ if(e.status!==401) setMsg($('ppListMsg'),e.message,'bad'); }
}
async function ppAdd(btn){
 var name=$('ppName').value, email=$('ppEmail').value;
 if(!name.trim()){ setMsg($('ppMsg'),ERRMSG.name_required,'bad'); return; }
 if(!email.trim()){ setMsg($('ppMsg'),ERRMSG.email_required,'bad'); return; }
 var body={name:name,email:email};
 if(!$('ppPathBox').hidden) body.pathway=$('ppPath').value;
 await run(btn,async function(){
  var j;
  try{ j=await api('/api/people/add',{json:body}); }
  catch(e){ throw ppScopeError(e); }
  S.pp=j; renderPeople();
  $('ppName').value=''; $('ppEmail').value=''; $('ppPath').value='';
  var who=ppFind(j.ref);
  setMsg($('ppMsg'),'Added '+(who?who.name:name.trim())+'.'+(j.warning==='scope_unchecked'?
   ' Pokamind could not confirm the email domain: check the address.':''),j.warning?'':'ok');
  if(who&&who.valid&&j.qr_available) openBadge(who.ref);
 },$('ppMsg'));
}
function openEdit(ref){
 var x=ppFind(ref); if(!x) return;
 ppEditRef=ref;
 $('ppdName').value=x.name||''; $('ppdEmail').value=x.email||'';
 var paths=(S.pp&&Array.isArray(S.pp.pathways))?S.pp.pathways:[];
 ppEditPath=x.pathway||'';
 fillPathways($('ppdPath'),paths,ppEditPath);
 $('ppdPathBox').hidden=!paths.length&&!ppEditPath;
 setMsg($('ppdMsg'),'','');
 dlgOpen($('ppdlg'));
}
async function ppSave(btn){
 var name=$('ppdName').value, email=$('ppdEmail').value;
 var body={ref:ppEditRef,name:name,email:email};
 // Sent only when changed: left out, the person keeps the pathway they have; "" removes it.
 if(!$('ppdPathBox').hidden&&$('ppdPath').value!==ppEditPath) body.pathway=$('ppdPath').value;
 await run(btn,async function(){
  var j;
  try{ j=await api('/api/people/update',{json:body}); }
  catch(e){ if(e.status===404) loadPeople(); throw ppScopeError(e); }
  S.pp=j; renderPeople();
  dlgClose($('ppdlg'));
  setMsg($('ppListMsg'),'Saved.','ok');
 },$('ppdMsg'));
}
function ppRemove(ref,btn){
 var x=ppFind(ref); if(!x) return;
 confirmDlg({title:'Remove '+(x.name||'this person'),danger:true,confirmText:'Remove',
  body:'<p><b>'+esc(txt(x.name))+'</b> ('+esc(txt(x.email))+') leaves the roster and their badge '+
   'stops working at once.</p><ul><li>Their past sessions stay in Pokamind.</li>'+
   '<li>To give them a badge again, add them again: a new code is printed, the old one stays dead.</li></ul>'
 }).then(function(yes){
  if(!yes) return;
  run(btn,async function(){
   var j;
   try{ j=await api('/api/people/remove',{json:{ref:ref}}); }
   catch(e){ if(e.status===404) loadPeople(); throw e; }
   S.pp=j; renderPeople();
   setMsg($('ppListMsg'),'Removed.','ok');
  },$('ppListMsg'));
 });
}
function openBadge(ref){
 var x=ppFind(ref); if(!x) return;
 $('bgTitle').textContent=x.name||'Badge';
 $('bgImg').alt='QR badge for '+(x.name||'this person');
 $('bgImg').src=ppUrl('/api/people/badge',ref,'&format=svg');
 $('bgPrint').href=ppUrl('/api/people/sheet',ref);
 $('bgSvg').href=ppUrl('/api/people/badge',ref,'&format=svg&download=1');
 $('bgPng').href=ppUrl('/api/people/badge',ref,'&format=png&download=1');
 dlgOpen($('bgdlg'));
}
function closeBadge(){
 dlgClose($('bgdlg'));
 $('bgImg').removeAttribute('src');
}

// ---- spoken menu ------------------------------------------------------------
// GET /api/menu?lang= is the host app's view of one language: which file it serves
// (source, authored), that menu, the record's modules for the language and why each is
// or is not offered. The page edits a DRAFT of the menu and saves it as this robot's own
// (configs/site); Reset removes that file and the Pokamind-built menu is served again.
var MENUMSG={
 menu_not_mapping:'The menu is not a menu (the file is damaged).',
 prompt_missing:'The opening sentence is empty.',
 prompt_invalid:'The opening sentence is longer than 600 characters, or has a < or > in it.',
 options_empty:'The menu offers nothing: add at least one module.',
 too_many_options:'Six options at most: a visitor chooses by saying one to six.',
 option_key_not_number:'An option is not numbered one to six.',
 module_id_invalid:'An option has no valid Pokamind module.',
 name_invalid:'A name is empty, longer than 60 characters, or has a comma, a line break or one of < > " `.',
 keyword_invalid:'A keyword is empty, longer than 60 characters, or has a line break or one of < > " `.',
 keyword_has_comma:'A keyword has a comma in it: one keyword per comma-separated entry.',
 keyword_conflict:'Two options share a keyword (or one keyword is part of another), so the robot could not tell them apart.',
 default_not_an_option:'The default option is not one of the options.',
 menu_unreadable:'The menu file on the robot cannot be read.'};
var MODREASON={
 no_language:'no language in the Pokamind record',
 unavailable:'not available',
 over_menu_limit:'more than six modules: not offered',
 duplicate:'listed twice in the record',
 module_id_invalid:'its id cannot be used',
 not_found:'Pokamind cannot find it',
 not_visible_to_robot:'not visible to this robot',
 language_mismatch:'its language does not match'};
var NUMWORDS={en:['one','two','three','four','five','six'],ca:['u','dos','tres','quatre','cinc','sis']};
var MN={lang:null,data:null,opts:[]};
function mnLang(){
 var allowed=(S.setup&&Array.isArray(S.setup.langs)&&S.setup.langs.length)?S.setup.langs:null;
 if(MN.lang&&(!allowed||allowed.indexOf(MN.lang)>=0)) return MN.lang;
 var cur=S.lang||(S.overview&&S.overview.lang);
 if(cur&&(!allowed||allowed.indexOf(cur)>=0)) return cur;
 return allowed?allowed[0]:'ca';
}
function mnModule(id){
 var mods=(MN.data&&Array.isArray(MN.data.modules))?MN.data.modules:[];
 for(var i=0;i<mods.length;i++){ if(mods[i].module_id===id) return mods[i]; }
 return null;
}
function mnFromData(d){
 var m=(d&&d.menu)||{}, opts=[], o=m.options||{};
 Object.keys(o).sort(function(a,b){return parseInt(a,10)-parseInt(b,10);}).forEach(function(k){
  var x=o[k]||{};
  opts.push({module_id:String(x.module_id||''),name:String(x.name||''),
   keywords:Array.isArray(x.keywords)?x.keywords.map(String):[]});
 });
 MN.opts=opts;
 $('mnPrompt').value=String(m.prompt||'');
}
function mnDraft(){
 mnCollect();
 var options={};
 MN.opts.forEach(function(x,i){
  options[String(i+1)]={module_id:x.module_id,name:x.name.trim(),keywords:x.keywords};
 });
 var menu={prompt:$('mnPrompt').value.trim(),options:options};
 if(MN.opts.length) menu['default']='1';
 return menu;
}
// Read the inputs back into MN.opts (names and keywords the operator typed).
function mnCollect(){
 MN.opts.forEach(function(x,i){
  var n=$('mnName'+i), k=$('mnKw'+i);
  if(n) x.name=n.value;
  if(k) x.keywords=k.value.split(',').map(function(w){return w.trim();})
   .filter(function(w){return w.length>0;});
 });
}
function renderMenu(){
 var d=MN.data;
 Array.prototype.forEach.call($('mnLang').querySelectorAll('button'),function(b){
  var l=b.getAttribute('data-lang');
  var allowed=(S.setup&&Array.isArray(S.setup.langs)&&S.setup.langs.length)?S.setup.langs:null;
  b.setAttribute('aria-pressed',l===MN.lang?'true':'false');
  b.disabled=!!(allowed&&allowed.indexOf(l)<0);
 });
 if(!d) return;
 var lab=menuLabel(d), st=menuState({source:d.source,authored:d.authored,
  errors:Array.isArray(d.errors)?d.errors.length:0});
 $('mnChip').textContent=lab; $('mnChip').className='chip '+(st==='ok'?'ok':(st==='warn'?'warn':'bad'));
 var where={env:'An override in /opt/emori/.env names this file; it wins over everything on this page.',
  site:'This robot\\'s own menu, saved on this page. Reset to Pokamind to serve the built one again.',
  prod:'The menu your fork ships (configs/prod).',
  auto:'Built from this robot\\'s Pokamind record'+(d.authored==='pokamind'?'.':
   ': some names are drafted from module titles. Check they are easy to say, or rename them here.'),
  dev:'A development menu with placeholder modules: Pokamind will refuse them.',
  builtin:'The built-in placeholder menu: Pokamind will refuse it.'};
 $('mnWhere').textContent=where[d.source]||'';
 var ban=[];
 if(d.auto===false) ban.push(['warn','Pokamind menus are switched off on this robot',
  'EMORI_SITE_AUTO=0: the robot serves a saved or shipped menu only.']);
 else if(d.scope_status&&d.scope_status!=='ok') ban.push(['warn','No verified Pokamind record yet',
  'Sync it on the Pokamind tab. Until then the robot cannot build a menu, nor check a saved one.']);
 if(Array.isArray(d.errors)&&d.errors.length) ban.push(['bad','The served menu has problems',
  d.errors.map(function(c){return MENUMSG[c]||String(c).replace(/_/g,' ');}).join(' ')]);
 $('mnBan').innerHTML=ban.map(function(b){
  return '<div class="ban '+b[0]+'"><b>'+esc(b[1])+'</b>'+esc(b[2])+'</div>'; }).join('');
 renderMenuOpts();
 var mods=Array.isArray(d.modules)?d.modules:[];
 var offered=mods.filter(function(m){return m.number;}).length;
 $('mnModChip').textContent=mods.length+' module'+(mods.length===1?'':'s')+' '+MID+' '+offered+' offered';
 $('mnModChip').className='chip '+(mods.length?'ok':'mut');
 $('mnMods').innerHTML=mods.length?mods.map(function(m){
  var ok=m.available&&!m.problem;
  return '<div class=item><i class="dot '+(m.number?'ok':(ok?'idle':'bad'))+'"></i><div class=grow>'+
   '<div class=t1>'+esc(txt(m.title))+'</div>'+
   '<div class="t2 mono">'+esc(m.module_id)+(m.language?' '+MID+' '+esc(m.language):'')+'</div>'+
   (m.number?'<div class=t2>offered as number '+esc(m.number)+'</div>':
    '<div class="t2'+(ok?'':' bad')+'">'+esc(MODREASON[m.reason]||(m.reason?String(m.reason).replace(/_/g,' '):'not offered'))+'</div>')+
   '</div></div>';
 }).join(''):'<p class=empty>'+esc(d.scope_status==='ok'?'The Pokamind record lists no module for this robot.':
  'No Pokamind record yet: sync it on the Pokamind tab.')+'</p>';
 $('mnReset').disabled=!d.saved;
}
function renderMenuOpts(){
 var opts=MN.opts;
 $('mnCount').textContent=opts.length+' of 6';
 $('mnCount').className='chip '+(opts.length?'ok':'warn');
 $('mnOpts').innerHTML=opts.length?opts.map(function(x,i){
  var m=mnModule(x.module_id);
  return '<div class=opt><div class=oh><span class=on>'+(i+1)+'</span>'+
   '<span class=ot>'+esc(m?txt(m.title):x.module_id)+'</span><span class=acts>'+
   '<button class="btn ghost sm" type=button data-mup="'+i+'" aria-label="Move up"'+(i===0?' disabled':'')+'>\\u2191</button>'+
   '<button class="btn ghost sm" type=button data-mdown="'+i+'" aria-label="Move down"'+(i===opts.length-1?' disabled':'')+'>\\u2193</button>'+
   '<button class="btn dghost sm" type=button data-mdel="'+i+'" aria-label="Remove">\\u2715</button></span></div>'+
   '<input type=text id="mnName'+i+'" maxlength=60 autocomplete=off spellcheck=false aria-label="Name of option '+(i+1)+'" value="'+esc(x.name)+'">'+
   '<input type=text id="mnKw'+i+'" maxlength=600 autocomplete=off spellcheck=false aria-label="Keywords of option '+(i+1)+'" placeholder="keywords, separated by commas" value="'+esc(x.keywords.join(', '))+'">'+
   '</div>';
 }).join(''):'<p class=empty>No options yet. Add a module below.</p>';
 var used={}; opts.forEach(function(x){used[x.module_id]=1;});
 var mods=(MN.data&&Array.isArray(MN.data.modules))?MN.data.modules:[];
 var free=mods.filter(function(m){
  return m.available&&!m.problem&&!used[m.module_id]&&(m.eligible||m.reason==='over_menu_limit');
 });
 $('mnAddBox').hidden=!(free.length&&opts.length<6);
 $('mnAdd').innerHTML=free.map(function(m){
  return '<option value="'+esc(m.module_id)+'">'+esc(txt(m.title))+'</option>'; }).join('');
}
async function loadMenu(){
 MN.lang=mnLang();
 renderMenu();
 try{
  MN.data=await api('/api/menu?lang='+encodeURIComponent(MN.lang));
  mnFromData(MN.data);
  setMsg($('mnLoadMsg'),'','');
 }catch(e){ if(e.status!==401) setMsg($('mnLoadMsg'),e.message,'bad'); return; }
 renderMenu();
}
function menuProblems(data){
 var rows=(data&&Array.isArray(data.problems))?data.problems:[];
 if(!rows.length) return '';
 return rows.map(function(p){
  return (p.option?'Option '+p.option+': ':'')+(MENUMSG[p.code]||String(p.code).replace(/_/g,' '));
 }).join(' ');
}
async function mnSave(btn){
 var menu=mnDraft();
 await run(btn,async function(){
  try{ await api('/api/menu/save',{json:{lang:MN.lang,menu:menu}}); }
  catch(e){ var more=menuProblems(e.data); if(more) e.message+=' '+more; throw e; }
  setMsg($('mnMsg'),'Saved. The robot offers this menu from now on, no restart needed.','ok');
  S.setup=null;
  await loadMenu();
 },$('mnMsg'));
}
async function mnReset(btn){
 var yes=await confirmDlg({title:'Reset to Pokamind',confirmText:'Reset',danger:true,
  body:'<p>The menu saved on this page is removed. The robot then offers the menu built from '+
   'its Pokamind record again, at once.</p>'});
 if(!yes) return;
 await run(btn,async function(){
  await api('/api/menu/reset',{json:{lang:MN.lang}});
  setMsg($('mnMsg'),'Back to the menu built from Pokamind.','ok');
  S.setup=null;
  await loadMenu();
 },$('mnMsg'));
}
async function mnPreview(btn){
 var menu=mnDraft();
 await run(btn,async function(){
  try{ await api('/api/menu/preview',{json:{lang:MN.lang,menu:menu}}); }
  catch(e){ var more=menuProblems(e.data); if(more) e.message+=' '+more; throw e; }
  setMsg($('mnMsg'),'The robot is saying the opening sentence now.','ok');
 },$('mnMsg'));
}
// The opening sentence from the option names, in the menu's language (the same
// template the robot drafts with).
function mnPromptFromNames(){
 mnCollect();
 var ca=MN.lang==='ca', words=NUMWORDS[MN.lang]||NUMWORDS.en;
 var parts=MN.opts.map(function(x,i){ return words[i]+(ca?' per ':' for ')+x.name.trim(); });
 if(!parts.length) return;
 var list=parts.length===1?parts[0]:
  parts.slice(0,-1).join(', ')+(ca?', o ':', or ')+parts[parts.length-1];
 $('mnPrompt').value=(ca?'Hola. Per triar la teva pr\\u00e0ctica, digues ':
  'Welcome. To choose your practice, say ')+list+'.';
}

// ---- settings -----------------------------------------------------------------
// GET /api/settings: one row per setting (value, or null for a secret; where it comes
// from; whether the running host app has it yet), the pending restart, the scope. A
// save sends only what changed; an emptied box removes the value saved here, and the
// .env, Pokamind or default value applies again.
var ST_GROUPS={
 mail:['EMORI_SMTP_HOST','EMORI_SMTP_PORT','EMORI_SMTP_USER','EMORI_SMTP_PASS','EMORI_SMTP_FROM',
  'EMORI_SMTP_TLS_VERIFY','EMORI_RESULTS_TO','EMORI_RESULTS_BCC'],
 fiware:['EMORI_FIWARE','ORION_URL','NGSILD_TENANT','QL_NOTIFY_URL'],
 data:['EMORI_RECORD','EMORI_DASH_RETENTION_DAYS'],
 tuning:['EMORI_SESSION_START_MS','EMORI_SESSION_START_S',
  'EMORI_CUE_BODY_YAW','EMORI_CUE_Z_MM','EMORI_CUE_AMP','EMORI_TT_OVERRIDES'],
 server:['POKAMIND_BASE']};
var ST_KIND={EMORI_SMTP_PASS:'secret',EMORI_FIWARE:'bool01',EMORI_RECORD:'bool01',
 EMORI_SMTP_TLS_VERIFY:'bool01',EMORI_CUE_BODY_YAW:'bool01',
 EMORI_SMTP_PORT:'number',EMORI_DASH_RETENTION_DAYS:'number',EMORI_SMTP_FROM:'email',
 EMORI_SESSION_START_MS:'number',EMORI_SESSION_START_S:'decimal',
 EMORI_CUE_Z_MM:'decimal',EMORI_CUE_AMP:'decimal'};
var ST_HINT={
 EMORI_SMTP_PORT:'Usually 587 (STARTTLS); 465 = TLS from the start. Empty = 587.',
 EMORI_SMTP_TLS_VERIFY:'Default On. Off only for a mail server with a self-signed certificate on a network you trust: the password then goes to whoever answers.',
 EMORI_SMTP_PASS:'Write-only: the robot never shows it again. Leave empty to keep it.',
 EMORI_SMTP_FROM:'Needed, unless the user name is itself an e-mail address.',
 EMORI_RESULTS_TO:'Comma-separated e-mail addresses.',
 EMORI_RESULTS_BCC:'Comma-separated e-mail addresses.',
 NGSILD_TENANT:'Stored data is kept per tenant: change it only for a new installation.',
 EMORI_DASH_RETENTION_DAYS:'0 keeps everything.',
 POKAMIND_BASE:'https:// address. Changing it needs the PIN, and Pokamind must answer there.',
 EMORI_SESSION_START_MS:'0 = off, else 5000 to 600000. Empty = the value this robot\\'s hardware supplies.',
 EMORI_SESSION_START_S:'0 = off, else 5 to 600, and not below the screen deadline.',
 EMORI_CUE_Z_MM:'0 to 20.',
 EMORI_CUE_AMP:'0.5 to 2.0.',
 EMORI_TT_OVERRIDES:'Applied to the session page at the next host app restart.'};
// The keys whose change needs the PIN typed again (the admin service checks it too).
var ST_PIN={POKAMIND_BASE:1,EMORI_SMTP_HOST:1,EMORI_SMTP_PORT:1,
 EMORI_SMTP_USER:1,EMORI_SMTP_TLS_VERIFY:1};
var STCLEAR={};
function stField(k){
 var r=keyRow(k)||{}, kind=ST_KIND[k]||'text', id='st_'+k, input;
 if(kind==='bool01'){
  input='<select id="'+id+'"><option value="">Default</option><option value="1">On</option>'+
   '<option value="0">Off</option></select>';
 }else if(kind==='secret'){
  input='<div class=pwrow><input type=password id="'+id+'" maxlength=512 autocomplete=new-password '+
   'placeholder="'+(r.set?'set \\u2014 type to replace':'not set')+'">'+
   '<button class="btn ghost sm" type=button data-stclear="'+esc(k)+'"'+(r.source==='admin'?'':' disabled')+
   '>Clear</button></div>';
 }else{
  input='<input type=text id="'+id+'" maxlength=2000 autocomplete=off autocapitalize=none autocorrect=off '+
   'spellcheck=false inputmode="'+(kind==='number'?'numeric':(kind==='decimal'?'decimal':
   (kind==='email'?'email':'text')))+'">';
 }
 return '<div class=field><label for="'+id+'">'+esc(keyLabel(k))+' '+srcChip(r.source,!!r.running_differs)+
  '</label>'+input+(ST_HINT[k]?'<p class=hint>'+esc(ST_HINT[k])+'</p>':'')+
  (r.problem?'<p class="hint bad">'+esc('The value saved here is not used. '+(CODEMSG[r.problem]||''))+'</p>':'')+
  '</div>';
}
var AUTO_DECISION={not_run:'not checked yet',disabled:'switched off',
 hostapp_unreachable:'the host app is not answering',nothing_pending:'nothing is waiting',
 manual_reasons:'waiting for Apply now (a person saved a change)',busy:'waiting: the robot is in use',
 waiting_idle:'waiting for the robot to stay idle',
 rate_limited:'waiting: one automatic restart per 30 minutes at most',
 applied:'restarted the host app',restart_failed:'the restart failed'};
function renderAuto(){
 var a=(S.st&&S.st.auto_apply)||null, chip=$('stAutoChip');
 if(!a){ chip.textContent=DASH; chip.className='chip mut'; $('stAuto').innerHTML=''; return; }
 chip.textContent=a.enabled?'automatic':'Apply only';
 chip.className='chip '+(a.enabled?'ok':'mut');
 $('stAuto').innerHTML=
  row('Automatic restarts',esc(a.enabled?('after '+Math.round((a.idle_s||0)/60)+' idle minutes, for changes '+
   'from Pokamind and the dashboard password'):'off (EMORI_SITE_AUTO_APPLY_IDLE_S=0)'),a.enabled?'':'mut')+
  row('Now',esc(AUTO_DECISION[a.last_decision]||txt(a.last_decision)),'mut')+
  row('Last automatic restart',esc(isNum(a.applied_ago_s)?ago(Date.now()/1000-a.applied_ago_s):
   'none since the admin service started'),'mut');
}
var CRED_ITEMS=[
 {k:'EMORI_ADMIN_PIN',t:'Admin PIN',what:'Signs in to this page.',acts:[['pinchange','Change']]},
 {k:'EMORI_AP_PASSWORD',t:'Recovery hotspot',what:'The robot\\'s own Wi-Fi, for when no other network works.',
  acts:[['apreveal','Reveal'],['apchange','Change']]},
 {k:'EMORI_DASH_PASSWORD',t:'Dashboard password',what:'The management dashboard on the robot (:8090/dashboard).',
  acts:[['dashreveal','Reveal'],['dashreset','Reset']]}];
function renderCreds(){
 $('stCreds').innerHTML=CRED_ITEMS.map(function(it){
  var r=keyRow(it.k);
  return '<div class=item><div class=grow><div class=t1>'+esc(it.t)+' '+
   (r?srcChip(r.source,!!r.running_differs):'')+'</div><div class=t2>'+esc(it.what)+'</div></div>'+
   '<div class=acts>'+it.acts.map(function(a){
    return '<button class="btn ghost sm" type=button data-cred="'+a[0]+'">'+esc(a[1])+'</button>';
   }).join('')+'</div></div>';
 }).join('');
}
function renderSettings(){
 renderCreds();
 renderAuto();
 var st=S.st;
 if(!st) return;
 $('stApply').innerHTML=st.restart_pending?applyBanner(st.restart_reasons,st.auto_apply):'';
 var inv=(st.invalid_running&&typeof st.invalid_running==='object')?Object.keys(st.invalid_running):[];
 $('stBan').innerHTML=inv.length?'<div class="ban bad"><b>Saved values the robot refused at its start</b>'+
  esc(inv.map(keyLabel).join(', ')+'. They are not in use: correct or clear them.')+'</div>':'';
 badge('settings',!!st.restart_pending||inv.length>0);
 Object.keys(ST_GROUPS).forEach(function(g){
  $('stGroup-'+g).innerHTML=ST_GROUPS[g].map(stField).join('');
  ST_GROUPS[g].forEach(function(k){
   var r=keyRow(k)||{}, el=$('st_'+k);
   if(el&&(ST_KIND[k]||'text')!=='secret') el.value=r.value==null?'':String(r.value);
  });
 });
 STCLEAR={};
}
async function stSave(group,btn){
 var body={}, msg=$('stMsg-'+group);
 ST_GROUPS[group].forEach(function(k){
  var r=keyRow(k)||{}, el=$('st_'+k);
  if(!el) return;
  if((ST_KIND[k]||'text')==='secret'){
   if(STCLEAR[k]) body[k]=null; else if(el.value) body[k]=el.value;
   return;
  }
  var v=el.value.trim(), cur=r.value==null?'':String(r.value);
  if(v===cur) return;
  body[k]=v?v:null;
 });
 var keys=Object.keys(body);
 if(!keys.length){ setMsg(msg,'Nothing changed.',''); return; }
 if(keys.some(function(k){return ST_PIN[k]===1;})){
  var vals=await formDlg({title:'Change the Pokamind address',confirmText:'Save',
   body:'<p>A new Pokamind address is saved only if Pokamind answers there, signed with '+
    'this robot\\'s own token. Applied at the next host app restart.</p>',
   fields:[{id:'pin',label:'Admin PIN',type:'pin'}]});
  if(!vals) return;
  body.pin=vals.pin;
 }
 await run(btn,async function(){
  var j=await api('/api/settings',{json:body});
  var saved=Array.isArray(j.saved)?j.saved:keys;
  setMsg(msg,'Saved: '+saved.map(keyLabel).join(', ')+'. Applied at the next host app restart.','ok');
  await loadSettings();
 },msg);
}
async function stTest(btn){
 var to=$('stTestTo').value.trim();
 if(!to){ setMsg($('stTestMsg'),ERRMSG.recipient_invalid,'bad'); return; }
 await run(btn,async function(){
  var j=await api('/api/results-email/test',{json:{to:to}});
  setMsg($('stTestMsg'),j.sent?'Sent. Check that inbox, and its spam folder.':
   (ERRMSG[j.code]||'The test e-mail was not sent.'),j.sent?'ok':'bad');
 },$('stTestMsg'));
}
// A revealed credential is on screen for its visible_s, then wiped: never in S,
// localStorage or the URL, and gone when the tab changes or the phone locks.
var secretTimer=null;
function hideSecret(){
 if(secretTimer){ clearInterval(secretTimer); secretTimer=null; }
 var box=$('stSecret'); if(!box) return;
 $('stSecretRows').innerHTML=''; box.hidden=true;
}
function showSecret(title,rows,secs){
 hideSecret();
 var left=Math.max(1,Math.min(120,parseInt(secs,10)||30));
 $('stSecretTitle').textContent=title;
 $('stSecretRows').innerHTML=rows.map(function(r){
  return row(r[0],'<span class=mono>'+esc(txt(r[1]))+'</span>'); }).join('');
 $('stSecret').hidden=false;
 $('stSecretLeft').textContent='Hides in '+left+'s.';
 secretTimer=setInterval(function(){
  left--;
  if(left<=0){ hideSecret(); return; }
  $('stSecretLeft').textContent='Hides in '+left+'s.';
 },1000);
}
async function credAction(kind,btn){
 var msg=$('stCredMsg'), vals;
 if(kind==='pinchange'){
  vals=await formDlg({title:'Change the admin PIN',confirmText:'Change PIN',
   body:'<p>Every other phone signs in again; this one stays signed in.</p>',
   fields:[{id:'current',label:'Current PIN',type:'pin'},{id:'next',label:'New PIN',type:'pin'},
    {id:'again',label:'New PIN again',type:'pin'}]});
  if(!vals) return;
  if(vals.next!==vals.again){ setMsg(msg,'The two new PINs differ. Nothing was changed.','bad'); return; }
  await run(btn,async function(){
   await api('/api/pin/change',{json:{current:vals.current,'new':vals.next}});
   setMsg(msg,'PIN changed. The admin service restarts\\u2026','ok');
   await waitForAdmin(msg);
  },msg);
  return;
 }
 if(kind==='apchange'){
  vals=await formDlg({title:'Change the recovery hotspot',confirmText:'Save',
   body:'<p>Leave a box empty to keep it. The admin service restarts and renames the hotspot; '+
    'if your phone is on that hotspot right now, the change waits for the robot\\'s next start on '+
    'the site Wi-Fi, so you are not cut off.</p>',
   fields:[{id:'ssid',label:'New name',type:'text',max:32},
    {id:'password',label:'New password (8 to 63 characters)',type:'password',max:63},
    {id:'pin',label:'Admin PIN',type:'pin'}]});
  if(!vals) return;
  await run(btn,async function(){
   await api('/api/credentials/ap',{json:{pin:vals.pin,ssid:vals.ssid||null,password:vals.password||null}});
   setMsg(msg,'Saved. The admin service restarts\\u2026 Write the new hotspot down.','ok');
   await waitForAdmin(msg);
  },msg);
  return;
 }
 if(kind==='dashreset'){
  vals=await formDlg({title:'Reset the dashboard password',confirmText:'Reset',danger:true,
   body:'<p>A new random password replaces the old one, and is shown once. The host app takes it '+
    'at its next restart (by itself, once the robot has been idle), which signs every dashboard '+
    'session out.</p>',
   fields:[{id:'pin',label:'Admin PIN',type:'pin'}]});
  if(!vals) return;
  await run(btn,async function(){
   var j=await api('/api/credentials/dashboard/reset',{json:{pin:vals.pin}});
   showSecret('New dashboard password',[['Password',j.password]],j.visible_s);
   setMsg(msg,'Reset. Write the new password down now.','ok');
   loadSettings();
  },msg);
  return;
 }
 var which=kind==='apreveal'?'ap':'dashboard';
 vals=await formDlg({title:which==='ap'?'Reveal the hotspot':'Reveal the dashboard password',
  confirmText:'Reveal',danger:true,
  body:'<p>Shown for 30 seconds. Anyone who sees it can use it.</p>',
  fields:[{id:'pin',label:'Admin PIN',type:'pin'}]});
 if(!vals) return;
 await run(btn,async function(){
  var j=await api('/api/credentials/reveal',{json:{which:which,pin:vals.pin}});
  showSecret(which==='ap'?'Recovery hotspot':'Dashboard password',
   which==='ap'?[['Name',j.ssid],['Password',j.password]]:[['Password',j.password]],j.visible_s);
  setMsg(msg,'','');
 },msg);
}

// ---- the form dialog (PIN re-entry, attestations, new values) ----------------
var fmResolve=null, fmSpec=[];
function formDlg(o){
 return new Promise(function(res){
  if(fmResolve){ var prev=fmResolve; fmResolve=null; prev(null); }
  fmResolve=res; fmSpec=o.fields||[];
  $('fmTitle').textContent=o.title||'Confirm';
  $('fmBody').innerHTML=o.body||'';
  $('fmGo').textContent=o.confirmText||'Confirm';
  $('fmGo').className='btn'+(o.danger?' danger':'');
  setMsg($('fmMsg'),'','');
  $('fmFields').innerHTML=fmSpec.map(function(f){
   var id='fm_'+f.id;
   if(f.type==='check') return '<label class=chk><input type=checkbox id="'+id+'"><span>'+esc(f.label)+'</span></label>';
   var attrs=f.type==='pin'?'type=password inputmode=numeric pattern="[0-9]*" maxlength=6':
    (f.type==='password'?'type=password maxlength='+(f.max||128):'type=text maxlength='+(f.max||256));
   return '<div class=field><label for="'+id+'">'+esc(f.label)+'</label><input '+attrs+' id="'+id+
    '" autocomplete=off autocapitalize=none autocorrect=off spellcheck=false></div>';
  }).join('');
  dlgOpen($('fmdlg'));
  var first=fmSpec.filter(function(f){return f.type!=='check';})[0];
  if(first) setTimeout(function(){ try{ $('fm_'+first.id).focus(); }catch(e){} },60);
 });
}
function fmFinish(ok){
 var r=fmResolve; if(!r) return;
 var vals=null;
 if(ok){
  vals={};
  for(var i=0;i<fmSpec.length;i++){
   var f=fmSpec[i], el=$('fm_'+f.id);
   if(f.type==='check'){
    if(!el.checked){ setMsg($('fmMsg'),'Tick the box to confirm: it is your statement.','bad'); return; }
    vals[f.id]=true;
   }else{
    vals[f.id]=el.value;
    if(f.type==='pin'&&!/^[0-9]{6}$/.test(el.value)){ setMsg($('fmMsg'),'The PIN is six digits.','bad'); return; }
   }
  }
 }
 fmResolve=null;
 Array.prototype.forEach.call($('fmFields').querySelectorAll('input'),function(x){ x.value=''; x.checked=false; });
 $('fmFields').innerHTML='';
 dlgClose($('fmdlg'));
 r(vals);
}

// ---- services -------------------------------------------------------------
function renderServices(){
 var list=S.services;
 if(!Array.isArray(list)){ return; }
 var known={}, out=[];
 list.forEach(function(s){ if(s&&s.unit) known[s.unit]=s; });
 var order=UNIT_ORDER.filter(function(u){return known[u];})
  .concat(list.filter(function(s){return s&&s.unit&&UNIT_ORDER.indexOf(s.unit)<0;})
   .map(function(s){return s.unit;}));
 order.forEach(function(u){
  var s=known[u]; if(!s) return;
  var k=svcKind(s), self=u==='emori-admin.service';
  out.push('<div class=item><i class="dot '+k+'"></i>'+
   '<div class=grow><div class=t1>'+esc(UNIT_LABEL[u]||u)+'</div>'+
   '<div class="t2'+(k==='bad'?' bad':'')+'">'+esc(svcText(s))+
   (s.since?' '+MID+' '+esc(ago(s.since)):'')+'</div>'+
   '<div class=t2>'+esc(UNIT_WHAT[u]||u)+'</div></div>'+
   '<div class=acts>'+
   (!self&&k!=='ok'?'<button class="btn sm" type=button data-svc="start" data-unit="'+esc(u)+'">Start</button>':'')+
   '<button class="btn ghost sm" type=button data-svc="restart" data-unit="'+esc(u)+'">Restart</button>'+
   (!self&&k==='ok'?'<button class="btn ghost sm" type=button data-svc="stop" data-unit="'+esc(u)+'">Stop</button>':'')+
   '</div>'+
   (UNIT_NOTE[u]?'<p class=note style="flex:1 1 100%">'+esc(UNIT_NOTE[u])+'</p>':'')+
   '</div>');
 });
 $('svcList').innerHTML=out.length?out.join(''):'<p class=empty>No services reported.</p>';
 renderHubBack(known['emori-hub.service']);
 badge('services',list.some(function(s){return svcKind(s)==='bad';}));
}
// The way out of a hub that does not come up, for a site with no ssh: offered while the flag is
// up and the hub unit is not running. The way back lowers the flag first, so a run is followed
// with the flag down as well, and one that stopped short is offered again.
function renderHubBack(hub){
 var sw=S.hub.sw||{};
 var stuck=!S.hub.mode&&!!sw.stuck;
 var failing=S.hub.mode&&!!hub&&svcKind(hub)!=='ok';
 var can=!sw.running&&!sw.to_hub&&(stuck||failing);
 var show=!!sw.running||can;
 $('hubBack').hidden=!show;
 if(!show){ setMsg($('hubBackMsg'),''); return; }
 $('hubBackGo').hidden=!can;
 var motor=' The graph also moves back to the motor container, so the robot parks, wakes again and is unavailable for up to about five minutes.';
 $('hubBackNote').textContent=!can?''
  :stuck
  ?'The last switch back did not finish. This tries again and puts the host app back. If the graph is still in the hub it moves back to the motor container too, so the robot parks, wakes again and is unavailable for up to about five minutes.'
  :S.hub.graph
  ?'The hub is not running. This puts the host app back.'+motor
  :'The hub is not running. This puts the host app back and leaves the motors alone. It takes under a minute.';
 if(sw.running) setMsg($('hubBackMsg'),'Switching back to the host app. This list updates when it is done.','ok');
 else if(sw.exit&&sw.exit!=='0') setMsg($('hubBackMsg'),'The last switch did not finish ('+sw.reason+').','bad');
}
async function hubBack(btn){
 var yes=await confirmDlg({title:'Back to the host app',body:'<p>'+esc($('hubBackNote').textContent)+'</p>',
  confirmText:'Go back',danger:true});
 if(!yes) return;
 await run(btn,async function(){
  await api('/api/hub/rollback',{json:{}});
  setMsg($('hubBackMsg'),'Switching back to the host app. This list updates when it is done.','ok');
  await sleep(3000);
  await loadServices();
 },$('hubBackMsg'));
}
async function loadServices(){
 try{
  var j=await api('/api/services');
  S.hub={mode:!!(j&&j.hub_mode),graph:!!(j&&j.hub_graph),sw:j&&j.hub_switch};
  S.services=Array.isArray(j)?j:(Array.isArray(j.services)?j.services:[]);
  renderServices();
 }catch(e){ if(e.status!==401) setMsg($('svcMsg'),e.message,'bad'); }
}
async function svcAction(unit,action,btn){
 var label=UNIT_LABEL[unit]||unit, self=unit==='emori-admin.service';
 var body='<p>'+esc(action==='restart'?'Restart':(action==='start'?'Start':'Stop'))+' <b>'+
  esc(label)+'</b>?</p>';
 if(UNIT_NOTE[unit]) body+='<p style="margin-top:8px">'+esc(UNIT_NOTE[unit])+'</p>';
 if(action==='stop') body+='<p style="margin-top:8px">The robot cannot run sessions while this is stopped.</p>';
 if(self) body+='<p style="margin-top:8px"><b>You will briefly lose this page.</b> It comes back '+
  'on its own in a few seconds; nothing else on the robot is touched.</p>';
 var yes=await confirmDlg({title:label,body:body,
  confirmText:(action==='restart'?'Restart':(action==='start'?'Start':'Stop')),
  danger:action==='stop'||self});
 if(!yes) return;
 await run(btn,async function(){
  await api('/api/services/action',{json:{unit:unit,action:action}});
  setMsg($('svcMsg'),label+': '+action+' sent.','ok');
  if(self){ await waitForAdmin(); }
  else { await sleep(1200); }
  await loadServices();
  loadOverview();
 },$('svcMsg'));
}
// Restarting emori-admin kills the process serving this page. /healthz needs no
// cookie, so it is the one probe that still answers while the session cookie is
// in flight; poll it until the service is back rather than showing a dead page.
async function waitForAdmin(el){
 el=el||$('svcMsg');
 setMsg(el,'Admin service restarting \\u2014 waiting for it to come back\\u2026','');
 // It answers until the restart lands (about a second later): wait for that first.
 await sleep(2500);
 for(var i=0;i<20;i++){
  await sleep(1000);
  try{
   var r=await fetch('/healthz',{cache:'no-store'});
   if(r.ok){ setMsg(el,'Admin service is back.','ok'); setReachable(true); return; }
  }catch(e){}
 }
 setMsg(el,'The admin service has not answered yet. Reload this page in a moment.','bad');
}

// ---- health / readycheck --------------------------------------------------
function gatesOf(res){
 if(!res) return null;
 if(Array.isArray(res)) return res;
 if(Array.isArray(res.gates)) return res.gates;
 if(Array.isArray(res.results)) return res.results;
 return null;
}
var GRANK={fail:0,warn:1,pass:2,skip:3};
function renderReady(){
 var r=S.ready||{}, chip=$('readyChip'), out=$('readyGates'), tiles=$('readyTiles');
 $('readyRun').textContent=r.running?'Running\\u2026':'Run readycheck';
 $('readyRun').disabled=!!r.running;
 if(r.running){
  chip.textContent='running'; chip.className='chip mut';
  setMsg($('readyMsg'),'Started '+ago(r.started_at)+'. This can take up to two minutes.','');
 }else{
  chip.textContent=r.result?'finished':'never run';
  chip.className='chip '+(r.result?'ok':'mut');
 }
 if(r.error){ setMsg($('readyMsg'),String(r.error),'bad'); }
 var gates=gatesOf(r.result);
 if(!gates){
  tiles.hidden=true;
  out.innerHTML=r.running
   ? ''
   : (r.result
      ? '<div class=ban><b>Readycheck answered in an unexpected shape</b><span class=mono>'+
        esc(JSON.stringify(r.result).slice(0,600))+'</span></div>'
      : '<p class=empty>No result yet. Run it once before you leave the site.</p>');
  return;
 }
 var c={pass:0,fail:0,warn:0,skip:0};
 gates.forEach(function(g){ var s=String(g&&g.status||'').toLowerCase();
  if(c[s]!==undefined) c[s]++; });
 tiles.hidden=false;
 tiles.innerHTML=tile('Pass',String(c.pass),c.pass?'ok':'')+
  tile('Warn',String(c.warn),c.warn?'warn':'')+
  tile('Fail',String(c.fail),c.fail?'bad':'')+
  tile('Skip',String(c.skip),'');
 if(!r.running) setMsg($('readyMsg'),
  c.fail?(c.fail+' gate'+(c.fail>1?'s':'')+' failed \\u2014 fix these before the robot is left running.')
        :'Every gate the robot could test came back clean.',c.fail?'bad':'ok');
 badge('health',c.fail>0);
 // Failures first: on a phone the operator must not scroll to find them.
 var sorted=gates.slice().sort(function(a,b){
  var ra=GRANK[String(a.status||'').toLowerCase()], rb=GRANK[String(b.status||'').toLowerCase()];
  return (ra===undefined?4:ra)-(rb===undefined?4:rb);
 });
 out.innerHTML=sorted.map(function(g){
  var st=String(g.status||'').toLowerCase();
  var cls=(GRANK[st]!==undefined?st:'skip');
  return '<div class="gate '+cls+'"><div class=gh><span class=gs>'+esc(st.toUpperCase()||'?')+
   '</span><span class=gn>'+esc(txt(g.name))+'</span></div>'+
   '<div class=gt><span class=gc>'+esc(txt(g.code))+'</span>'+
   (g.hint?'<br>'+esc(g.hint):'')+'</div></div>';
 }).join('');
}
async function loadReady(){
 try{ S.ready=await api('/api/readycheck/status'); renderReady(); }
 catch(e){ if(e.status!==401) setMsg($('readyMsg'),e.message,'bad'); return; }
 if(readyTimer) clearTimeout(readyTimer);
 if(S.ready&&S.ready.running) readyTimer=setTimeout(loadReady,2500);
}

// ---- logs -----------------------------------------------------------------
function logClass(line){
 var l=String(line);
 if(/error|fail|traceback|critical|abort/i.test(l)) return 'e';
 if(/warn/i.test(l)) return 'w';
 return '';
}
function renderLogs(){
 var lines=S.logs;
 var box=$('logOut');
 if(!Array.isArray(lines)){ return; }
 if(!lines.length){ box.innerHTML='<p class=empty>Nothing in the journal for that service.</p>'; return; }
 box.innerHTML=lines.map(function(l){
  var c=logClass(l);
  return '<div'+(c?' class='+c:'')+'>'+esc(l)+'</div>';
 }).join('');
 box.scrollTop=box.scrollHeight;
}
function logUnits(){
 var seen={}, out=[];
 UNIT_ORDER.forEach(function(u){ seen[u]=1; out.push(u); });
 var svc=S.services||(S.overview&&S.overview.services)||[];
 if(Array.isArray(svc)) svc.forEach(function(s){
  if(s&&s.unit&&!seen[s.unit]){ seen[s.unit]=1; out.push(s.unit); } });
 return out;
}
function fillLogUnits(){
 var sel=$('logUnit'), cur=sel.value;
 var units=logUnits();
 sel.innerHTML=units.map(function(u){
  return '<option value="'+esc(u)+'">'+esc(UNIT_LABEL[u]||u)+'</option>';
 }).join('');
 if(cur&&units.indexOf(cur)>=0) sel.value=cur;
 else { var pref=store('log_unit'); if(pref&&units.indexOf(pref)>=0) sel.value=pref; }
}
async function loadLogs(btn){
 var unit=$('logUnit').value, n=$('logLines').querySelector('[aria-pressed=true]');
 var lines=n?n.getAttribute('data-n'):'200';
 store('log_unit',unit);
 await run(btn,async function(){
  var j=await api('/api/logs?unit='+encodeURIComponent(unit)+'&lines='+encodeURIComponent(lines));
  S.logs=Array.isArray(j.lines)?j.lines:[];
  renderLogs();
  setMsg($('logMsg'),S.logs.length+' lines '+MID+' '+(UNIT_LABEL[unit]||unit),'');
 },$('logMsg'));
}

// ---- system ---------------------------------------------------------------
var SYS_LABEL={uptime_s:'Uptime',cpu_pct:'CPU',mem_pct:'Memory',disk_pct:'Disk',
 temp_c:'Temperature',commit:'Deployed commit',loadavg:'Load average',
 hostname:'Hostname',kernel:'Kernel',model:'Model',mem_total_kb:'Memory total',
 mem_available_kb:'Memory available',disk_total_b:'Disk total',disk_free_b:'Disk free',
 boot_time:'Booted',robot_serial:'Robot serial',robot_wlan_mac:'Wi-Fi MAC',
 recorded_serial:'Recorded serial'};
function fmtLoad(v){
 if(Array.isArray(v)) return v.map(function(x){return isNum(x)?x.toFixed(2):DASH;}).join('  ');
 if(isNum(v)) return v.toFixed(2);
 if(v&&typeof v==='object'){
  var a=[v['1'],v['5'],v['15'],v.one,v.five,v.fifteen].filter(isNum);
  if(a.length) return a.map(function(x){return x.toFixed(2);}).join('  ');
 }
 return DASH;
}
function fmtBytes(b){
 if(!isNum(b)) return DASH;
 var u=['B','KB','MB','GB','TB'], i=0, v=b;
 while(v>=1024&&i<u.length-1){ v/=1024; i++; }
 return (v>=10?Math.round(v):Math.round(v*10)/10)+' '+u[i];
}
function renderSystem(){
 var s=S.sys||(S.overview&&S.overview.system)||{};
 $('sysTiles').innerHTML=
  tile('Uptime',uptime(s.uptime_s))+
  tile('CPU',pct(s.cpu_pct),lvl(s.cpu_pct,80,95))+
  tile('Memory',pct(s.mem_pct),lvl(s.mem_pct,80,92))+
  tile('Disk',pct(s.disk_pct),lvl(s.disk_pct,85,94))+
  tile('Temp',isNum(s.temp_c)?Math.round(s.temp_c)+'\\u00b0C':DASH,lvl(s.temp_c,70,80));
 var rows='';
 if(s.loadavg!==undefined) rows+=row('Load average','<span class=mono>'+esc(fmtLoad(s.loadavg))+'</span>');
 rows+=row('Deployed commit','<span class=mono>'+esc(txt(s.commit))+'</span>');
 // Anything else the service chose to report is shown verbatim rather than
 // dropped: this page is the only window an operator on site has.
 // `ok` is the response envelope's flag, not a fact about the robot: printed as
 // a row it reads as "one thing here is ok", which invites the reading that
 // something else on the page is not.
 var shown={uptime_s:1,cpu_pct:1,mem_pct:1,disk_pct:1,temp_c:1,commit:1,loadavg:1,ok:1};
 Object.keys(s).forEach(function(k){
  if(shown[k]) return;
  var v=s[k];
  if(v===null||v===undefined) return;
  if(typeof v==='object') return;
  var val=String(v);
  if(/_b$/.test(k)&&isNum(v)) val=fmtBytes(v);
  else if(/_kb$/.test(k)&&isNum(v)) val=fmtBytes(v*1024);
  else if(/_pct$/.test(k)&&isNum(v)) val=pct(v);
  rows+=row(SYS_LABEL[k]||k.replace(/_/g,' '),'<span class=mono>'+esc(val)+'</span>','mut');
 });
 $('sysRows').innerHTML=rows;
}
async function loadSystem(){
 try{ S.sys=await api('/api/system'); renderSystem(); }
 catch(e){
  if(e.status===401) return;
  renderSystem();
  setMsg($('sysMsg'),e.message,'bad');
 }
}
async function powerAction(kind,btn){
 var verb=kind==='reboot'?'Reboot':'Shut down';
 var body='<p>'+esc(verb)+' <b>'+esc(ROBOT||'this robot')+'</b> now?</p><ul>'+
  (kind==='reboot'
   ? '<li>Any visitor session ends immediately.</li>'+
     '<li>The robot is back in about ninety seconds and rejoins its Wi-Fi on its own.</li>'
   : '<li>Any visitor session ends immediately.</li>'+
     '<li>Someone has to press the power button on the robot to bring it back.</li>')+
  '<li>This page stops answering until it returns.</li></ul>';
 var yes=await confirmDlg({title:verb+' the robot',body:body,danger:true,
  confirmText:verb+' now',word:CONFIRM_WORD});
 if(!yes) return;
 await run(btn,async function(){
  await api(kind==='reboot'?'/api/system/reboot':'/api/system/poweroff',{method:'POST'});
  setMsg($('dangerMsg'),kind==='reboot'
   ? 'Reboot sent. Reload this page in about ninety seconds.'
   : 'Shutdown sent. Wait for the robot lights to go out before unplugging it.','ok');
 },$('dangerMsg'));
}

// ---- dialogs --------------------------------------------------------------
function dlgOpen(d){
 if(d.showModal){ try{ d.showModal(); return; }catch(e){} }
 d.setAttribute('open',''); d.classList.add('fb'); $('fbback').classList.add('on');
}
function dlgClose(d){
 try{ d.close(); }catch(e){}
 d.removeAttribute('open'); d.classList.remove('fb'); $('fbback').classList.remove('on');
}
var cdResolve=null, cdWord='';
function confirmDlg(o){
 return new Promise(function(res){
  cdResolve=res; cdWord=o.word||'';
  $('cdTitle').textContent=o.title||'Confirm';
  $('cdBody').innerHTML=o.body||'';
  $('cdGo').textContent=o.confirmText||'Confirm';
  $('cdGo').className='btn'+(o.danger?' danger':'');
  var box=$('cdWordBox'), inp=$('cdWord');
  if(cdWord){
   box.hidden=false; inp.value=''; $('cdGo').disabled=true;
   $('cdWordLabel').textContent='Type '+cdWord+' to confirm';
  }else{ box.hidden=true; $('cdGo').disabled=false; }
  dlgOpen($('cdlg'));
  if(cdWord) setTimeout(function(){ try{ inp.focus(); }catch(e){} },60);
 });
}
function cdFinish(v){
 var r=cdResolve; cdResolve=null;
 dlgClose($('cdlg'));
 if(r) r(v);
}
var jdTarget='';
function openJoin(ssid){
 jdTarget=ssid||'';
 $('jdTitle').textContent=ssid?('Join '+ssid):'Join a hidden network';
 $('jdSsid').value=ssid||'';
 $('jdSsid').readOnly=!!ssid;
 $('jdPw').value=''; $('jdPw').type='password';
 $('jdEye').textContent='Show'; $('jdEye').setAttribute('aria-pressed','false');
 setMsg($('jdMsg'),'','');
 $('jdStep1').hidden=false; $('jdStep2').hidden=true;
 dlgOpen($('jdlg'));
}
// An interstitial: the operator reads what is about to
// happen to their own phone BEFORE the robot is told to move.
function joinStep2(){
 var ssid=$('jdSsid').value.trim();
 if(!ssid){ setMsg($('jdMsg'),'Enter the network name.','bad'); return; }
 var ap=(S.net&&S.net.ap_ssid)?String(S.net.ap_ssid):'the robot hotspot';
 $('jdWhat').innerHTML='<p><b>What happens next</b></p><ul>'+
  '<li>The robot leaves the network it is on and joins <b>'+esc(ssid)+'</b>.</li>'+
  '<li>Your phone does not follow it. If you are on '+esc(ap)+
   ' right now, this page goes quiet until you join <b>'+esc(ssid)+'</b> too.</li>'+
  '<li>If the robot cannot join, it brings its own hotspot back by itself. '+
   'Rejoin <b>'+esc(ap)+'</b> and open <code>'+esc(AP_URL)+'</code>.</li>'+
  '<li>Either way, come back to <b>Network</b> and read the last error at the top.</li></ul>';
 $('jdStep1').hidden=true; $('jdStep2').hidden=false;
}
async function joinFire(btn){
 var ssid=$('jdSsid').value.trim(), pw=$('jdPw').value;
 await run(btn,async function(){
  await api('/api/network/connect',{json:{ssid:ssid,password:pw}});
  dlgClose($('jdlg'));
  setMsg($('scanMsg'),'','');
  watchNetwork('connect',ssid);
 },$('jdMsg'));
}

__ADDON_JS__

// ---- wiring ---------------------------------------------------------------
LOADERS.home=function(){ loadOverview(); loadSetup(); };
LOADERS.network=loadNetwork;
LOADERS.audio=loadAudio;
LOADERS.lang=loadLang;
LOADERS.people=loadPeople;
LOADERS.pokamind=function(){ loadPokamind(); loadSettings(); };
LOADERS.menu=function(){ if(!S.setup) loadSetup(); loadMenu(); };
LOADERS.settings=loadSettings;
LOADERS.services=function(){ loadServices().then(fillLogUnits); };
LOADERS.health=loadReady;
LOADERS.logs=fillLogUnits;
LOADERS.system=loadSystem;

$('tabs').addEventListener('click',function(ev){
 var b=ev.target.closest('.tab'); if(b) go(b.getAttribute('data-tab'));
});
document.addEventListener('click',function(ev){
 var g=ev.target.closest('[data-go]'); if(g){ go(g.getAttribute('data-go')); }
});
$('signout').addEventListener('click',function(){
 run($('signout'),async function(){
  try{ await api('/api/logout',{method:'POST'}); }catch(e){}
  location.href='/';
 });
});

// network
$('scanBtn').addEventListener('click',function(){
 var b=$('scanBtn');
 setMsg($('scanMsg'),'Scanning\\u2026','');
 run(b,async function(){
  var j=await api('/api/network/scan',{method:'POST'});
  S.scan=Array.isArray(j.ssids)?j.ssids:[];
  renderScan();
  setMsg($('scanMsg'),S.scan.length?(S.scan.length+' networks found.'):'','');
 },$('scanMsg'));
});
$('otherBtn').addEventListener('click',function(){ openJoin(''); });
$('scanList').addEventListener('click',function(ev){
 var b=ev.target.closest('[data-join]'); if(b) openJoin(b.getAttribute('data-join'));
});
$('savedList').addEventListener('click',function(ev){
 var b=ev.target.closest('[data-forget]'); if(!b) return;
 var ssid=b.getAttribute('data-forget');
 confirmDlg({title:'Forget network',danger:true,confirmText:'Forget',
  body:'<p>The robot will no longer rejoin <b>'+esc(ssid)+'</b> on its own.</p>'+
   '<p style="margin-top:8px">If it is connected to that network right now, it drops off it and '+
   'falls back to its hotspot.</p>'}).then(function(yes){
  if(!yes) return;
  run(b,async function(){
   await api('/api/network/forget',{json:{ssid:ssid}});
   setMsg($('savedMsg'),'Forgot '+ssid+'.','ok');
   await loadNetwork();
  },$('savedMsg'));
 });
});
$('hotspotBtn').addEventListener('click',function(){
 var ap=(S.net&&S.net.ap_ssid)?String(S.net.ap_ssid):'the robot hotspot';
 confirmDlg({title:'Start hotspot now',danger:true,confirmText:'Start hotspot',
  body:'<p>The robot leaves the site Wi-Fi and broadcasts its own network.</p><ul>'+
   '<li>It has <b>no internet</b> in this mode, so it cannot run any session.</li>'+
   '<li>Join <b>'+esc(ap)+'</b> from your phone, then open <code>'+esc(AP_URL)+'</code>.</li>'+
   '<li>This page goes quiet the moment the robot switches. That is expected.</li></ul>'
 }).then(function(yes){
  if(!yes) return;
  run($('hotspotBtn'),async function(){
   await api('/api/network/hotspot',{method:'POST'});
   watchNetwork('hotspot',ap);
  },$('hotspotMsg'));
 });
});

// audio
bindSlider('spk','spkVal',function(v){return String(Math.round(v));},function(v){
 return api('/api/audio/speaker',{json:{volume:Math.round(v)}}).then(function(){
  if(S.audio) S.audio.speaker=Math.round(v);
  loadOverview();
 });
},$('spkMsg'));
bindSlider('mic','micVal',function(v){return String(Math.round(v));},function(v){
 return api('/api/audio/mic',{json:{volume:Math.round(v)}}).then(function(){
  if(S.audio) S.audio.mic=Math.round(v);
  loadOverview();
 });
},$('micMsg'));
bindSlider('msess','msessVal',function(v){return v.toFixed(2);},null,null);
bindSlider('midle','midleVal',function(v){return v.toFixed(2);},null,null);
$('testBtn').addEventListener('click',function(){
 run($('testBtn'),async function(){
  await api('/api/audio/test-sound',{method:'POST'});
  setMsg($('spkMsg'),'Sent. If you heard nothing, raise the speaker and try again.','ok');
 },$('spkMsg'));
});
$('advSave').addEventListener('click',function(){
 run($('advSave'),async function(){
  await api('/api/audio/mic-profiles',{json:{
   session:parseFloat($('msess').value), idle:parseFloat($('midle').value)}});
  setMsg($('advMsg'),'Saved. The host app picks these up at its next profile switch.','ok');
  await loadAudio();
 },$('advMsg'));
});
$('advReset').addEventListener('click',function(){
 run($('advReset'),async function(){
  await api('/api/audio/mic-profiles',{json:{session:null,idle:null}});
  setMsg($('advMsg'),'Back to the robot defaults.','ok');
  await loadAudio();
 },$('advMsg'));
});

// language
$('langEn').addEventListener('click',function(){ setLang('en',$('langEn')); });
$('langCa').addEventListener('click',function(){ setLang('ca',$('langCa')); });

// pokamind
$('pkTest').addEventListener('click',function(){ pkTest($('pkTest')); });
$('pkReplace').addEventListener('click',openReplace);
$('pkReveal').addEventListener('click',openReveal);
$('tkGo').addEventListener('click',replaceStep);
$('tkNo').addEventListener('click',function(){ $('tkIn').value=''; dlgClose($('tkdlg')); });
$('tkIn').addEventListener('keydown',function(ev){
 if(ev.key==='Enter'){ ev.preventDefault(); replaceStep(); } });
$('tkEye').addEventListener('click',function(){
 var f=$('tkIn'), show=f.type==='password';
 f.type=show?'text':'password';
 this.textContent=show?'Hide':'Show';
 this.setAttribute('aria-pressed',show?'true':'false');
});
$('tkdlg').addEventListener('cancel',function(ev){
 ev.preventDefault(); $('tkIn').value=''; dlgClose($('tkdlg')); });
$('pinGo').addEventListener('click',function(){ revealFire($('pinGo')); });
$('pinNo').addEventListener('click',function(){ $('pinIn').value=''; dlgClose($('pindlg')); });
$('pinIn').addEventListener('keydown',function(ev){
 if(ev.key==='Enter'){ ev.preventDefault(); revealFire($('pinGo')); } });
$('pindlg').addEventListener('cancel',function(ev){
 ev.preventDefault(); $('pinIn').value=''; dlgClose($('pindlg')); });

// people
$('ppAdd').addEventListener('click',function(){ ppAdd($('ppAdd')); });
$('ppEmail').addEventListener('keydown',function(ev){
 if(ev.key==='Enter'){ ev.preventDefault(); ppAdd($('ppAdd')); } });
$('ppList').addEventListener('click',function(ev){
 var b=ev.target.closest('[data-pbadge],[data-pedit],[data-pdel]'); if(!b) return;
 if(b.hasAttribute('data-pbadge')) openBadge(b.getAttribute('data-pbadge'));
 else if(b.hasAttribute('data-pedit')) openEdit(b.getAttribute('data-pedit'));
 else ppRemove(b.getAttribute('data-pdel'),b);
});
$('ppdGo').addEventListener('click',function(){ ppSave($('ppdGo')); });
$('ppdNo').addEventListener('click',function(){ dlgClose($('ppdlg')); });
$('ppdEmail').addEventListener('keydown',function(ev){
 if(ev.key==='Enter'){ ev.preventDefault(); ppSave($('ppdGo')); } });
$('ppdlg').addEventListener('cancel',function(ev){ ev.preventDefault(); dlgClose($('ppdlg')); });
$('bgClose').addEventListener('click',closeBadge);
$('bgdlg').addEventListener('cancel',function(ev){ ev.preventDefault(); closeBadge(); });

// services
$('svcRefresh').addEventListener('click',function(){
 run($('svcRefresh'),loadServices,$('svcMsg'));
});
$('hubBackGo').addEventListener('click',function(){ hubBack($('hubBackGo')); });
$('svcList').addEventListener('click',function(ev){
 var b=ev.target.closest('[data-svc]'); if(!b) return;
 svcAction(b.getAttribute('data-unit'),b.getAttribute('data-svc'),b);
});

// health
$('readyRun').addEventListener('click',function(){
 // run() re-enables the button in its finally; renderReady owns the disabled
 // state while a check is in flight, so it gets the last word.
 run($('readyRun'),async function(){
  try{ await api('/api/readycheck/run',{method:'POST'}); }
  catch(e){ if(e.status!==409) throw e; }
  await loadReady();
 },$('readyMsg')).then(renderReady);
});

// logs
$('logLines').addEventListener('click',function(ev){
 var b=ev.target.closest('button[data-n]'); if(!b) return;
 Array.prototype.forEach.call(this.querySelectorAll('button'),function(x){
  x.setAttribute('aria-pressed',x===b?'true':'false'); });
});
$('logRefresh').addEventListener('click',function(){ loadLogs($('logRefresh')); });
$('logUnit').addEventListener('change',function(){ store('log_unit',this.value); });

// system
$('sysRefresh').addEventListener('click',function(){
 run($('sysRefresh'),loadSystem,$('sysMsg'));
});
$('rebootBtn').addEventListener('click',function(){ powerAction('reboot',$('rebootBtn')); });
$('offBtn').addEventListener('click',function(){ powerAction('poweroff',$('offBtn')); });

// dialogs
$('cdGo').addEventListener('click',function(){ cdFinish(true); });
$('cdNo').addEventListener('click',function(){ cdFinish(false); });
$('cdlg').addEventListener('cancel',function(ev){ ev.preventDefault(); cdFinish(false); });
$('cdWord').addEventListener('input',function(){
 $('cdGo').disabled=this.value.trim().toLowerCase()!==cdWord.trim().toLowerCase();
});
$('cdWord').addEventListener('keydown',function(ev){
 if(ev.key==='Enter'&&!$('cdGo').disabled){ ev.preventDefault(); cdFinish(true); }
});
$('jdNext').addEventListener('click',joinStep2);
$('jdBack').addEventListener('click',function(){
 $('jdStep2').hidden=true; $('jdStep1').hidden=false;
});
$('jdCancel1').addEventListener('click',function(){ dlgClose($('jdlg')); });
$('jdGo').addEventListener('click',function(){ joinFire($('jdGo')); });
$('jdlg').addEventListener('cancel',function(ev){ ev.preventDefault(); dlgClose($('jdlg')); });
$('jdEye').addEventListener('click',function(){
 var p=$('jdPw'), show=p.type==='password';
 p.type=show?'text':'password';
 this.textContent=show?'Hide':'Show';
 this.setAttribute('aria-pressed',show?'true':'false');
});
$('fbback').addEventListener('click',function(){
 if($('cdlg').hasAttribute('open')) cdFinish(false);
 if($('jdlg').hasAttribute('open')) dlgClose($('jdlg'));
 if($('tkdlg').hasAttribute('open')){ $('tkIn').value=''; dlgClose($('tkdlg')); }
 if($('pindlg').hasAttribute('open')){ $('pinIn').value=''; dlgClose($('pindlg')); }
 if($('ppdlg').hasAttribute('open')) dlgClose($('ppdlg'));
 if($('bgdlg').hasAttribute('open')) closeBadge();
 if($('fmdlg').hasAttribute('open')) fmFinish(false);
});
// menu
$('mnLang').addEventListener('click',function(ev){
 var b=ev.target.closest('button[data-lang]'); if(!b||b.disabled) return;
 MN.lang=b.getAttribute('data-lang'); MN.data=null; MN.opts=[];
 loadMenu();
});
$('mnOpts').addEventListener('click',function(ev){
 var b=ev.target.closest('[data-mup],[data-mdown],[data-mdel]'); if(!b) return;
 mnCollect();
 var i;
 if(b.hasAttribute('data-mup')){ i=parseInt(b.getAttribute('data-mup'),10);
  if(i>0){ var a=MN.opts[i]; MN.opts[i]=MN.opts[i-1]; MN.opts[i-1]=a; } }
 else if(b.hasAttribute('data-mdown')){ i=parseInt(b.getAttribute('data-mdown'),10);
  if(i<MN.opts.length-1){ var c=MN.opts[i]; MN.opts[i]=MN.opts[i+1]; MN.opts[i+1]=c; } }
 else { i=parseInt(b.getAttribute('data-mdel'),10); MN.opts.splice(i,1); }
 renderMenuOpts();
});
$('mnAddBtn').addEventListener('click',function(){
 var id=$('mnAdd').value; if(!id||MN.opts.length>=6) return;
 mnCollect();
 var m=mnModule(id)||{};
 // A first name from the title, cut where a title usually goes on; edit it before saving.
 var name=String(m.title||'').split(/[,:;(]| - /)[0].trim().slice(0,40);
 MN.opts.push({module_id:id,name:name,keywords:[]});
 renderMenuOpts();
});
$('mnPromptGen').addEventListener('click',mnPromptFromNames);
$('mnSave').addEventListener('click',function(){ mnSave($('mnSave')); });
$('mnPreview').addEventListener('click',function(){ mnPreview($('mnPreview')); });
$('mnReset').addEventListener('click',function(){ mnReset($('mnReset')); });

// settings
$('v-settings').addEventListener('click',function(ev){
 var b=ev.target.closest('[data-stsave],[data-stclear],[data-cred]'); if(!b) return;
 if(b.hasAttribute('data-stsave')) stSave(b.getAttribute('data-stsave'),b);
 else if(b.hasAttribute('data-cred')) credAction(b.getAttribute('data-cred'),b);
 else {
  var k=b.getAttribute('data-stclear'); STCLEAR[k]=true;
  var el=$('st_'+k); if(el){ el.value=''; el.placeholder='cleared \\u2014 tap Save'; }
  b.disabled=true;
 }
});
$('stTest').addEventListener('click',function(){ stTest($('stTest')); });
document.addEventListener('click',function(ev){
 var b=ev.target.closest('[data-apply]'); if(b) applyNow(b);
});
$('pkSync').addEventListener('click',function(){ pkSync($('pkSync')); });

// the form dialog
$('fmGo').addEventListener('click',function(){ fmFinish(true); });
$('fmNo').addEventListener('click',function(){ fmFinish(false); });
$('fmdlg').addEventListener('cancel',function(ev){ ev.preventDefault(); fmFinish(false); });
$('fmFields').addEventListener('keydown',function(ev){
 if(ev.key==='Enter'){ ev.preventDefault(); fmFinish(true); } });
window.addEventListener('hashchange',function(){
 var t=(location.hash||'').replace('#','');
 if(t&&t!==active) go(t);
});

// ---- boot -----------------------------------------------------------------
fillLogUnits();
renderSetup();
go((location.hash||'').replace('#','')||store('tab')||'home');
loadOverview();
// One timer for the whole page. Home polls every 5s; on the
// other tabs the same call still runs, at 20s, purely to keep the header status
// chip honest -- the CM4 has under a core spare and a live session owns it.
setInterval(function(){
 if(document.visibilityState!=='visible') return;
 tickN++;
 if(active==='home'||tickN%4===0) loadOverview();
 // The checklist reads six host app routes: every 30 s on Home is plenty.
 if(active==='home'&&tickN%6===0) loadSetup();
 // A way back takes minutes with the motor, and its block says the list follows it.
 if(active==='services'&&S.hub.sw&&S.hub.sw.running) loadServices();
},5000);
document.addEventListener('visibilitychange',function(){
 if(document.visibilityState==='visible') loadOverview();
 else { hideReveal(); hideSecret(); }  // a phone put away keeps no secret on screen
});
"""

APP_HTML = (
    _HEAD
    + """<title>EMORI robot admin</title><style>"""
    + _BASE_CSS
    + _APP_CSS
    + """</style></head><body>"""
    + _APP_BODY
    + "<script>"
    + _APP_JS
    + """</script></body></html>"""
)
