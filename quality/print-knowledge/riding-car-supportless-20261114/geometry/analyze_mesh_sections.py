from __future__ import annotations

import hashlib
import json
import posixpath
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

LIBS = Path(r"C:/Users/makim/AppData/Local/Temp/nekomo-analysis-libs")
if LIBS.exists():
    sys.path.append(str(LIBS))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Polygon as MplPolygon
from shapely.geometry import LineString, Polygon
from shapely.ops import nearest_points
import trimesh


HERE = Path(__file__).resolve().parent
JOB = HERE.parent
INPUT = JOB / "input" / "Car_Toy_Model_Interior_Bam_60mm.3mf"
CACHE = JOB / "input" / "studio-cache.3mf"
MODEL_PATH = "3D/3dmodel.model"
CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
NS = {"m": CORE, "p": PROD}
MEIRYO = Path(r"C:/Windows/Fonts/meiryo.ttc")
PART_COLORS = {"Body1": "#22577a", "Body14": "#ed7d3a", "Body3": "#d1495b"}


@dataclass
class MeshInstance:
    name: str
    model_path: str
    object_id: str
    transform: np.ndarray
    raw_vertices: np.ndarray
    faces: np.ndarray
    mesh: trimesh.Trimesh


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def parse_transform(value: str | None) -> np.ndarray:
    """Read a 3MF row-vector transform: 3x3 linear part then row translation."""
    if not value:
        return np.eye(4, dtype=float)
    values = np.asarray([float(part) for part in value.split()], dtype=float)
    if values.size != 12:
        raise ValueError(f"Expected 12 3MF matrix values, got {values.size}")
    matrix = np.eye(4, dtype=float)
    matrix[:3, :3] = values[:9].reshape(3, 3)
    matrix[3, :3] = values[9:12]
    return matrix


