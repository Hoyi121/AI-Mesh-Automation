# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import run_case


FIXTURE = Path(__file__).with_name("fake_foam.py").resolve()


def codes(stage: dict) -> set[str]:
    return {issue["code"] for issue in stage["diagnostics"]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.root = self.scratch / "Cavity demo with spaces"
        self.template = self.root / run_case.TEMPLATE
        shutil.copytree(run_case.ROOT / "cases", self.root / "cases")
        self.commands = {
            name: [sys.executable, str(FIXTURE), name] for name in ("blockMesh", "checkMesh", "icoFoam", "foamRun")
        }

    def run_fixture(self, scenario="success", solver="auto", env_version=True, timeout=10, retries=0):
        options = argparse.Namespace(solver=solver, timeout=timeout, retries=retries)
        with mock.patch.object(run_case, "find_commands", return_value=self.commands), mock.patch.dict(os.environ):
            os.environ["AI_MESH_TEST_SCENARIO"] = scenario
            os.environ.pop("FOAM_API", None)
            if env_version:
                os.environ["WM_PROJECT_VERSION"] = "fixture-v2512"
            else:
                os.environ.pop("WM_PROJECT_VERSION", None)
            report, path = run_case.run_workflow(self.root, options)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), report)
        self.assertEqual(path.parent.parent, self.root / "runs")
        return report, path.parent

    def test_cli_help_invalid_arguments_and_missing_runtime_from_another_directory(self):
        for name in ("run_case.py", "log_parser.py"):
            shutil.copy2(run_case.ROOT / name, self.root / name)
        command = [sys.executable, str(self.root / "run_case.py")]
        env = dict(os.environ, PATH="", PYTHONIOENCODING="utf-8")
        cwd = self.scratch
        for flags, exit_code in ((["--help"], 0), (["--timeout", "0"], 2), (["--retries", "3"], 2), ([], 1)):
            with self.subTest(flags=flags):
                result = subprocess.run(
                    [*command, *flags], cwd=cwd, env=env, capture_output=True, encoding="utf-8", timeout=15,
                )
                self.assertEqual(result.returncode, exit_code, result.stderr)
        report_path, = (self.root / "runs").glob("*/report.json")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(codes(report), {"missing_command"})

    def test_success_prefers_icofoam_and_preserves_the_reference_case(self):
        original = (self.template / "0" / "U").read_bytes()
        report, run_dir = self.run_fixture()
        self.assertEqual(report["openfoam"]["selected_solver"], "icoFoam")
        self.assertEqual(report["openfoam"]["version_source"], "WM_PROJECT_VERSION")
        self.assertEqual(report["status"], "passed")
        attempt, = report["attempts"]
        stages = attempt["stages"]
        self.assertEqual([stage["stage"] for stage in stages], ["blockMesh", "checkMesh", "solver"])
        self.assertTrue(all(stage["exit_code"] == 0 and (run_dir / stage["log"]).is_file() for stage in stages))
        self.assertTrue(stages[1]["mesh"]["passed"])
        self.assertEqual(stages[1]["mesh"]["cells"], 400)
        self.assertEqual(stages[1]["mesh"]["max_non_orthogonality_deg"], 0)
        self.assertEqual(stages[2]["solver"]["last_time"], 0.5)
        self.assertEqual(stages[2]["solver"]["residuals"]["p"]["last_final"], 5e-7)
        self.assertTrue((run_dir / attempt["case"] / "0.5" / "U").is_file())
        self.assertEqual((self.template / "0" / "U").read_bytes(), original)
        self.assertFalse((self.template / "constant" / "polyMesh").exists())

    def test_auto_falls_back_to_foamrun_and_probes_version(self):
        del self.commands["icoFoam"]
        report, run_dir = self.run_fixture(env_version=False)
        self.assertEqual(report["status"], "passed")
        environment = report["openfoam"]
        self.assertEqual(environment["selected_solver"], "foamRun")
        self.assertEqual(environment["solver_module"], "incompressibleFluid")
        self.assertEqual(environment["version"], "v2512")
        self.assertTrue((run_dir / environment["version_probe"]["log"]).is_file())
        self.assertEqual(report["attempts"][0]["stages"][2]["solver"]["last_time"], 0.5)

    def test_requested_solver_never_falls_back(self):
        del self.commands["icoFoam"]
        report, _ = self.run_fixture(solver="icoFoam")
        self.assertEqual(codes(report), {"missing_command"})
        self.assertEqual(report["attempts"], [])

    def test_solver_override_selects_foamrun_with_both_commands_available(self):
        report, _ = self.run_fixture(solver="foamRun")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["openfoam"]["selected_solver"], "foamRun")

    def test_command_lookup_uses_absolute_path_entries(self):
        installation = self.scratch / "OpenFOAM installation"
        installation.mkdir()
        name = "blockMesh.exe" if os.name == "nt" else "blockMesh"
        executable = installation / name
        executable.touch()
        executable.chmod(0o700)
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join((".", str(installation)))}):
            self.assertEqual(run_case.find_commands(), {"blockMesh": [str(executable)]})

    def test_missing_required_file_stops_before_meshing(self):
        (self.template / "0" / "p").unlink()
        report, _ = self.run_fixture()
        self.assertEqual(codes(report), {"missing_case_file"})
        self.assertEqual(report["attempts"], [])

    def test_external_hard_link_is_rejected(self):
        outside = self.scratch / "outside"
        outside.write_text("untouched", encoding="utf-8")
        target = self.template / "0" / "p"
        target.unlink()
        os.link(outside, target)
        report, _ = self.run_fixture()
        self.assertEqual(codes(report), {"invalid_case_file"})
        self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_dictionary_directives_are_rejected(self):
        target = self.template / "0" / "p"
        target.write_text('#include "other"\n', encoding="utf-8")
        report, _ = self.run_fixture()
        self.assertEqual(codes(report), {"invalid_case_file"})

    def test_redirected_run_root_is_rejected(self):
        outside = self.scratch / "elsewhere"
        outside.mkdir()
        output = self.root / "runs"
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(outside), str(output))
        else:
            output.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            run_case.make_run_dir(self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_blockmesh_errors_capture_stderr_and_stop_with_no_retry(self):
        report, run_dir = self.run_fixture("mesh_exit", retries=2)
        attempt, = report["attempts"]
        stage, = attempt["stages"]
        self.assertEqual(stage["exit_code"], 3)
        self.assertEqual(codes(stage), {"mesh_generation_failed", "foam_fatal_io_error"})
        self.assertIn("Test invalid block specification", (run_dir / stage["log"]).read_text(encoding="utf-8"))
        fatal = next(issue for issue in stage["diagnostics"] if issue["code"] == "foam_fatal_io_error")
        self.assertIn("Test invalid block specification", fatal["message"])

    def test_blockmesh_requires_generated_mesh_files(self):
        report, _ = self.run_fixture("mesh_output_missing")
        stage, = report["attempts"][0]["stages"]
        self.assertEqual(codes(stage), {"mesh_output_missing"})

    def test_checkmesh_must_pass_both_the_exit_status_and_log_gate(self):
        for scenario, expected in (
            ("mesh_failed_verdict", "mesh_quality_failed"),
            ("mesh_conflicting_verdict", "mesh_quality_failed"),
            ("mesh_nonzero", "mesh_check_failed"),
            ("mesh_unrecognized_verdict", "mesh_verdict_missing"),
        ):
            with self.subTest(scenario=scenario):
                report, _ = self.run_fixture(scenario)
                self.assertEqual(report["status"], "failed")
                stages = report["attempts"][0]["stages"]
                self.assertEqual([stage["stage"] for stage in stages], ["blockMesh", "checkMesh"])
                self.assertIn(expected, codes(stages[-1]))
                mesh = stages[-1]["mesh"]
                self.assertFalse(mesh["passed"])
                if expected == "mesh_quality_failed":
                    self.assertEqual(mesh["max_aspect_ratio"], 6.5e10)
                    self.assertIn("High aspect ratio", mesh["problem_samples"][0])

    def test_solver_exit_divergence_nonfinite_data_and_incomplete_output(self):
        for scenario, expected in (
            ("floating_point", "floating_point_exception"),
            ("divergence", "solver_divergence"),
            ("nonfinite", "invalid_solver_number"),
            ("missing_output", "solver_output_missing"),
            ("no_end", "solver_end_missing"),
        ):
            with self.subTest(scenario=scenario):
                report, _ = self.run_fixture(scenario)
                self.assertEqual(report["status"], "failed")
                stage = report["attempts"][0]["stages"][-1]
                self.assertIn(expected, codes(stage))
                if scenario == "floating_point":
                    self.assertIn("solver_failed", codes(stage))
                if scenario == "nonfinite":
                    self.assertEqual(stage["solver"]["residuals"]["p"]["records"], 1)

    def test_residual_records_are_optional(self):
        report, _ = self.run_fixture("no_residuals")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["attempts"][0]["stages"][-1]["solver"]["residuals"], {})

    def test_timeout_retry_uses_a_new_case_and_keeps_partial_log(self):
        report, run_dir = self.run_fixture("timeout_once", timeout=1, retries=1)
        self.assertEqual(report["status"], "passed")
        first, second = report["attempts"]
        timed_out, = first["stages"]
        self.assertEqual(codes(timed_out), {"timeout"})
        self.assertIn("Test command waiting", (run_dir / timed_out["log"]).read_text(encoding="utf-8"))
        self.assertTrue((run_dir / first["case"] / "timeout-marker").exists())
        self.assertFalse((run_dir / second["case"] / "timeout-marker").exists())

    def test_retry_limit(self):
        report, _ = self.run_fixture("timeout_always", timeout=1, retries=2)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["attempts"]), 3)
        self.assertTrue(all(codes(attempt["stages"][-1]) == {"timeout"} for attempt in report["attempts"]))


if __name__ == "__main__":
    unittest.main()
