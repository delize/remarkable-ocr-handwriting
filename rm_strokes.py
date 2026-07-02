#!/usr/bin/env python3
"""
rm_strokes.py — best-effort region hints from reMarkable stroke (ink) data.

Parses the vector stroke geometry that `rmscene` exposes for a single `.rm`
page (pen tool + point coordinates) and clusters strokes into rough bounding
boxes. This is NOT handwriting recognition: no open, offline, pip-installable
ink-to-text engine exists to pair with the vision-LLM OCR core in rm_ocr.py.
The mature ones (reMarkable's own "Convert to text", MyScript iink, Azure Ink
Recognizer) are all proprietary cloud services, which would break this
project's "fully local" guarantee.

What the stroke data DOES give us for free, with no new recognition model: a
rough signal for "this region of the page is probably a sketch/diagram, not
handwritten text" — based only on how big and how squarish a cluster of ink
is. That's useful as a short hint appended to the OCR prompt (so the model
describes a diagram instead of hallucinating a transcription of it), and as
an exported detail in the transcript's frontmatter, independent of whether
the model uses the hint.

Classification is a size/shape heuristic, not a model — it will misfire on
compact diagrams and on effusive handwriting. Treat `likely_drawing` as a
hint, not a fact.
"""
import dataclasses

# reMarkable's own "plain paragraph" line height, in native (226 dpi) scene
# units — the same constant rmc's SVG exporter uses (LINE_HEIGHTS[PLAIN]).
# Used to calibrate the clustering gap and the drawing-size threshold without
# a second magic number.
_LINE_HEIGHT = 70
_CLUSTER_GAP = _LINE_HEIGHT / 2          # merge strokes whose padded bboxes touch
_DRAWING_HEIGHT_THRESHOLD = _LINE_HEIGHT * 2   # taller than ~2 lines stacked


@dataclasses.dataclass
class Region:
    bbox: tuple        # (xmin, ymin, xmax, ymax) in native scene units
    stroke_count: int
    tools: list        # sorted Pen tool names seen in this cluster
    likely_drawing: bool


def _bbox(points):
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _overlaps(a, b, gap):
    return not (a[2] + gap < b[0] or b[2] + gap < a[0]
                or a[3] + gap < b[1] or b[3] + gap < a[1])


def regions_from_lines(lines):
    """Cluster stroke bounding boxes into regions.

    `lines` is an iterable of objects with `.points` (each having `.x`/`.y`)
    and `.tool` (an object with `.name`, e.g. rmscene's `Pen` enum member).
    Pure logic, no I/O — safe to unit-test with small fake objects.
    """
    boxes = []
    for line in lines:
        if not line.points:
            continue
        boxes.append((_bbox(line.points), line.tool.name))
    n = len(boxes)
    if n == 0:
        return []

    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        for j in range(i + 1, n):
            if _overlaps(boxes[i][0], boxes[j][0], _CLUSTER_GAP):
                union(i, j)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    regions = []
    for members in clusters.values():
        xs0 = min(boxes[i][0][0] for i in members)
        ys0 = min(boxes[i][0][1] for i in members)
        xs1 = max(boxes[i][0][2] for i in members)
        ys1 = max(boxes[i][0][3] for i in members)
        tools = sorted({boxes[i][1] for i in members})
        width, height = xs1 - xs0, ys1 - ys0
        aspect = width / height if height else float("inf")
        likely_drawing = height > _DRAWING_HEIGHT_THRESHOLD and 0.3 < aspect < 3.0
        regions.append(Region(bbox=(xs0, ys0, xs1, ys1), stroke_count=len(members),
                               tools=tools, likely_drawing=likely_drawing))
    return regions


def page_regions(rm_path):
    """Parse one `.rm` file and return `regions_from_lines(...)` as plain dicts.

    Lazy-imports rmscene (only needed when STROKE_CONTEXT is actually on).
    Dicts (not dataclasses) so this round-trips through JSON for the render
    cache's sidecar file.
    """
    from rmscene import read_tree
    from rmscene import scene_items as si

    with open(rm_path, "rb") as f:
        tree = read_tree(f)
    lines = [item for item in tree.walk() if isinstance(item, si.Line)]
    return [dataclasses.asdict(r) for r in regions_from_lines(lines)]


def summarize(regions):
    """Small JSON-able summary: counts + tool set — the frontmatter export."""
    regions = regions or []
    return {
        "regions": len(regions),
        "likely_drawing_regions": sum(1 for r in regions if r["likely_drawing"]),
        "tools": sorted({t for r in regions for t in r["tools"]}),
    }


def prompt_hint(regions):
    """One short sentence to append to the OCR prompt, or None if nothing stands out."""
    if not regions:
        return None
    drawings = [r for r in regions if r["likely_drawing"]]
    if not drawings:
        return None
    spots = "; ".join(
        f"({r['bbox'][0]:.0f},{r['bbox'][1]:.0f})-({r['bbox'][2]:.0f},{r['bbox'][3]:.0f})"
        for r in drawings
    )
    return (
        f"Note: stroke analysis suggests this page has {len(drawings)} non-text "
        f"region(s) (likely a sketch, diagram, or drawing) roughly at {spots}. "
        "Transcribe handwritten text normally; for those regions, briefly "
        "describe them in [brackets] instead of guessing at exact wording."
    )
