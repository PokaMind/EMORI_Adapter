"""Printable QR badges for the roster: one SVG badge, a PNG of the code, an A4 sheet.

What a badge encodes is exactly what the kiosk's scanner hands to ``/api/identify``:
the roster KEY (``identity.Roster.resolve`` looks the decoded string up verbatim, after
``server._decoded_qr_payload`` strips surrounding whitespace). It is an opaque badge
id -- ``EMORI:`` and ten random characters for a badge issued on the admin page -- and
never a name or an e-mail address: those stay in ``configs/roster.yaml`` on the robot.

The payloads the admin page issues are upper-case letters, digits and ``:`` only, so
the QR is in ALPHANUMERIC mode and fits a version-1 symbol (21x21 modules) at error
correction Q. Few, large modules are what a wide-angle robot camera reads at arm's
length: on the 90 x 88 mm badge below the code is 58 mm square including its 4-module
quiet zone, i.e. 2 mm per module, with the rest of the white card around it.

Rendering is pure Python (``segno``, no dependencies) and works offline: no CDN, no
web font, no network. ``segno`` is a hostapp dependency (``pyproject.toml``); a robot
updated by a QUICK deploy may not have it yet, which :func:`available` reports so the
admin page can say "deploy again" instead of failing obscurely.
"""

from __future__ import annotations

import html
import io
from typing import Iterable, Optional, Sequence

# Error correction Q (25% of the symbol can be lost): a printed badge gets creased,
# glared and held at an angle. segno may still raise it when the version has room.
ERROR_LEVEL = "q"
QUIET_ZONE = 4  # modules: the QR spec minimum; the card around it adds more

# Badge geometry, millimetres. Two columns x three rows fill an A4 page inside 10 mm
# margins (180 x 264 of 190 x 277).
BADGE_W_MM = 90.0
BADGE_H_MM = 88.0
QR_MM = 58.0
PER_PAGE = 6

_INK = "#111418"
_BRAND = "#3F659F"
_MUTED = "#5E6670"


class BadgeUnavailable(RuntimeError):
    """``segno`` is not installed in this robot's venv (a quick deploy)."""


def available() -> bool:
    try:
        import segno  # noqa: F401,PLC0415 - optional until the next full deploy
    except Exception:  # noqa: BLE001 - any import failure is "not available"
        return False
    return True


def make_qr(payload: str):
    """The segno QR for one badge payload (never a Micro QR: phones and jsQR skip those)."""

    try:
        import segno  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        raise BadgeUnavailable("segno is not installed") from e
    return segno.make(payload, error=ERROR_LEVEL, micro=False)


def _modules(qr) -> list[list[bool]]:
    return [[bool(cell) for cell in row]
            for row in qr.matrix_iter(scale=1, border=0, verbose=False)]


def qr_path(payload: str) -> tuple[str, int]:
    """``(svg path data, side in modules incl. the quiet zone)`` for ``payload``.

    One ``<path>`` in module units, runs of dark modules merged per row: a few hundred
    bytes, and it scales to any print size without a single blurred edge.
    """

    matrix = _modules(make_qr(payload))
    parts: list[str] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            parts.append("M%d %dh%dv1h-%dz" % (start + QUIET_ZONE, y + QUIET_ZONE,
                                               x - start, x - start))
    return "".join(parts), len(matrix) + 2 * QUIET_ZONE


def qr_svg(payload: str, *, size_mm: float = QR_MM) -> str:
    """A standalone SVG of the code alone (quiet zone included), sized in millimetres."""

    path, side = qr_path(payload)
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%gmm" height="%gmm" '
            'viewBox="0 0 %d %d" shape-rendering="crispEdges">'
            '<rect width="%d" height="%d" fill="#fff"/><path fill="%s" d="%s"/></svg>'
            % (size_mm, size_mm, side, side, side, side, _INK, path))


def qr_png(payload: str, *, scale: int = 16) -> bytes:
    """A PNG of the code alone (segno's own writer: no Pillow needed)."""

    buf = io.BytesIO()
    make_qr(payload).save(buf, kind="png", scale=scale, border=QUIET_ZONE)
    return buf.getvalue()


def _label(robot_name: str, site_name: Optional[str]) -> str:
    robot = (robot_name or "").strip()
    site = (site_name or "").strip()
    if site and site.casefold() != robot.casefold():
        return "%s · %s" % (robot, site) if robot else site
    return robot


def _name_font_mm(name: str) -> float:
    # Average glyph advance of a sans bold is ~0.58 em; keep the line inside 80 mm.
    return max(3.6, min(6.4, 80.0 / (0.58 * max(1, len(name)))))