def transform_points(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return points @ matrix[:3, :3] + matrix[3, :3]


def get_object(root: ET.Element, object_id: str) -> ET.Element:
    for obj in root.findall("m:resources/m:object", NS):
        if obj.get("id") == object_id:
            return obj
    raise KeyError(f"Missing 3MF object {object_id}")


def package_model(archive: zipfile.ZipFile, path: str) -> ET.Element:
    root = ET.fromstring(archive.read(path))
    if root.get("unit", "millimeter") != "millimeter":
        raise ValueError(f"Only millimeter model units are supported: {path}={root.get('unit')}")
    return root


def part_labels(archive: zipfile.ZipFile, build_object_id: str) -> dict[str, str]:
    """Read labels only; Metadata/model_settings.config matrices are ignored."""
    try:
        root = ET.fromstring(archive.read("Metadata/model_settings.config"))
    except KeyError:
        return {}
    obj = next((x for x in root.findall("object") if x.get("id") == build_object_id), None)
    if obj is None:
        return {}
    labels: dict[str, str] = {}
    for index, part in enumerate(obj.findall("part"), start=1):
        for metadata in part.findall("metadata"):
            if metadata.get("key") == "name" and metadata.get("value"):
                labels[str(index)] = metadata.get("value", "")
    return labels


def load_instances(path: Path) -> list[MeshInstance]:
    with zipfile.ZipFile(path) as archive:
        root = package_model(archive, MODEL_PATH)
        labels_for_build: dict[str, dict[str, str]] = {}
        instances: list[MeshInstance] = []
        visiting: set[tuple[str, str]] = set()

        def walk(model_path: str, object_id: str, to_plate: np.ndarray, label: str | None = None) -> None:
            key = (model_path, object_id)
            if key in visiting:
                raise ValueError(f"Cyclic 3MF component reference: {key}")
            model = root if model_path == MODEL_PATH else package_model(archive, model_path)
            obj = get_object(model, object_id)
            mesh_node = obj.find("m:mesh", NS)
            if mesh_node is not None:
                vertex_nodes = mesh_node.find("m:vertices", NS).findall("m:vertex", NS)
                triangle_nodes = mesh_node.find("m:triangles", NS).findall("m:triangle", NS)
                raw_vertices = np.asarray(
                    [[float(v.get(axis)) for axis in ("x", "y", "z")] for v in vertex_nodes],
                    dtype=float,
                )
                faces = np.asarray(
                    [[int(t.get(axis)) for axis in ("v1", "v2", "v3")] for t in triangle_nodes],
                    dtype=np.int64,
                )
                placed = transform_points(raw_vertices, to_plate)
                mesh = trimesh.Trimesh(vertices=placed, faces=faces, process=True)
                instances.append(MeshInstance(
                    name=label or f"object_{object_id}",
                    model_path=model_path,
                    object_id=object_id,
                    transform=to_plate.copy(),
                    raw_vertices=raw_vertices,
                    faces=faces,
                    mesh=mesh,
                ))
                return

            components = obj.findall("m:components/m:component", NS)
            if not components:
                raise ValueError(f"Object {object_id} has neither mesh nor components")
            visiting.add(key)
            if model_path == MODEL_PATH and object_id not in labels_for_build:
                labels_for_build[object_id] = part_labels(archive, object_id)
            labels = labels_for_build.get(object_id, {}) if model_path == MODEL_PATH else {}
            for index, component in enumerate(components, start=1):
                child_path = component.get(f"{{{PROD}}}path") or model_path
                child_path = posixpath.normpath(child_path.lstrip("/"))
                child_matrix = parse_transform(component.get("transform"))
                child_label = label or labels.get(str(index))
                walk(child_path, component.get("objectid"), child_matrix @ to_plate, child_label)
            visiting.remove(key)

        for item in root.findall("m:build/m:item", NS):
            walk(MODEL_PATH, item.get("objectid"), parse_transform(item.get("transform")))
        return instances


def bounds(mesh: trimesh.Trimesh) -> dict[str, list[float]]:
    return {
        "min_mm": [float(x) for x in mesh.bounds[0]],
        "max_mm": [float(x) for x in mesh.bounds[1]],
        "size_mm": [float(x) for x in mesh.extents],
    }


def section_lines(mesh: trimesh.Trimesh, z: float) -> list[np.ndarray]:
    section = mesh.section(plane_origin=[0.0, 0.0, z], plane_normal=[0.0, 0.0, 1.0])
    if section is None:
        return []
    return [line[:, :2] for line in section.discrete if len(line) > 1]


def contour_polygons(lines: list[np.ndarray]) -> list[tuple[Polygon, int]]:
    polygons: list[Polygon] = []
    for line in lines:
        if len(line) < 4 or np.linalg.norm(line[0] - line[-1]) > 1e-5:
            continue
        polygon = Polygon(line)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.geom_type == "Polygon" and polygon.area > 1e-8:
            polygons.append(polygon)
        elif polygon.geom_type == "MultiPolygon":
            polygons.extend(x for x in polygon.geoms if x.area > 1e-8)
    result: list[tuple[Polygon, int]] = []
    for polygon in polygons:
        point = polygon.representative_point()
        containers = [
            other for other in polygons
            if other.area > polygon.area + 1e-8 and other.contains(point)
        ]
        depth = 0
        parent = min(containers, key=lambda x: x.area) if containers else None
        while parent is not None:
            depth += 1
            parent_point = parent.representative_point()
            parents = [
                other for other in polygons
                if other.area > parent.area + 1e-8 and other.contains(parent_point)
            ]
            parent = min(parents, key=lambda x: x.area) if parents else None
        result.append((polygon, depth))
    return result


def fill_material(ax, lines: list[np.ndarray], color: str, alpha: float = 0.25) -> None:
    """Use nested closed section loops as material/void boundaries."""
    nested = contour_polygons(lines)
    for polygon, depth in sorted(nested, key=lambda x: x[0].area, reverse=True):
        is_material = depth % 2 == 0
        ax.add_patch(MplPolygon(
            np.asarray(polygon.exterior.coords),
            closed=True,
            facecolor=color if is_material else "white",
            edgecolor="none",
            alpha=alpha if is_material else 1.0,
            zorder=1 if is_material else 2,
        ))


def draw_center_section(instances: list[MeshInstance], output: Path, z: float) -> dict[str, int]:
    fig, ax = plt.subplots(figsize=(12, 7), constrained_layout=True)
    counts: dict[str, int] = {}
    for instance in instances:
        lines = section_lines(instance.mesh, z)
        counts[instance.name] = len(lines)
        color = PART_COLORS.get(instance.name, "#444444")
        fill_material(ax, lines, color)
        for line in lines:
            ax.plot(line[:, 0], line[:, 1], color=color, linewidth=1.1, alpha=0.98, zorder=3)
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("X [mm]（車両長手）")
    ax.set_ylabel("Y [mm]（車高）")
    ax.set_title(f"RIDING CAR mesh断面　固定 Z = {z:.3f} mm")
    ax.grid(True, linewidth=0.35, alpha=0.35)
    font = FontProperties(fname=str(MEIRYO)) if MEIRYO.exists() else None
    note_font = {"fontproperties": font} if font else {}
    ax.annotate(
        "+Z 造形方向＝紙面手前 ⊙",
        xy=(0.98, 0.95), xycoords="axes fraction", ha="right", va="top", fontsize=11,
        **note_font,
    )
    ax.annotate(
        "断面平面: XY　　空白: meshの空洞",
        xy=(0.98, 0.89), xycoords="axes fraction", ha="right", va="top", fontsize=9,
        **note_font,
    )
    handles = [
        plt.Line2D([], [], color=PART_COLORS.get(x.name, "#444444"), label=x.name)
        for x in instances
    ]
    ax.legend(handles=handles, loc="lower right", framealpha=0.9)
    if font:
        for label in ax.get_xticklabels() + ax.get_yticklabels():
            label.set_fontproperties(font)
        ax.xaxis.label.set_fontproperties(font)
        ax.yaxis.label.set_fontproperties(font)
        ax.title.set_fontproperties(font)
    fig.savefig(output, dpi=180)
    plt.close(fig)
    return counts


def topology(mesh: trimesh.Trimesh) -> dict[str, object]:
    welded = mesh.copy()
    welded.merge_vertices()
    components = welded.split(only_watertight=False)
    return {
        "vertices_after_weld": int(len(welded.vertices)),
        "faces": int(len(welded.faces)),
        "connected_components": int(len(components)),
        "watertight": bool(welded.is_watertight),
        "winding_consistent": bool(welded.is_winding_consistent),
        "volume_mm3": float(welded.volume) if welded.is_watertight else None,
        "euler_number": int(welded.euler_number),
    }


def compare_instances(source: list[MeshInstance], cache: list[MeshInstance]) -> dict[str, object]:
    if not cache:
        return {
            "status": "not_comparable",
            "all_parts_match": None,
            "reason": "studio-cache.3mf core model has no mesh resources and no build items",
        }
    source_by_name = {x.name: x for x in source}
    cache_by_name = {x.name: x for x in cache}
    result: dict[str, object] = {"all_parts_match": set(source_by_name) == set(cache_by_name), "parts": {}}
    for name in sorted(set(source_by_name) | set(cache_by_name)):
        a, b = source_by_name.get(name), cache_by_name.get(name)
        equal = bool(
            a is not None and b is not None
            and np.array_equal(a.raw_vertices, b.raw_vertices)
            and np.array_equal(a.faces, b.faces)
            and np.allclose(a.transform, b.transform, atol=1e-10, rtol=0)
            and np.allclose(a.mesh.vertices, b.mesh.vertices, atol=1e-10, rtol=0)
        )
        result["parts"][name] = {
            "raw_vertices_equal": bool(a is not None and b is not None and np.array_equal(a.raw_vertices, b.raw_vertices)),
            "faces_equal": bool(a is not None and b is not None and np.array_equal(a.faces, b.faces)),
            "build_transform_equal": bool(a is not None and b is not None and np.allclose(a.transform, b.transform, atol=1e-10, rtol=0)),
            "plate_vertices_equal": equal,
        }
    result["all_parts_match"] = bool(result["all_parts_match"] and all(x["plate_vertices_equal"] for x in result["parts"].values()))
    return result


def main() -> None:
    source = load_instances(INPUT)
    cache = load_instances(CACHE)
    whole = trimesh.util.concatenate([x.mesh for x in source])
    center_z = float((whole.bounds[0, 2] + whole.bounds[1, 2]) / 2.0)
    section_png = HERE / "center-section.png"
    line_counts = draw_center_section(source, section_png, center_z)
    with zipfile.ZipFile(CACHE) as cache_archive:
        cache_model = package_model(cache_archive, MODEL_PATH)
        cache_inventory = {
            "object_count": len(cache_model.findall("m:resources/m:object", NS)),
            "build_item_count": len(cache_model.findall("m:build/m:item", NS)),
        }
        embedded_gcode = cache_archive.read("Metadata/plate_1.gcode")
    separate_gcode = JOB / "input" / "studio-cache-192layers.gcode"
    gcode_comparison = {
        "embedded_sha256": hashlib.sha256(embedded_gcode).hexdigest().upper(),
        "embedded_byte_count": len(embedded_gcode),
        "separate_file_sha256": sha256(separate_gcode) if separate_gcode.exists() else None,
        "separate_file_byte_count": separate_gcode.stat().st_size if separate_gcode.exists() else None,
        "byte_identical": embedded_gcode == separate_gcode.read_bytes() if separate_gcode.exists() else None,
    }
    report = {
        "source_file": str(INPUT),
        "original_download_path": r"C:\Users\makim\Downloads\Car_Toy_Model_Interior_Bam_60mm.3mf",
        "source_sha256": sha256(INPUT),
        "cache_3mf": str(CACHE),
        "cache_sha256": sha256(CACHE),
        "cache_core_model_inventory": cache_inventory,
        "cache_embedded_gcode_comparison": gcode_comparison,
        "coordinate_frame": "plate/build coordinates, mm; +Z is the build direction",
        "transform_rule": "3MF model build item and component transforms only; Metadata/model_settings.config matrix is ignored",
        "parts": [
            {
                "name": x.name,
                "source_model": x.model_path,
                "source_object_id": x.object_id,
                "transform_row_major_3mf": [
                    *[float(v) for v in x.transform[:3, :3].reshape(-1)],
                    *[float(v) for v in x.transform[3, :3]],
                ],
                "raw_vertex_count": int(len(x.raw_vertices)),
                "raw_face_count": int(len(x.faces)),
                "bbox_plate": bounds(x.mesh),
                "topology": topology(x.mesh),
                "center_section_polyline_count": line_counts[x.name],
            }
            for x in source
        ],
        "assembly_bbox_plate": bounds(whole),
        "center_section_z_mm": center_z,
        "source_cache_mesh_comparison": compare_instances(source, cache),
        "artifacts": {"center_section_png": str(section_png)},
    }
    (HERE / "measurements.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "bbox_mm": report["assembly_bbox_plate"]["size_mm"],
        "center_section_z_mm": center_z,
        "source_cache_mesh_match": report["source_cache_mesh_comparison"]["all_parts_match"],
        "parts": [(x["name"], x["bbox_plate"]["size_mm"], x["topology"]["watertight"]) for x in report["parts"]],
        "outputs": [section_png.name, "measurements.json"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
