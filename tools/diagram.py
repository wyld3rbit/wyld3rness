"""Draw each decision diagram from its spec file: one source per picture, with the live page's own icons.

    code/docs/adr/fig/<name>.toml ──► code/docs/adr/img/<name>.svg       (the ADR shows it)
                                  └─► the live page, between <!-- diagram <name> --> markers
                                       (the console shows it under "Where it runs")
    code/docs/fig/<name>.toml     ──► code/docs/img/<name>.svg           (the design document and
                                                                          READMEs show it; no page copy)

Why two folders: an ADR picture records a decision as it was made, and the
console shows it beside the decision. A docs picture shows the system as it is
now and changes with it, so it has no place in a dated record.

Why a spec and not the live page: the page draws the logical flow (what talks to
what), while a decision often needs another view, such as where each part runs.
Why not Mermaid: it has no Lucide icons. The spec holds only layout and words;
each box's icon is taken from the box of that name on the live page, so icons and
colours have one source too (the page's pinned Lucide 0.460.0, see NOTICE).

A spec has zones (labelled areas: a machine, a container, a network), boxes
(name, subtitle, the page box whose icon it borrows, built or planned) and
edges (from, to, an optional label, built or planned, and optional `via` points
for an edge that bends around boxes). An edge leaves and enters its boxes on the
sides facing its next point; a label sits at the middle of the edge's longest
segment, beside it when that segment is vertical. Class names start with `dg-`, so a diagram
inside the page can neither restyle the page nor be counted as one of its
nodes by the page's own checks (code/examples/findyfixy/tests/test_page_truth.py).

The drift guard is a test (tools/test_diagram.py): every committed picture,
and every copy in the page, must equal a fresh render of its spec. Edit a
picture by hand and `make check` fails.

Run from the training root: `make adr-snapshot` (it draws these too). Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from pathlib import Path
import re
import sys
import tomllib

import adr_snapshot as snap

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "code" / "docs" / "adr" / "fig"
OUT = ROOT / "code" / "docs" / "adr" / "img"
DOC_SPECS = ROOT / "code" / "docs" / "fig"
DOC_OUT = ROOT / "code" / "docs" / "img"
PAGE = snap.PAGE
BOX_W, BOX_H = 150, 64
CAPTION_SPACE = 36

STYLE = """\
<style>
.dg-zone { fill: #0d1729; stroke: #2a3a5c; stroke-width: 1; }
.dg-zone.harness { stroke: #c084fc; stroke-opacity: .45; }
.dg-zone.human { stroke: #e07a5f; stroke-opacity: .6; }
.dg-zone-label { fill: #8da2c8; font-size: 12px; font-weight: 600; letter-spacing: .04em; }
.dg-zone-label.harness { fill: #c084fc; }
.dg-zone-label.human { fill: #e07a5f; }
.dg-box rect { fill: #0b1220; stroke: #5cc8ff; stroke-width: 1.5; }
.dg-box .dg-t { fill: #dbe6ff; font-size: 14px; font-weight: 600; text-anchor: middle; }
.dg-box .dg-s { fill: #8da2c8; font-size: 11px; text-anchor: middle; }
.dg-box.planned rect { stroke: #7f8fb0; stroke-dasharray: 5 4; }
.dg-box.planned .dg-t, .dg-box.planned .dg-s { fill: #7f8fb0; }
.dg-box .i { fill: none; stroke: #e07a5f; stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.dg-box.planned .i { stroke: #7f8fb0; }
.dg-edge { fill: none; stroke: #5b6f99; stroke-width: 2; marker-end: url(#dg-arrow); }
.dg-edge.planned { stroke: #7f8fb0; stroke-width: 1.5; stroke-dasharray: 5 5; marker-end: url(#dg-arrow-planned); }
.dg-edge-label { fill: #8da2c8; font-size: 11px; }  /* each label sets its own text-anchor */
.dg-caption { fill: #dbe6ff; font-size: 14px; font-weight: 600; }
.dg text { font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
</style>
"""
DEFS = (
    '<defs><marker id="dg-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
    'orient="auto"><path d="M0,1 L10,5 L0,9 z" fill="#5b6f99"/></marker>'
    '<marker id="dg-arrow-planned" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
    'orient="auto"><path d="M0,1 L10,5 L0,9 z" fill="#7f8fb0"/></marker></defs>\n'
)
STATUSES = ("built", "planned")
ZONE_KINDS = ("plain", "harness", "human")


class SpecError(ValueError):
    """A spec names something that does not exist, or is missing a field. The message says which."""


@dataclass(frozen=True)
class Box:
    """One box: where it sits, its words, and its status."""

    id: str
    name: str
    sub: str
    icon: str
    x: float
    y: float
    status: str

    @property
    def center(self) -> tuple[float, float]:
        """The box's middle."""
        return self.x + BOX_W / 2, self.y + BOX_H / 2


def page_icons(page: str) -> dict[str, str]:
    """Each page box's icon elements, by box name, with the box's own position removed. Pure."""
    nodes, _, _, _ = snap.page_parts(page)
    icons = {}
    for block in nodes:
        shapes = re.findall(r"<(?:path|circle|rect|line|polyline|polygon|ellipse) class=\"i\"[^>]*/>", block)
        icons[snap.node_name(block)] = "".join(re.sub(r' transform="[^"]*"', "", s) for s in shapes)
    return icons


def _field(entry: dict[str, object], key: str, where: str) -> object:
    """A required field of a spec entry, or SpecError naming it."""
    if key not in entry:
        raise SpecError(f"{where}: missing {key!r}")
    return entry[key]


def _status(entry: dict[str, object], where: str) -> str:
    """The entry's status, built or planned."""
    status = str(entry.get("status", "built"))
    if status not in STATUSES:
        raise SpecError(f"{where}: status must be one of {STATUSES}, got {status!r}")
    return status


Point = tuple[float, float]


def _side(box: Box, toward: Point) -> Point:
    """Where an edge meets `box` on its way to or from `toward`: the side facing it, aligned with it. Pure.

    The side is chosen by the larger gap from the box's middle. Along that side
    the point lines up with `toward` when it can, so an edge to a point straight
    above or beside the box stays straight.
    """
    (cx, cy), (tx, ty) = box.center, toward
    if abs(tx - cx) > abs(ty - cy):
        x = box.x + BOX_W if tx > cx else box.x
        return x, min(max(ty, box.y), box.y + BOX_H)
    y = box.y + BOX_H if ty > cy else box.y
    return min(max(tx, box.x), box.x + BOX_W), y


def route(a: Box, b: Box, via: list[Point]) -> list[Point]:
    """The points of an edge from `a` to `b` through `via`. Pure."""
    first = via[0] if via else b.center
    last = via[-1] if via else a.center
    return [_side(a, first), *via, _side(b, last)]


def _label_at(points: list[Point]) -> tuple[float, float, str]:
    """Where an edge's label goes: the middle of its longest segment, beside it if vertical. Pure."""
    segments = list(zip(points, points[1:], strict=False))
    (x1, y1), (x2, y2) = max(segments, key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))
    if x1 == x2:
        return x1 + 8, (y1 + y2) / 2 + 4, "start"
    return (x1 + x2) / 2, (y1 + y2) / 2 - 6, "middle"


def render(spec_text: str, page: str) -> str:
    """The SVG for one spec, with icons from `page`. Pure. Raises SpecError on an unknown icon or box."""
    spec = tomllib.loads(spec_text)
    icons = page_icons(page)
    width, height = float(_field(spec, "width", "spec")), float(_field(spec, "height", "spec"))
    caption = str(_field(spec, "caption", "spec"))
    out = []
    for i, z in enumerate(spec.get("zone", [])):
        where, kind = f"zone {i + 1}", str(z.get("kind", "plain"))
        if kind not in ZONE_KINDS:
            raise SpecError(f"{where}: kind must be one of {ZONE_KINDS}, got {kind!r}")
        x, y, w, h = (float(_field(z, k, where)) for k in ("x", "y", "w", "h"))
        cls = "" if kind == "plain" else f" {kind}"
        out.append(f'<rect class="dg-zone{cls}" x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="10"/>')
        out.append(
            f'<text class="dg-zone-label{cls}" x="{x + 12:g}" y="{y + 18:g}">{escape(str(_field(z, "label", where)))}</text>'
        )
    boxes: dict[str, Box] = {}
    for i, b in enumerate(spec.get("box", [])):
        where = f"box {i + 1}"
        box = Box(
            str(_field(b, "id", where)),
            str(_field(b, "name", where)),
            str(b.get("sub", "")),
            str(_field(b, "icon", where)),
            float(_field(b, "x", where)),
            float(_field(b, "y", where)),
            _status(b, where),
        )
        if box.icon not in icons:
            raise SpecError(f"{where}: no box named {box.icon!r} on the live page to take an icon from")
        if box.id in boxes:
            raise SpecError(f"{where}: id {box.id!r} is used twice")
        boxes[box.id] = box
    edges = []
    for i, e in enumerate(spec.get("edge", [])):
        where = f"edge {i + 1}"
        ends = [str(_field(e, k, where)) for k in ("from", "to")]
        missing = [end for end in ends if end not in boxes]
        if missing:
            raise SpecError(f"{where}: no box with id {missing}")
        via = [(float(x), float(y)) for x, y in e.get("via", [])]
        points = route(boxes[ends[0]], boxes[ends[1]], via)
        cls = " planned" if _status(e, where) == "planned" else ""
        d = "M" + " L".join(f"{x:g},{y:g}" for x, y in points)
        edges.append(f'<path class="dg-edge{cls}" d="{d}"/>')
        if e.get("label"):
            lx, ly, anchor = _label_at(points)
            edges.append(
                f'<text class="dg-edge-label" x="{lx:g}" y="{ly:g}" text-anchor="{anchor}">'
                f"{escape(str(e['label']))}</text>"
            )
    for box in boxes.values():
        cls = " planned" if box.status == "planned" else ""
        cx = box.x + BOX_W / 2 + 10
        out_box = (
            f'<g class="dg-box{cls}"><rect x="{box.x:g}" y="{box.y:g}" width="{BOX_W}" height="{BOX_H}" rx="8"/>'
            f'<g transform="translate({box.x + 5:g} {box.y + 4:g}) scale(.56)">{icons[box.icon]}</g>'
            f'<text class="dg-t" x="{cx:g}" y="{box.y + 26:g}">{escape(box.name)}</text>'
            f'<text class="dg-s" x="{cx:g}" y="{box.y + 44:g}">{escape(box.sub)}</text></g>'
        )
        edges.append(out_box)
    top = -CAPTION_SPACE
    return (
        f'<svg class="dg" xmlns="http://www.w3.org/2000/svg" viewBox="0 {top:g} {width:g} {height + CAPTION_SPACE:g}" '
        f'width="{width:g}" height="{height + CAPTION_SPACE:g}" role="img" aria-label="{escape(caption)}">\n'
        f"{STYLE}{DEFS}"
        f'<rect x="0" y="{top:g}" width="{width:g}" height="{height + CAPTION_SPACE:g}" fill="#0b1220"/>\n'
        f'<text class="dg-caption" x="14" y="{top + 24:g}">{escape(caption)}</text>\n'
        + "\n".join(out)
        + "\n"
        + "\n".join(edges)
        + "\n</svg>\n"
    )


def specs() -> list[Path]:
    """Every ADR spec file, sorted."""
    return sorted(SPECS.glob("*.toml"))


def doc_specs() -> list[Path]:
    """Every docs spec file, sorted."""
    return sorted(DOC_SPECS.glob("*.toml"))


def region(name: str) -> re.Pattern[str]:
    """The page region that holds diagram `name`'s copy."""
    return re.compile(rf"(<!-- diagram {re.escape(name)} -->\n)(.*?)(<!-- /diagram -->)", re.S)


def page_copy(page: str, name: str) -> str | None:
    """The copy of diagram `name` inside the page, or None when the page has no region for it. Pure."""
    found = region(name).search(page)
    return found.group(2) if found else None


def with_copy(page: str, name: str, svg: str) -> str:
    """The page with diagram `name`'s region holding `svg`. Raises SpecError if the page has no such region. Pure."""
    if page_copy(page, name) is None:
        raise SpecError(f"the live page has no <!-- diagram {name} --> region")
    return region(name).sub(lambda m: m.group(1) + svg + m.group(3), page, count=1)


def main() -> int:
    """Draw every ADR spec into its picture and the page's region for it, then every docs spec into its picture."""
    page = PAGE.read_text()
    OUT.mkdir(parents=True, exist_ok=True)
    for spec in specs():
        svg = render(spec.read_text(), page)
        (OUT / f"{spec.stem}.svg").write_text(svg)
        page = with_copy(page, spec.stem, svg)
        print(f"wrote {(OUT / f'{spec.stem}.svg').relative_to(ROOT)} and its copy in the live page")
    PAGE.write_text(page)
    DOC_OUT.mkdir(parents=True, exist_ok=True)
    for spec in doc_specs():
        (DOC_OUT / f"{spec.stem}.svg").write_text(render(spec.read_text(), page))
        print(f"wrote {(DOC_OUT / f'{spec.stem}.svg').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