def badge_svg(payload: str, name: str, *, robot_name: str = "",
              site_name: Optional[str] = None, standalone: bool = True) -> str:
    """One badge: the site line, the code, the person's name and a one-line hint.

    ``name`` and the labels are operator input, so they are XML-escaped; nothing else
    from outside reaches the markup. ``standalone`` adds the XML namespace for a file
    download; the print sheet inlines the same markup into HTML.
    """

    path, side = qr_path(payload)
    w, h = BADGE_W_MM, BADGE_H_MM
    qr_x, qr_y = (w - QR_MM) / 2, 12.5
    name = (name or "").strip()
    font = _name_font_mm(name)
    squeeze = ' textLength="80" lengthAdjust="spacingAndGlyphs"' if (
        0.58 * font * len(name) > 80) else ""
    label = _label(robot_name, site_name)
    ns = ' xmlns="http://www.w3.org/2000/svg"' if standalone else ""
    return (
        '<svg%s width="%gmm" height="%gmm" viewBox="0 0 %g %g" role="img" '
        'aria-label="%s" font-family="Helvetica, Arial, sans-serif">'
        '<rect x="0.25" y="0.25" width="%g" height="%g" rx="4" fill="#fff" '
        'stroke="#B8BEC6" stroke-width="0.3" stroke-dasharray="1.5 1.2"/>'
        '<text x="%g" y="8.6" text-anchor="middle" font-size="3.4" font-weight="700" '
        'letter-spacing="0.25" fill="%s">%s</text>'
        '<svg x="%g" y="%g" width="%g" height="%g" viewBox="0 0 %d %d" '
        'shape-rendering="crispEdges"><rect width="%d" height="%d" fill="#fff"/>'
        '<path fill="%s" d="%s"/></svg>'
        '<text x="%g" y="78.2" text-anchor="middle" font-size="%.2f" font-weight="700" '
        'fill="%s"%s>%s</text>'
        '<text x="%g" y="84.4" text-anchor="middle" font-size="2.5" fill="%s">'
        'Show this to the robot’s camera · Mostra-la a la càmera del robot'
        '</text></svg>'
        % (ns, w, h, w, h, html.escape("Badge: " + name, quote=True), w - 0.5, h - 0.5,
           w / 2, _BRAND, html.escape(label.upper(), quote=False),
           qr_x, qr_y, QR_MM, QR_MM, side, side, side, side, _INK, path,
           w / 2, font, _INK, squeeze, html.escape(name, quote=False),
           w / 2, _MUTED))


def _chunks(items: Sequence, size: int) -> Iterable[Sequence]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


_SHEET_CSS = """
@page{size:A4 portrait;margin:10mm}
*{box-sizing:border-box}
body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
 color:#1B1F24;background:#EEF0F3}
.bar{padding:16px;max-width:760px;margin:0 auto}
.bar h1{font-size:20px;margin:0 0 4px}
.bar p{margin:6px 0;color:#4A525C}
.bar button{font:inherit;font-weight:700;min-height:44px;padding:0 18px;border-radius:10px;
 border:0;background:#5C83BF;color:#fff;margin-top:8px}
.page{width:190mm;margin:0 auto 16px;background:#fff;display:grid;
 grid-template-columns:90mm 90mm;grid-auto-rows:88mm;column-gap:10mm;row-gap:0;
 padding:0;break-after:page;page-break-after:always}
.page:last-child{break-after:auto;page-break-after:auto}
.page svg{display:block}
.empty{max-width:760px;margin:24px auto;padding:16px;background:#fff;border-radius:12px}
@media print{
 body{background:#fff}
 .bar{display:none}
 .page{margin:0}
}
"""


def sheet_html(people: Sequence[tuple[str, str]], *, robot_name: str = "",
               site_name: Optional[str] = None) -> str:
    """An A4 print view: six badges a page, cut lines, a Print button on screen only.

    ``people`` is ``[(payload, name), ...]``. Self-contained (inline CSS, inline SVG,
    one inline ``print()``): the page has to work with the phone on the robot's own
    hotspot, where there is no internet.
    """

    label = _label(robot_name, site_name)
    title = "Badges · %s" % label if label else "Badges"
    pages = []
    for chunk in _chunks(list(people), PER_PAGE):
        cells = "".join(
            badge_svg(payload, name, robot_name=robot_name, site_name=site_name,
                      standalone=False)
            for payload, name in chunk)
        pages.append('<section class="page">%s</section>' % cells)
    count = len(people)
    body = "".join(pages) if pages else (
        '<p class="empty">Nobody is on the roster yet. Add people on the admin page '
        'first.</p>')
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex"><title>%s</title><style>%s</style></head>'
        '<body><div class="bar"><h1>%s</h1>'
        '<p>%d badge%s, %d per A4 page. Print at <b>100%%</b> (turn off "fit to page") on '
        'white paper, then cut along the dashed lines. A badge works as soon as the person '
        'is on the roster.</p>'
        '<p>Keep the white margin around each code: the camera needs it.</p>'
        '<button type="button" onclick="window.print()">Print</button></div>%s'
        '</body></html>'
        % (html.escape(title), _SHEET_CSS, html.escape(title), count,
           "" if count == 1 else "s", PER_PAGE, body))
