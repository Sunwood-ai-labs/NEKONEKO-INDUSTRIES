"""Find nominal model-bottom contacts with non-dedicated support paths.

This is a G-code path check, not a deposited-bead or physical-removal test.
It intentionally fails closed when an eligible positive-extrusion path cannot
be interpreted using the supported modal G-code subset.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable


_WORD = re.compile(r"(?<![A-Za-z])([A-Za-z])\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
_MOTION = re.compile(r"^G0*([0-3])(?:\s|[A-Za-z]|$)", re.IGNORECASE)
_TOOL = re.compile(r"^T\s*(\d+)\s*$", re.IGNORECASE)
_M620 = re.compile(r"^M620\s+S(\d+)(A?)(?:\s|$)", re.IGNORECASE)
_M621 = re.compile(r"^M621\s+S(\d+)(A?)(?:\s|$)", re.IGNORECASE)
_LAYER_START = re.compile(r"^;\s*layer num/total_layer_count:\s*\d+/\d+", re.IGNORECASE)
_FEATURE = re.compile(r"^Support(?:\b|\s)", re.IGNORECASE)
_LAYER_HEIGHT = re.compile(r"^;\s*LAYER_HEIGHT:\s*(\S+)", re.IGNORECASE)
_LINE_WIDTH = re.compile(r"^;\s*LINE_WIDTH:\s*(\S+)", re.IGNORECASE)
_CELL_MM = 2.0
_SAMPLE_MM = 0.1
_Z_TOLERANCE_MM = 0.02
_MAX_CONTACTS = 20


@dataclass(frozen=True)
class _ExtrusionSegment:
    line: int
    feature: str
    tool: int
    start_x: float
    start_y: float
    end_x: float
    end_y: float
    z: float
    height: float
    width: float
    arc_center_x: float | None = None
    arc_center_y: float | None = None
    arc_sweep: float = 0.0
    arc_error: float = 0.0


@dataclass(frozen=True)
class _Piece:
    line: int
    tool: int
    feature: str
    z: float
    width: float
    arc_error: float
    ax: float
    ay: float
    bx: float
    by: float


def _is_excluded_feature(feature: str) -> bool:
    key = feature.strip().casefold()
    return (
        key in {"prime tower", "wipe tower", "custom", "skirt"}
        or key.endswith(" tower")
    )


def _xy_points(segment: _ExtrusionSegment) -> list[tuple[float, float]]:
    """Return a centerline polyline whose chords are at most 0.1 mm long."""
    if segment.arc_center_x is None or segment.arc_center_y is None:
        dx = segment.end_x - segment.start_x
        dy = segment.end_y - segment.start_y
        distance = math.hypot(dx, dy)
        steps = max(1, math.ceil(distance / _SAMPLE_MM))
        return [
            (
                segment.start_x + dx * index / steps,
                segment.start_y + dy * index / steps,
            )
            for index in range(steps + 1)
        ]

    cx, cy = segment.arc_center_x, segment.arc_center_y
    radius = math.hypot(segment.start_x - cx, segment.start_y - cy)
    steps = max(1, math.ceil(radius * abs(segment.arc_sweep) / _SAMPLE_MM))
    start_angle = math.atan2(segment.start_y - cy, segment.start_x - cx)
    points = [
        (
            cx + radius * math.cos(start_angle + segment.arc_sweep * index / steps),
            cy + radius * math.sin(start_angle + segment.arc_sweep * index / steps),
        )
        for index in range(steps + 1)
    ]
    points[0] = (segment.start_x, segment.start_y)
    points[-1] = (segment.end_x, segment.end_y)
    return points


def _point_segment_distance(px: float, py: float, piece: _Piece) -> float:
    dx, dy = piece.bx - piece.ax, piece.by - piece.ay
    length2 = dx * dx + dy * dy
    if length2 <= 1e-24:
        return math.hypot(px - piece.ax, py - piece.ay)
    t = max(0.0, min(1.0, ((px - piece.ax) * dx + (py - piece.ay) * dy) / length2))
    return math.hypot(px - (piece.ax + t * dx), py - (piece.ay + t * dy))


def _segment_distance(ax: float, ay: float, bx: float, by: float, other: _Piece) -> float:
    """Exact 2D centerline distance between two straight sample chords."""
    ux, uy = bx - ax, by - ay
    vx, vy = other.bx - other.ax, other.by - other.ay
    cross = ux * vy - uy * vx
    if abs(cross) > 1e-12:
        wx, wy = other.ax - ax, other.ay - ay
        t = (wx * vy - wy * vx) / cross
        u = (wx * uy - wy * ux) / cross
        if -1e-12 <= t <= 1 + 1e-12 and -1e-12 <= u <= 1 + 1e-12:
            return 0.0
    return min(
        _point_segment_distance(ax, ay, other),
        _point_segment_distance(bx, by, other),
        _point_segment_distance(other.ax, other.ay, _Piece(0, 0, "", 0.0, 0.0, 0.0, ax, ay, bx, by)),
        _point_segment_distance(other.bx, other.by, _Piece(0, 0, "", 0.0, 0.0, 0.0, ax, ay, bx, by)),
    )


def _arc_sweep(
    motion: int,
    start: tuple[float, float],
    end: tuple[float, float],
    center: tuple[float, float],
) -> float:
    start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
    end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
    if motion == 3:
        sweep = (end_angle - start_angle) % (2 * math.pi)
    else:
        sweep = -((start_angle - end_angle) % (2 * math.pi))
    if math.hypot(end[0] - start[0], end[1] - start[1]) < 1e-9:
        return 2 * math.pi if motion == 3 else -2 * math.pi
    return sweep


def audit_contacts(
    gcode_bytes: bytes,
    dedicated_tools: set[int],
    default_width: float | None = None,
) -> dict:
    """Audit model bottom paths against nearby ordinary support paths.

    `dedicated_tools` contains logical slicer tool ids whose support paths are
    intentional separation material and are excluded from contact findings.
    The returned evidence describes nominal centerline overlap only.
    """
    issues: list[dict[str, str]] = []
    issue_messages: set[str] = set()

    def issue(message: str) -> None:
        if message not in issue_messages:
            issue_messages.add(message)
            issues.append({"code": "NNI-TR-005", "message": message})

    if default_width is not None and (not math.isfinite(default_width) or default_width <= 0):
        issue("default_width must be a finite positive millimeter value when provided.")
        default_width = None

    text = gcode_bytes.decode("utf-8", "replace")
    line_offset = 0
    if "; HEADER_BLOCK_END" in text:
        header, _, text = text.partition("; HEADER_BLOCK_END")
        line_offset = header.count("\n")

    feature = "Unknown"
    layer_started = False
    in_flush = False
    relative_xyz = False
    relative_e: bool | None = None
    e_position = 0.0
    x_position: float | None = None
    y_position: float | None = None
    z_position: float | None = None
    x_offset = 0.0
    y_offset = 0.0
    z_offset = 0.0
    height: float | None = None
    width: float | None = None
    active_tool: int | None = None
    active_raw_tool: int | None = None
    pending_tool: int | None = None
    pending_seen: int | None = None
    pending_valid = False
    plane = "G17"
    absolute_ij = False
    inches_seen = False
    model_segments: list[_ExtrusionSegment] = []
    support_segments: list[_ExtrusionSegment] = []
    flush_current = False

    def current_is_candidate() -> bool:
        return layer_started and not flush_current and not _is_excluded_feature(feature)

    for relative_line_number, raw_line in enumerate(text.splitlines(), 1):
        line_number = line_offset + relative_line_number
        if _LAYER_START.match(raw_line):
            layer_started = True
        if raw_line.startswith("; FEATURE:"):
            feature = raw_line.split(":", 1)[1].strip()
        flush_current = in_flush
        if "; FLUSH_START" in raw_line:
            flush_current = True
            in_flush = True
        if "; FLUSH_END" in raw_line:
            flush_current = True
            in_flush = False
        height_match = _LAYER_HEIGHT.match(raw_line)
        if height_match:
            try:
                parsed = float(height_match.group(1))
                height = parsed if math.isfinite(parsed) and parsed > 0 else None
            except ValueError:
                height = None
        width_match = _LINE_WIDTH.match(raw_line)
        if width_match:
            try:
                parsed = float(width_match.group(1))
                width = parsed if math.isfinite(parsed) and parsed > 0 else None
            except ValueError:
                width = None

        command = raw_line.split(";", 1)[0].strip()
        if not command:
            continue

        selection = _M620.fullmatch(command)
        if selection:
            selected = int(selection.group(1))
            if pending_tool is not None:
                issue("A new M620 tool selection began before the previous M621 commit.")
            pending_tool = selected
            pending_seen = None
            pending_valid = bool(selection.group(2)) or selected == 255
            if not pending_valid:
                issue(f"M620 S{selected} selection is missing its AMS A marker.")
            continue
        commit = _M621.fullmatch(command)
        if commit:
            committed = int(commit.group(1))
            if (
                pending_tool == committed
                and pending_seen == committed
                and pending_valid
                and (bool(commit.group(2)) or committed == 255)
            ):
                active_tool = None if committed == 255 else committed
            else:
                active_tool = None
                issue(f"M621 S{committed} did not match a completed M620/T selection.")
            pending_tool = None
            pending_seen = None
            pending_valid = False
            continue
        tool_match = _TOOL.fullmatch(command)
        if tool_match:
            active_raw_tool = int(tool_match.group(1))
            if active_raw_tool == 1000:
                continue
            selected = active_raw_tool
            if pending_tool is None:
                active_tool = None
                issue(f"T{selected} appeared without a pending M620 S<n>A selection.")
            elif selected != pending_tool:
                active_tool = None
                pending_seen = None
                issue(f"M620 selected S{pending_tool}A but G-code selected T{selected}.")
            else:
                pending_seen = selected
            continue

        upper = command.upper()
        if re.match(r"^G0*28(?:\s|[A-Za-z]|$)", command, re.IGNORECASE):
            # Homing invalidates the previous path position. An absolute XYZ
            # travel must establish it again before a model/support extrusion.
            x_position = y_position = z_position = None
            x_offset = y_offset = z_offset = 0.0
            continue
        if upper == "G90.1":
            absolute_ij = True
            continue
        if upper == "G91.1":
            absolute_ij = False
            continue
        if upper == "G90":
            relative_xyz = False
            continue
        if upper == "G91":
            relative_xyz = True
            continue
        if upper == "M82":
            relative_e = False
            continue
        if upper == "M83":
            relative_e = True
            continue
        if upper == "G17":
            plane = "G17"
            continue
        if upper == "G18":
            plane = "G18"
            continue
        if upper == "G19":
            plane = "G19"
            continue
        if upper == "G20":
            inches_seen = True
            continue
        if upper == "G21":
            inches_seen = False
            continue

        words = {letter.upper(): float(value) for letter, value in _WORD.findall(command)}
        if re.match(r"^G0*92(?:\s|[A-Za-z]|$)", command, re.IGNORECASE):
            if "X" in words:
                if layer_started:
                    issue(f"Line {line_number}: G92 X reset after layer start is unsupported.")
                prior = 0.0 if x_position is None else x_position
                x_offset = prior - words["X"]
                x_position = prior
            if "Y" in words:
                if layer_started:
                    issue(f"Line {line_number}: G92 Y reset after layer start is unsupported.")
                prior = 0.0 if y_position is None else y_position
                y_offset = prior - words["Y"]
                y_position = prior
            if "Z" in words:
                if layer_started:
                    issue(f"Line {line_number}: G92 Z reset after layer start is unsupported.")
                prior = 0.0 if z_position is None else z_position
                z_offset = prior - words["Z"]
                z_position = prior
            if "E" in words:
                e_position = words["E"]
            continue

        motion_match = _MOTION.match(command)
        if not motion_match:
            continue
        motion = int(motion_match.group(1))
        start_x, start_y, start_z = x_position, y_position, z_position
        for axis in ("X", "Y", "Z"):
            if axis not in words:
                continue
            value = words[axis]
            if relative_xyz:
                old = {"X": x_position, "Y": y_position, "Z": z_position}[axis]
                if old is None:
                    if current_is_candidate() and "E" in words:
                        issue(f"Line {line_number}: relative {axis} position is unknown.")
                    continue
                value += old
            elif axis == "X":
                value += x_offset
            elif axis == "Y":
                value += y_offset
            else:
                value += z_offset
            if axis == "X":
                x_position = value
            elif axis == "Y":
                y_position = value
            else:
                z_position = value

        extrusion_delta: float | None = None
        path_has_xy = (
            start_x is not None and start_y is not None and x_position is not None and y_position is not None
            and ("X" in words or "Y" in words)
            and math.hypot(x_position - start_x, y_position - start_y) > 1e-9
        )
        if "E" in words:
            if relative_e is None:
                if current_is_candidate() and path_has_xy:
                    issue(f"Line {line_number}: E mode is unknown (neither M82 nor M83 was seen).")
            elif relative_e:
                extrusion_delta = words["E"]
            else:
                extrusion_delta = words["E"] - e_position
                e_position = words["E"]
        if extrusion_delta is None or extrusion_delta <= 1e-6:
            continue

        if not layer_started or flush_current or _is_excluded_feature(feature):
            continue
        if motion in (0, 1) and ("X" in words or "Y" in words) and (
            start_x is None or start_y is None or x_position is None or y_position is None
        ):
            issue(f"Line {line_number}: positive extrusion has an unknown XY segment start or end.")
            continue
        if motion in (0, 1) and not path_has_xy:
            if "Z" in words and start_z is not None and z_position is not None and abs(z_position - start_z) > 1e-6:
                issue(f"Line {line_number}: positive extrusion changes Z without an XY path.")
            continue  # E-only unretract/prime moves and zero-length XY moves have no path to compare.
        if feature.strip().casefold() == "unknown":
            issue(f"Line {line_number}: positive extrusion has no classified feature.")
            continue
        if inches_seen:
            issue(f"Line {line_number}: positive extrusion in G20 inch mode is unsupported.")
            continue
        if plane != "G17":
            issue(f"Line {line_number}: positive extrusion while {plane} is active is unsupported.")
            continue
        if pending_tool is not None or active_tool is None:
            issue(f"Line {line_number}: positive extrusion has no completed logical material tool.")
            continue
        if _FEATURE.match(feature):
            is_support = True
        else:
            is_support = False
            if active_tool in dedicated_tools:
                issue(f"Line {line_number}: dedicated support tool T{active_tool} has model-feature extrusion.")
                continue

        if start_x is None or start_y is None or x_position is None or y_position is None or z_position is None:
            issue(f"Line {line_number}: positive extrusion has an unknown XYZ path position.")
            continue
        if start_z is None or abs(z_position - start_z) > 0.02:
            issue(f"Line {line_number}: positive extrusion changes Z by more than 0.02 mm.")
            continue
        if height is None:
            issue(f"Line {line_number}: positive extrusion has no valid current LAYER_HEIGHT.")
            continue
        effective_width = width if width is not None else default_width
        if effective_width is None or not math.isfinite(effective_width) or effective_width <= 0:
            issue(f"Line {line_number}: positive extrusion has no valid current LINE_WIDTH or default_width.")
            continue

        center_x: float | None = None
        center_y: float | None = None
        sweep = 0.0
        if motion in (2, 3):
            if absolute_ij:
                issue(f"Line {line_number}: absolute-IJ G90.1 arc centers are unsupported.")
                continue
            if "R" in words:
                issue(f"Line {line_number}: R-defined G{motion} arc is unsupported; IJ offsets are required.")
                continue
            if "I" not in words and "J" not in words:
                issue(f"Line {line_number}: G{motion} arc has no relative I/J center offset.")
                continue
            if words.get("P", 1.0) > 1:
                issue(f"Line {line_number}: multi-turn G{motion} arc is unsupported.")
                continue
            center_x = start_x + words.get("I", 0.0)
            center_y = start_y + words.get("J", 0.0)
            radius = math.hypot(start_x - center_x, start_y - center_y)
            end_radius = math.hypot(x_position - center_x, y_position - center_y)
            if radius <= 1e-9 or abs(radius - end_radius) > 0.02:
                issue(f"Line {line_number}: G{motion} IJ arc radius is invalid or inconsistent.")
                continue
            sweep = _arc_sweep(motion, (start_x, start_y), (x_position, y_position), (center_x, center_y))
            arc_error = radius * (1 - math.cos(abs(sweep) / max(1, math.ceil(radius * abs(sweep) / _SAMPLE_MM)) / 2))
        else:
            arc_error = 0.0

        segment = _ExtrusionSegment(
            line=line_number,
            feature=feature,
            tool=active_tool,
            start_x=start_x,
            start_y=start_y,
            end_x=x_position,
            end_y=y_position,
            z=z_position,
            height=height,
            width=effective_width,
            arc_center_x=center_x,
            arc_center_y=center_y,
            arc_sweep=sweep,
            arc_error=arc_error,
        )
        (support_segments if is_support else model_segments).append(segment)

    if pending_tool is not None and (model_segments or support_segments):
        issue(f"M620 S{pending_tool} remained uncommitted while eligible paths were being analyzed.")

    # Bucket normal-material support chords by centi-millimeter Z and 2 mm XY cells.
    support_index: dict[tuple[int, int, int], list[_Piece]] = {}
    max_support_width = 0.0
    max_support_arc_error = 0.0
    for segment in support_segments:
        if segment.tool in dedicated_tools:
            continue
        max_support_width = max(max_support_width, segment.width)
        max_support_arc_error = max(max_support_arc_error, segment.arc_error)
        points = _xy_points(segment)
        z_key = round(segment.z * 100)
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            piece = _Piece(segment.line, segment.tool, segment.feature, segment.z, segment.width, segment.arc_error, ax, ay, bx, by)
            cell = (z_key, math.floor(((ax + bx) / 2) / _CELL_MM), math.floor(((ay + by) / 2) / _CELL_MM))
            support_index.setdefault(cell, []).append(piece)

    contact_by_pair: dict[tuple[int, int], dict] = {}
    checked_model_samples = 0
    for segment in model_segments:
        bottom_z = segment.z - segment.height
        starts_on_bed = abs(bottom_z) <= 0.001
        points = _xy_points(segment)
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            checked_model_samples += 1
            if starts_on_bed:
                continue  # Direct bed starts are legitimate; there is no lower support face.
            mid_x, mid_y = (ax + bx) / 2, (ay + by) / 2
            nominal_limit = (segment.width + max_support_width) / 2
            # A qualifying pair's chord midpoints are at most half of each
            # <=0.1 mm chord apart from the closest points, plus arc error.
            # Iterate grid cells whose indexed midpoints can fit inside that bound.
            cell_radius = nominal_limit + _SAMPLE_MM + segment.arc_error + max_support_arc_error
            x0 = math.floor((mid_x - cell_radius) / _CELL_MM)
            x1 = math.floor((mid_x + cell_radius) / _CELL_MM)
            y0 = math.floor((mid_y - cell_radius) / _CELL_MM)
            y1 = math.floor((mid_y + cell_radius) / _CELL_MM)
            z_base = round(bottom_z * 100)
            closest: tuple[float, _Piece] | None = None
            for z_key in range(z_base - 3, z_base + 4):
                for cell_x in range(x0, x1 + 1):
                    for cell_y in range(y0, y1 + 1):
                        for support in support_index.get((z_key, cell_x, cell_y), ()):
                            if abs(support.z - bottom_z) > _Z_TOLERANCE_MM + 1e-9:
                                continue
                            nominal_limit = (segment.width + support.width) / 2
                            arc_margin = segment.arc_error + support.arc_error
                            limit = nominal_limit + arc_margin
                            distance = _segment_distance(ax, ay, bx, by, support)
                            if distance <= limit + 1e-9 and (closest is None or distance < closest[0]):
                                closest = (distance, support)
            if closest is None:
                continue
            distance, support = closest
            key = (segment.line, support.line)
            record = {
                "model_line": segment.line,
                "support_line": support.line,
                "model_tool": segment.tool,
                "support_tool": support.tool,
                "model_z": round(segment.z, 6),
                "support_z": round(support.z, 6),
                "xy_distance_mm": round(distance, 6),
                "xy_nominal_limit_mm": round((segment.width + support.width) / 2, 6),
                "arc_approximation_margin_mm": round(segment.arc_error + support.arc_error, 6),
            }
            previous = contact_by_pair.get(key)
            if previous is None or distance < previous["xy_distance_mm"]:
                contact_by_pair[key] = {**record, "xy_distance_mm": distance}

    ordered_contacts = sorted(contact_by_pair.values(), key=lambda item: (item["model_line"], item["support_line"]))
    public_contacts = [
        {**item, "xy_distance_mm": round(item["xy_distance_mm"], 6)}
        for item in ordered_contacts[:_MAX_CONTACTS]
    ]
    if contact_by_pair:
        issue(
            f"Ordinary support centerlines nominally overlap model-bottom paths at "
            f"{len(contact_by_pair)} distinct model/support G-code line pair(s)."
        )

    return {
        "status": "BLOCK" if issues else "PASS_FILE_ONLY",
        "checked_model_samples": checked_model_samples,
        "total_contacts": len(contact_by_pair),
        "contacts_truncated": len(contact_by_pair) > _MAX_CONTACTS,
        "contacts": public_contacts,
        "issues": issues,
        "scope": [
            "Scans all eligible positive-extrusion model and support XY paths; startup, Prime tower, Custom, skirt, FLUSH, and tower features are excluded.",
            "A contact means nominal centerline distance is within half of each LINE_WIDTH and support Z is within 0.02 mm of model bottom (model Z minus LAYER_HEIGHT).",
            "G2/G3 IJ arcs are sampled into chords no longer than 0.1 mm; model and support sample chords are compared by 2D segment distance with a conservative chord-deviation margin. Nominal widths and that margin can overstate near-threshold contact.",
            "Dedicated support tools are excluded from contact findings. Bed-start model paths at Z=0 are exempt.",
            "This file-only path check does not prove complete geometric coverage, physical adhesion or separation, support accessibility, print quality, strength, or successful removal.",
        ],
    }
