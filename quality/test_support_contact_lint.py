"""Regression fixtures for local support contact, independent of archived jobs."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

QUALITY = Path(__file__).resolve().parent
sys.path.insert(0, str(QUALITY))
spec = importlib.util.spec_from_file_location("support_preflight", QUALITY / "support-removal-preflight.py")
preflight_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = preflight_module
spec.loader.exec_module(preflight_module)


def select(tool):
    return f"M620 S{tool}A\nT{tool}\nM621 S{tool}A\n"


def fixture_gcode(model_y=0, safe=False):
    return ("; HEADER_BLOCK_END\nM83\nG90\n" + select(0)
        + "; layer num/total_layer_count: 1/2\n; LAYER_HEIGHT: 0.2\n; LINE_WIDTH: 0.42\n"
        + "G0 X0 Y0 Z0.2\n; FEATURE: Support\nG1 X10 Y0 E1\n"
        + select(1) + "G0 X0 Y20 Z0.2\n; FEATURE: Support interface\nG1 X10 Y20 E1\n"
        + select(0) + "; layer num/total_layer_count: 2/2\n; LAYER_HEIGHT: 0.16\n"
        + f"G0 X0 Y{20 if safe else model_y} Z0.36\n; FEATURE: Outer wall\nG1 X10 Y{20 if safe else model_y} E1\n").encode()


def run_preflight(gcode, support=True):
    settings = {"support_interface_filament": "2", "filament_ids": ["GFA00", "GFS02"],
                "filament_is_support": ["0", "1"], "filament_type": ["PLA", "PLA-S"]}
    metadata = ('<config><plate><metadata key="support_used" value="' + str(support).lower()
        + '"/><filament id="1" tray_info_idx="GFA00" type="PLA" used_for_object="true" used_for_support="true"/>'
        + '<filament id="2" tray_info_idx="GFS02" type="PLA-S" used_for_object="false" used_for_support="true"/>'
        + '</plate></config>')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "fixture.3mf"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Metadata/project_settings.config", json.dumps(settings))
            archive.writestr("Metadata/slice_info.config", metadata)
            archive.writestr("Metadata/plate_1.gcode", gcode)
        return preflight_module.preflight(path)


class ContactRegression(unittest.TestCase):
    def test_interface_elsewhere_does_not_hide_pla_contact(self):
        result = run_preflight(fixture_gcode())
        self.assertEqual(result["status"], "BLOCK")
        self.assertTrue(any(issue["code"] == "NNI-TR-005" for issue in result["issues"]))

    def test_dedicated_interface_below_model(self):
        self.assertEqual(run_preflight(fixture_gcode(safe=True))["status"], "PASS_FILE_ONLY")

    def test_nearby_noncontact_support(self):
        self.assertEqual(run_preflight(fixture_gcode(model_y=1))["status"], "PASS_FILE_ONLY")

    def test_support_free_bed_start(self):
        gcode = ("; HEADER_BLOCK_END\nM83\n" + select(0)
                 + "; layer num/total_layer_count: 1/1\nG0 X0 Y0 Z0.2\n; FEATURE: Outer wall\nG1 X10 Y0 E1\n").encode()
        self.assertEqual(run_preflight(gcode, support=False)["status"], "PASS_FILE_ONLY")

    def test_arc_bulge_contacts_even_when_chord_does_not(self):
        gcode = fixture_gcode().replace(b"G1 X10 Y0 E1", b"G2 X10 Y0 I5 J0 E1", 1)
        gcode = gcode.replace(b"G0 X0 Y0 Z0.36", b"G0 X4.8 Y5 Z0.36")
        gcode = gcode.replace(b"; FEATURE: Outer wall\nG1 X10 Y0 E1", b"; FEATURE: Outer wall\nG1 X5.2 Y5 E1")
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_support_below_an_air_gap_is_not_contact(self):
        gcode = fixture_gcode().replace(b"Z0.36", b"Z0.6")
        self.assertEqual(run_preflight(gcode)["status"], "PASS_FILE_ONLY")

    def test_t1000_preserves_committed_material(self):
        gcode = fixture_gcode(safe=True).replace(b"; FEATURE: Outer wall", b"T1000\n; FEATURE: Outer wall")
        self.assertEqual(run_preflight(gcode)["status"], "PASS_FILE_ONLY")

    def test_missing_line_width_blocks_supported_job(self):
        gcode = fixture_gcode(safe=True).replace(b"; LINE_WIDTH: 0.42\n", b"")
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_unsupported_radius_arc_blocks(self):
        gcode = fixture_gcode(safe=True).replace(b"G1 X10 Y0 E1", b"G2 X10 Y0 R5 E1", 1)
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_absolute_extrusion_with_reset_detects_contact(self):
        gcode = fixture_gcode().replace(b"M83", b"M82").replace(b"; FEATURE:", b"G92 E0\n; FEATURE:")
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_unclassified_extrusion_cannot_claim_support_free(self):
        gcode = ("; HEADER_BLOCK_END\nM83\n" + select(0)
                 + "; layer num/total_layer_count: 1/1\nG0 X0 Y0 Z0.2\nG1 X10 Y0 E1\n").encode()
        self.assertEqual(run_preflight(gcode, support=False)["status"], "BLOCK")

    def test_counterclockwise_arc_contact(self):
        gcode = fixture_gcode().replace(b"G1 X10 Y0 E1", b"G3 X10 Y0 I5 J0 E1", 1)
        gcode = gcode.replace(b"G0 X0 Y0 Z0.36", b"G0 X4.8 Y-5 Z0.36")
        gcode = gcode.replace(b"; FEATURE: Outer wall\nG1 X10 Y0 E1", b"; FEATURE: Outer wall\nG1 X5.2 Y-5 E1")
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_relative_xy_support_contact(self):
        gcode = fixture_gcode().replace(b"; FEATURE: Support\nG1 X10 Y0 E1", b"; FEATURE: Support\nG91\nG1 X10 Y0 E1\nG90")
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_xyz_coordinate_reset_fails_closed(self):
        gcode = fixture_gcode(safe=True).replace(b"G0 X0 Y20 Z0.36", b"G0 X0 Y20 Z0.36\nG92 X100")
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_absolute_arc_center_mode_fails_closed(self):
        gcode = fixture_gcode(safe=True).replace(b"G1 X10 Y0 E1", b"G90.1\nG2 X10 Y0 I5 J0 E1", 1)
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_support_unretraction_at_travel_end_is_not_a_support_path(self):
        gcode = fixture_gcode(model_y=100).replace(b"G1 X10 Y0 E1\n", b"G1 X10 Y0 E1\nG0 X0 Y100\nG1 E.8\n", 1)
        self.assertEqual(run_preflight(gcode)["status"], "PASS_FILE_ONLY")

    def test_model_unretraction_at_travel_end_is_not_a_model_path(self):
        gcode = fixture_gcode(safe=True) + b"G0 X0 Y0 Z0.36\nG1 E.8\n"
        self.assertEqual(run_preflight(gcode)["status"], "PASS_FILE_ONLY")

    def test_current_model_line_width_changes_contact_threshold(self):
        gcode = fixture_gcode(model_y=0.5)
        self.assertEqual(run_preflight(gcode)["status"], "PASS_FILE_ONLY")
        gcode = gcode.replace(b"; FEATURE: Outer wall\n", b"; FEATURE: Outer wall\n; LINE_WIDTH: 0.8\n")
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_z_tolerance_does_not_bridge_a_larger_gap(self):
        self.assertEqual(run_preflight(fixture_gcode().replace(b"Z0.36", b"Z0.379"))["status"], "BLOCK")
        self.assertEqual(run_preflight(fixture_gcode().replace(b"Z0.36", b"Z0.381"))["status"], "PASS_FILE_ONLY")

    def test_xy_extrusion_with_unknown_start_is_not_unretraction(self):
        gcode = fixture_gcode(safe=True).replace(b"G0 X0 Y0 Z0.2\n", b"", 1)
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")

    def test_homing_invalidates_position_before_relative_extrusion(self):
        gcode = fixture_gcode(safe=True) + b"G28\nG91\nG1 X1 E1\nG90\n"
        self.assertEqual(run_preflight(gcode)["status"], "BLOCK")


if __name__ == "__main__":
    unittest.main()
