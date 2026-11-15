#!/usr/bin/env python
"""Parse RIDING CAR G-code modal state and draw a selected layer."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D


HERE = Path(__file__).resolve().parent
CASE = HERE.parent
INPUT = CASE / "input"
DEFAULT_GCODE = INPUT / "studio-cache-192layers.gcode"
DEFAULT_3MF = INPUT / "studio-cache.3mf"
DEFAULT_PREFLIGHT = CASE / "preflight-results.json"
WORD_RE = re.compile(r"([A-Za-z])\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)")
FEATURE_RE = re.compile(r";\s*FEATURE:\s*(.*?)\s*$", re.I)
Z_HEIGHT_RE = re.compile(r";\s*Z_HEIGHT:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))", re.I)

COLORS = {
    "Inner wall": "#2574a9",
    "Outer wall": "#1c9a75",
    "Bridge": "#e4572e",
    "Overhang wall": "#9a4dba",
    "Sparse infill": "#8396a8",
    "Internal solid infill": "#d6a329",
    "Bottom surface": "#5c6bc0",
    "Top surface": "#45a9c6",
    "Gap infill": "#cd7f32",
    "Floating vertical shell": "#eb6ea5",
    "Custom": "#777777",
    "(unlabeled)": "#aaaaaa",
}


@dataclass
class ModalState:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    e: float = 0.0
    xyz_absolute: bool = True
    e_absolute: bool = True
    units_mm: float = 1.0
    feed_mm_min: float | None = None
    layer: int = 0
    z_height: float | None = None
    in_layer_gcode: bool = False
    feature: str = "(unlabeled)"
    feature_block: int = 0
    fan: dict[str, float] = field(default_factory=lambda: {"P0": 0.0})


@dataclass
class Move:
    line: int
    layer: int
    z_height: float | None
    feature: str
    feature_block: int
    command: str
    start: tuple[float, float, float]
    end: tuple[float, float, float]
    points: list[tuple[float, float]]
    e_delta: float
    path_length_mm: float
    xy_length_mm: float
    feed_mm_min: float | None
    fan: dict[str, float]


def parse_words(text: str) -> dict[str, float]:
    return {m.group(1).upper(): float(m.group(2)) for m in WORD_RE.finditer(text)}


def arc_points(start, end, words, clockwise, units):
    if "I" not in words or "J" not in words:
        return [start[:2], end[:2]], math.dist(start, end), False
    cx, cy = start[0] + words["I"] * units, start[1] + words["J"] * units
    radius = math.hypot(start[0] - cx, start[1] - cy)
    end_radius = math.hypot(end[0] - cx, end[1] - cy)
    a0, a1 = math.atan2(start[1] - cy, start[0] - cx), math.atan2(end[1] - cy, end[0] - cx)
    turns = max(0, int(words.get("P", 0)))
    if clockwise:
        sweep = -((a0 - a1) % (2 * math.pi))
    else:
        sweep = (a1 - a0) % (2 * math.pi)
    if abs(sweep) < 1e-9:
        sweep = -2 * math.pi if clockwise else 2 * math.pi
    if turns:
        sweep += (-1 if clockwise else 1) * 2 * math.pi * (turns - 1)
    n = max(2, int(math.ceil(abs(sweep) / math.radians(8))))
    dz = end[2] - start[2]
    pts = [(cx + radius * math.cos(a0 + sweep * i / n),
            cy + radius * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]
    length = math.hypot(radius * abs(sweep), dz)
    return pts, length, abs(radius - end_radius) > 0.02


def parse_gcode(path: Path):
    state = ModalState()
    moves: list[Move] = []
    blocks: list[dict] = []
    counters = Counter()
    feature_blocks = Counter()
    current_block = None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    for line_no, raw in enumerate(lines, 1):
        comment = raw.split(";", 1)[1] if ";" in raw else ""
        if re.match(r"^\s*layer num/total_layer_count:\s*\d+/\d+", comment, re.I):
            state.in_layer_gcode = True
        if raw.strip().upper() == "; CHANGE_LAYER":
            state.layer += 1
            current_block = None
            continue
        zm = Z_HEIGHT_RE.search(raw)
        if zm:
            state.z_height = float(zm.group(1))
        fm = FEATURE_RE.search(raw)
        if fm:
            state.feature = fm.group(1).strip() or "(unlabeled)"
            state.feature_block += 1
            feature_blocks[state.feature] += 1
            current_block = {
                "block": state.feature_block, "layer": state.layer,
                "z_height": state.z_height, "feature": state.feature,
                "start_line": line_no, "end_line": line_no,
                "positive_moves": 0, "positive_xy_path_moves": 0,
                "positive_g1_moves": 0,
                "positive_arc_moves": 0, "positive_e_mm": 0.0,
                "path_length_mm": 0.0, "max_g1_xy_mm": 0.0,
                "max_g1_line": None, "min_feed_mm_min": None,
                "max_feed_mm_min": None, "fan_values": defaultdict(set),
                "extrusion_z_min": math.inf, "extrusion_z_max": -math.inf,
                "bbox": [math.inf, math.inf, -math.inf, -math.inf],
            }
            blocks.append(current_block)

        code = raw.split(";", 1)[0].strip()
        if not code:
            continue
        cmd_match = re.match(r"^([GMT])(\d+)\b", code, re.I)
        if not cmd_match:
            continue
        cmd = (cmd_match.group(1).upper() + cmd_match.group(2))
        words = parse_words(code[len(cmd_match.group(0)):])

        if cmd == "G90":
            state.xyz_absolute = True
            counters["G90"] += 1
            continue
        if cmd == "G91":
            state.xyz_absolute = False
            counters["G91"] += 1
            continue
        if cmd == "M82":
            state.e_absolute = True
            counters["M82"] += 1
            continue
        if cmd == "M83":
            state.e_absolute = False
            counters["M83"] += 1
            continue
        if cmd == "G20":
            state.units_mm = 25.4
            continue
        if cmd == "G21":
            state.units_mm = 1.0
            continue
        if cmd == "G92":
            for axis in "XYZE":
                if axis in words:
                    setattr(state, axis.lower(), words[axis] * state.units_mm)
            counters["G92"] += 1
            continue
        if cmd in ("M106", "M107"):
            fan_key = f"P{int(words.get('P', 0))}"
            state.fan[fan_key] = 0.0 if cmd == "M107" else words.get("S", 255.0)
            counters[cmd] += 1
            continue
        if "F" in words:
            state.feed_mm_min = words["F"] * state.units_mm
        if cmd not in ("G0", "G1", "G2", "G3"):
            continue

        start = (state.x, state.y, state.z)
        end_axes = []
        for axis in "XYZ":
            old = getattr(state, axis.lower())
            val = words.get(axis)
            new = old if val is None else (val * state.units_mm if state.xyz_absolute else old + val * state.units_mm)
            end_axes.append(new)
        end = tuple(end_axes)
        if "E" in words:
            raw_e = words["E"] * state.units_mm
            e_delta = raw_e - state.e if state.e_absolute else raw_e
            state.e = raw_e if state.e_absolute else state.e + raw_e
        else:
            e_delta = 0.0

        if cmd in ("G2", "G3"):
            pts, path_length, bad_radius = arc_points(start, end, words, cmd == "G2", state.units_mm)
            counters["arc_moves"] += 1
            counters["arc_radius_mismatch"] += int(bad_radius)
        else:
            pts = [start[:2], end[:2]]
            xy_length = math.hypot(end[0] - start[0], end[1] - start[1])
            path_length = math.hypot(xy_length, end[2] - start[2])
            counters["linear_moves"] += 1
        xy_length = math.hypot(end[0] - start[0], end[1] - start[1]) if cmd in ("G0", "G1") else sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

        if e_delta > 1e-9:
            counters["positive_extrusion_moves"] += 1
            counters[f"positive_{cmd.lower()}_moves"] += 1
            counters[f"positive_feature::{state.feature}"] += 1
            feature_key = state.feature.casefold()
            if state.in_layer_gcode and ("; FLUSH_START" in raw or "; FLUSH_END" in raw):
                counters["positive_ignored_moves"] += 1
            elif state.in_layer_gcode and (feature_key in {"prime tower", "wipe tower", "custom", "skirt"} or feature_key.endswith(" tower")):
                counters["positive_ignored_moves"] += 1
                counters[f"positive_ignored_feature::{state.feature}"] += 1
            elif state.in_layer_gcode and feature_key == "unknown":
                counters["positive_unknown_feature_moves"] += 1
            elif state.in_layer_gcode and state.feature.startswith("Support"):
                counters["positive_support_moves"] += 1
            elif state.in_layer_gcode and state.feature != "(unlabeled)":
                counters["positive_object_feature_moves"] += 1
            else:
                counters["positive_pre_layer_or_unlabeled_moves"] += 1
        if e_delta < -1e-9:
            counters["negative_extrusion_moves"] += 1
        counters["all_motion_commands"] += 1

        move = Move(line_no, state.layer, state.z_height, state.feature,
                    state.feature_block, cmd, start, end, pts, e_delta,
                    path_length, xy_length, state.feed_mm_min, dict(state.fan))
        moves.append(move)
        if current_block is not None and current_block["block"] == state.feature_block:
            b = current_block
            b["end_line"] = line_no
            if e_delta > 1e-9:
                b["positive_moves"] += 1
                b["positive_xy_path_moves"] += int(xy_length > 1e-9)
                b["positive_g1_moves"] += int(cmd == "G1")
                b["positive_arc_moves"] += int(cmd in ("G2", "G3"))
                b["positive_e_mm"] += e_delta
                b["path_length_mm"] += path_length
                b["extrusion_z_min"] = min(b["extrusion_z_min"], start[2], end[2])
                b["extrusion_z_max"] = max(b["extrusion_z_max"], start[2], end[2])
                if cmd == "G1" and xy_length > b["max_g1_xy_mm"]:
                    b["max_g1_xy_mm"] = xy_length
                    b["max_g1_line"] = {"line": line_no, "start": start[:2], "end": end[:2]}
                for p in pts:
                    b["bbox"][0] = min(b["bbox"][0], p[0])
                    b["bbox"][1] = min(b["bbox"][1], p[1])
                    b["bbox"][2] = max(b["bbox"][2], p[0])
                    b["bbox"][3] = max(b["bbox"][3], p[1])
                if state.feed_mm_min is not None:
                    b["min_feed_mm_min"] = state.feed_mm_min if b["min_feed_mm_min"] is None else min(b["min_feed_mm_min"], state.feed_mm_min)
                    b["max_feed_mm_min"] = state.feed_mm_min if b["max_feed_mm_min"] is None else max(b["max_feed_mm_min"], state.feed_mm_min)
                for key, value in state.fan.items():
                    b["fan_values"][key].add(value)
        state.x, state.y, state.z = end

    return lines, moves, blocks, counters, feature_blocks


def plot_layer_axes(ax, moves, layer, previous_layer=None):
    prior = [m for m in moves if previous_layer is not None and m.layer == previous_layer
             and m.e_delta > 1e-9 and m.xy_length_mm > 1e-9]
    for m in prior:
        ax.plot([p[0] for p in m.points], [p[1] for p in m.points],
                color="#b9bdc2", linewidth=0.42, alpha=0.31, zorder=1)
    current = [m for m in moves if m.layer == layer and m.e_delta > 1e-9 and m.xy_length_mm > 1e-9]
    features = sorted({m.feature for m in current})
    for m in current:
        color = COLORS.get(m.feature, "#777777")
        width = 1.05 if m.feature in ("Bridge", "Overhang wall") else 0.46
        ax.plot([p[0] for p in m.points], [p[1] for p in m.points], color=color,
                linewidth=width, alpha=0.92, zorder=3 if m.feature in ("Bridge", "Overhang wall") else 2)
    z = next((m.z_height for m in current if m.z_height is not None), None)
    ztitle = "" if z is None else f"Z_HEIGHT={z:.2f} mm"
    ax.set_title(f"第{layer}層 {ztitle}", fontsize=10)
    ax.set_xlabel("X [mm]")
    ax.set_ylabel("Y [mm]")
    ax.set_aspect("equal", adjustable="box")
    ax.grid(True, color="#d8d8d8", linewidth=0.45, alpha=0.65)
    ax.margins(0.04)
    return features, prior


def layer_legend(features, has_previous=True):
    handles = []
    if has_previous:
        handles.append(Line2D([0], [0], color="#b9bdc2", lw=1.3, label="前層の押出経路"))
    handles.extend(Line2D([0], [0], color=COLORS.get(f, "#777777"), lw=2, label=f) for f in features)
    return handles


def draw_layer(moves, layer, previous_layer, out_path):
    fig, ax = plt.subplots(figsize=(11, 8), dpi=180)
    features, _ = plot_layer_axes(ax, moves, layer, previous_layer)
    ax.legend(handles=layer_legend(features, previous_layer is not None), loc="upper left", fontsize=8, framealpha=0.93)
    ax.set_title(f"RIDING CAR G-code経路 第{layer}層（CHANGE_LAYERの1始まり番号）\n"
                 f"Z_HEIGHT={next((m.z_height for m in moves if m.layer == layer and m.z_height is not None), float('nan')):.2f} mm")
    plt.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def draw_layer_grid(moves, layers, out_path, title, ncols=3, wheel_zoom=False):
    nrows = math.ceil(len(layers) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(6.0 * ncols, 4.4 * nrows), dpi=180, squeeze=False)
    features = set()
    for index, layer in enumerate(layers):
        ax = axes.flat[index]
        drawn, _ = plot_layer_axes(ax, moves, layer, layer - 1 if layer > 1 else None)
        features.update(drawn)
        if wheel_zoom:
            cx, cy = [(72.28766, 79.22603), (106.76435, 79.22603)][index % 2]
            ax.set_xlim(cx - 6.2, cx + 6.2)
            ax.set_ylim(cy - 6.2, cy + 6.2)
            ax.plot(cx, cy, marker="+", color="#101010", markersize=8, mew=1.7, zorder=5)
            ax.text(cx + 0.25, cy + 0.25, "wheel軸中心", fontsize=7, color="#101010")
        else:
            ax.set_xlim(59, 121)
            ax.set_ylim(72, 108)
    for ax in list(axes.flat)[len(layers):]:
        ax.axis("off")
    fig.suptitle(title, fontsize=14)
    fig.legend(handles=layer_legend(sorted(features)), loc="lower center", ncol=min(5, len(features) + 1),
               fontsize=8, framealpha=0.93, bbox_to_anchor=(0.5, 0.005))
    fig.tight_layout(rect=(0, 0.055, 1, 0.95))
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gcode", type=Path, default=DEFAULT_GCODE)
    ap.add_argument("--archive", type=Path, default=DEFAULT_3MF)
    ap.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    ap.add_argument("--layer", type=int, default=5, help="1-based G-code CHANGE_LAYER number")
    ap.add_argument("--previous-layer", type=int, default=4)
    args = ap.parse_args()

    if not args.gcode.is_file():
        ap.error(f"G-code not found: {args.gcode}")
    if args.layer < 1:
        ap.error("--layer uses 1-based numbering")
    raw = args.gcode.read_bytes()
    lines, moves, blocks, counters, feature_blocks = parse_gcode(args.gcode)
    digest = hashlib.sha256(raw).hexdigest()
    embedded = []
    with zipfile.ZipFile(args.archive) as zf:
        for name in zf.namelist():
            if name.lower().endswith(".gcode"):
                blob = zf.read(name)
                embedded.append({"entry": name, "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(), "byte_match": blob == raw})

    for b in blocks:
        b["bbox"] = None if not math.isfinite(b["bbox"][0]) else b["bbox"]
        b["fan_values"] = {k: sorted(v) for k, v in b["fan_values"].items()}
    grouped = defaultdict(lambda: {"feature_blocks": 0, "positive_moves": 0, "positive_xy_path_moves": 0, "positive_e_only_moves": 0, "positive_g1_moves": 0, "positive_arc_moves": 0, "positive_e_mm": 0.0, "path_length_mm": 0.0, "max_g1_xy_mm": 0.0, "max_g1_line": None, "extrusion_z_min": math.inf, "extrusion_z_max": -math.inf})
    for b in blocks:
        key = (b["layer"], b["z_height"], b["feature"])
        g = grouped[key]
        g["feature_blocks"] += 1
    for m in moves:
        if m.e_delta <= 1e-9:
            continue
        key = (m.layer, m.z_height, m.feature)
        g = grouped[key]
        g["positive_moves"] += 1
        g["positive_xy_path_moves"] += int(m.xy_length_mm > 1e-9)
        g["positive_e_only_moves"] += int(m.xy_length_mm <= 1e-9 and abs(m.end[2] - m.start[2]) <= 1e-9)
        g["positive_g1_moves"] += int(m.command == "G1")
        g["positive_arc_moves"] += int(m.command in ("G2", "G3"))
        g["positive_e_mm"] += m.e_delta
        g["path_length_mm"] += m.path_length_mm
        g["extrusion_z_min"] = min(g["extrusion_z_min"], m.start[2], m.end[2])
        g["extrusion_z_max"] = max(g["extrusion_z_max"], m.start[2], m.end[2])
        if m.command == "G1" and m.xy_length_mm > g["max_g1_xy_mm"]:
            g["max_g1_xy_mm"] = m.xy_length_mm
            g["max_g1_line"] = {"line": m.line, "start": m.start[:2], "end": m.end[:2]}
    preflight = json.loads(args.preflight.read_text(encoding="utf-8")) if args.preflight.is_file() else {}
    expected = preflight.get("studio_cache", {})
    metrics = {
        "input": {"path": str(args.gcode), "bytes": len(raw), "sha256": digest,
                  "archive": str(args.archive), "embedded_gcode": embedded},
        "parser": {"line_count": len(lines), "change_layer_count": sum(1 for x in lines if x.strip() == "; CHANGE_LAYER"),
                   "modal_commands": {k: counters[k] for k in ("G90", "G91", "M82", "M83", "G92")},
                   "motion_commands": {k: counters[k] for k in ("all_motion_commands", "linear_moves", "arc_moves", "arc_radius_mismatch")},
                   "positive_extrusion_moves": counters["positive_extrusion_moves"],
                   "positive_feature_moves": counters["positive_object_feature_moves"],
                   "positive_pre_layer_or_unlabeled_moves": counters["positive_pre_layer_or_unlabeled_moves"],
                   "positive_preflight_ignored_moves": counters["positive_ignored_moves"],
                   "positive_unknown_feature_moves": counters["positive_unknown_feature_moves"],
                   "positive_support_moves": counters["positive_support_moves"],
                   "negative_extrusion_moves": counters["negative_extrusion_moves"],
                   "feature_block_counts": dict(feature_blocks)},
        "preflight_comparison": {"expected_object_feature_moves": expected.get("positive_object_feature_extrusion_moves"),
                                 "parsed_object_feature_moves": counters["positive_object_feature_moves"],
                                 "expected_support_moves": expected.get("positive_support_extrusion_moves"),
                                 "parsed_support_moves": counters["positive_support_moves"],
                                 "expected_interface_moves": expected.get("positive_support_interface_extrusion_moves"),
                                 "embedded_byte_match": bool(embedded) and all(x["byte_match"] for x in embedded)},
        "feature_by_layer": [{"layer": k[0], "z_height": k[1], "feature": k[2],
                              **{kk: (None if not math.isfinite(vv) else vv) if kk.startswith("extrusion_z_") else vv for kk, vv in v.items()}}
                             for k, v in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][2]))],
        "feature_blocks": blocks,
        "notes": ["層番号は CHANGE_LAYER の1始まり。Z_HEIGHTはG-codeコメント値。",
                  "押出移動数はE増分が正の移動コマンド数。G1直線とG2/G3円弧を分ける。",
                  "最大G1距離はコマンド全体の線分長。支持足場と空中区間を分割した未支持spanではない。",
                  "図の前層経路はXY位置の比較用。断面の実材料幅や実物の形成を証明しない。"],
    }
    (HERE / "gcode-measurements.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    draw_layer(moves, args.layer, args.previous_layer,
               HERE / f"toolpath-layer-{args.layer:03d}.png")
    print(json.dumps({"measurements": str(HERE / "gcode-measurements.json"),
                      "figure": str(HERE / f"toolpath-layer-{args.layer:03d}.png"),
                      "sha256": digest,
                      "parsed_object_feature_moves": counters["positive_object_feature_moves"],
                      "parsed_support_moves": counters["positive_support_moves"],
                      "arc_radius_mismatch": counters["arc_radius_mismatch"]}, ensure_ascii=False))


if __name__ == "__main__":
    plt.rcParams["font.family"] = "Meiryo"
    main()
