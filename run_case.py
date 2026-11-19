# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the bundled OpenFOAM cavity case in a fresh project-local directory."""

import argparse
import json
import os
import platform
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from log_parser import LogAnalysis, NUMERICAL_ADVICE, diagnostic, parse_log


ROOT = Path(__file__).resolve().parent
TEMPLATE = "cases/cavity"
TOOLS = ("blockMesh", "checkMesh", "icoFoam", "foamRun")
COMMON_INPUTS = ("0/U", "0/p", "system/blockMeshDict", "system/controlDict", "system/fvSchemes", "system/fvSolution")
SOLVER_INPUTS = {
    "icoFoam": ("constant/transportProperties", "constant/physicalProperties"),
    "foamRun": ("constant/physicalProperties", "constant/momentumTransport"),
}
MESH_FILES = tuple(f"constant/polyMesh/{name}" for name in ("points", "faces", "owner", "neighbour", "boundary"))
FINAL_FIELDS = ("0.5/U", "0.5/p")
EXIT_ADVICE = {
    "blockMesh": ("mesh_generation_failed", "Inspect system/blockMeshDict and the blockMesh log."),
    "checkMesh": ("mesh_check_failed", "Inspect the checkMesh log for topology or geometry errors."),
    "solver": ("solver_failed", "Inspect the solver log and check the selected OpenFOAM application and case dictionaries."),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def regular_file(root: Path, relative: str) -> Path:
    path = root
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        path /= part
        info = path.lstat()
        if is_link(info):
            raise ValueError("links and reparse points are unsupported")
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError("expected an ordinary directory")
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size == 0:
        raise ValueError("expected a non-empty, ordinary file with one link")
    return path


def snapshot_inputs(root: Path, solver: str | None) -> tuple[dict[str, bytes], list[dict]]:
    files = {}
    issues = []
    for relative in COMMON_INPUTS + SOLVER_INPUTS.get(solver, ()):
        name = f"{TEMPLATE}/{relative}"
        advice = "Restore the file from the tracked cavity template."
        try:
            data = regular_file(root, name).read_bytes()
            # The bundled dictionaries are self-contained. Keep includes and code directives out.
            if b"#" in data:
                raise ValueError("OpenFOAM dictionary directives are unsupported")
            files[relative] = data
        except FileNotFoundError:
            issues.append(diagnostic("missing_case_file", f"Missing {name}", advice))
        except (OSError, ValueError) as error:
            issues.append(diagnostic("invalid_case_file", f"{name}: {error}", advice))
    return files, issues


def make_run_dir(root: Path) -> Path:
    parent = root / "runs"
    parent.mkdir(exist_ok=True)
    info = parent.lstat()
    if is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("runs must be an ordinary directory within the project")
    return Path(tempfile.mkdtemp(prefix="run-", dir=parent))


def find_commands() -> dict[str, list[str]]:
    directories = [Path(entry.strip('"')) for entry in os.get_exec_path()]
    directories = [directory for directory in directories if directory.is_absolute()]
    commands = {}
    for name in TOOLS:
        filename = f"{name}.exe" if os.name == "nt" else name
        for directory in directories:
            executable = directory / filename
            if executable.is_file() and os.access(executable, os.X_OK):
                commands[name] = [str(executable)]
                break
    return commands


def stop_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        if process.poll() is None:
            try:
                subprocess.run(
                    [str(taskkill), "/T", "/F", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def execute(
    stage: str, command: list[str], cwd: Path, log_path: Path, run_dir: Path, temp_dir: Path, timeout: int
) -> tuple[dict, LogAnalysis]:
    result = {
        "stage": stage, "argv": [Path(command[0]).name, *command[1:]],
        "log": log_path.relative_to(run_dir).as_posix(),
        "exit_code": None, "timed_out": False, "retryable": False, "diagnostics": [],
    }
    started = time.monotonic()
    analysis = LogAnalysis()
    child_env = dict(
        os.environ, TMPDIR=str(temp_dir), TEMP=str(temp_dir), TMP=str(temp_dir),
        FOAM_JOB_DIR=str(temp_dir), FOAM_FILEHANDLER="uncollated",
    )
    try:
        with log_path.open("xb") as log:
            process = subprocess.Popen(
                command, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"),
            )
            try:
                process.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                timed_out = isinstance(error, subprocess.TimeoutExpired)
                result["timed_out"] = timed_out
                result["diagnostics"].append(diagnostic(
                    "timeout" if timed_out else "interrupted",
                    f"Process exceeded {timeout} seconds." if timed_out else "Run interrupted during this stage.",
                    "Inspect the partial log before rerunning.",
                ))
                try:
                    stop_process(process)
                    result["retryable"] = timed_out
                except (OSError, subprocess.TimeoutExpired):
                    result["diagnostics"].append(diagnostic(
                        "termination_unconfirmed", "Could not confirm process termination.",
                        "Check the process state before starting another run.",
                    ))
            result["exit_code"] = process.poll()
    except OSError as error:
        result["diagnostics"].append(diagnostic(
            "launch_or_log_error", str(error), "Check the executable, runtime libraries and run directory permissions."
        ))
    result["duration_seconds"] = round(time.monotonic() - started, 3)

    if log_path.is_file():
        try:
            with log_path.open(encoding="utf-8", errors="replace") as log:
                analysis = parse_log(log, stage)
        except OSError as error:
            result["diagnostics"].append(diagnostic("log_read_error", str(error), "Check that the log is readable."))
    result["diagnostics"].extend(analysis.diagnostics.values())
    return result, analysis


def preflight(root: Path, run_dir: Path, args: argparse.Namespace, commands: dict) -> tuple[dict, dict[str, bytes], list[dict]]:
    solver = args.solver
    if solver == "auto":
        solver = next((name for name in SOLVER_INPUTS if name in commands), None)
    openfoam = {
        "project": os.environ.get("WM_PROJECT"),
        "version": os.environ.get("WM_PROJECT_VERSION") or os.environ.get("FOAM_API") or None,
        "version_source": None,
        "available": {name: name in commands for name in TOOLS},
        "requested_solver": args.solver,
        "selected_solver": solver if solver in commands else None,
        "solver_module": "incompressibleFluid" if solver == "foamRun" and solver in commands else None,
    }
    if openfoam["version"]:
        openfoam["version_source"] = "WM_PROJECT_VERSION" if os.environ.get("WM_PROJECT_VERSION") else "FOAM_API"
    elif "blockMesh" in commands:
        probe, analysis = execute(
            "version_probe", [*commands["blockMesh"], "-help"], run_dir,
            run_dir / "version.log", run_dir, run_dir, min(15, args.timeout),
        )
        openfoam["version_probe"] = probe
        if analysis.version:
            openfoam.update(version=analysis.version, version_source="blockMesh -help")

    files, issues = snapshot_inputs(root, solver)
    missing = [tool for tool in ("blockMesh", "checkMesh") if tool not in commands]
    if solver not in commands:
        missing.append(solver or "icoFoam or foamRun")
    for name in missing:
        issues.append(diagnostic(
            "missing_command", f"Could not find {name} on PATH.",
            "Activate the OpenFOAM environment and verify the required application is available.",
        ))
    if probe := openfoam.get("version_probe"):
        for issue in probe["diagnostics"]:
            if issue["code"] in {"interrupted", "termination_unconfirmed"}:
                issues.append(issue)
    return openfoam, files, issues


def missing_outputs(case_dir: Path, expected: tuple[str, ...]) -> list[str]:
    missing = []
    for relative in expected:
        try:
            regular_file(case_dir, relative)
        except (OSError, ValueError):
            missing.append(relative)
    return missing


def assess_stage(result: dict, analysis: LogAnalysis, case_dir: Path) -> None:
    stage = result["stage"]
    issues = result["diagnostics"]
    exit_code = result["exit_code"]
    if stage == "solver" and exit_code == -signal.SIGFPE and "floating_point_exception" not in analysis.diagnostics:
        issues.append(diagnostic("floating_point_exception", "Solver exited on SIGFPE.", NUMERICAL_ADVICE))
    if exit_code not in (None, 0) and not result["timed_out"] and not any(issue["code"] == "interrupted" for issue in issues):
        code, advice = EXIT_ADVICE[stage]
        issues.append(diagnostic(code, f"Process exited with status {exit_code}.", advice))

    if stage == "checkMesh":
        mesh = analysis.mesh
        if mesh["failed_checks"]:
            issues.append(diagnostic(
                "mesh_quality_failed", f"checkMesh reported {mesh['failed_checks']} failed checks.",
                "Review the failing checks and fix the mesh before solving.",
            ))
        elif exit_code == 0 and not mesh["ok_marker"]:
            issues.append(diagnostic(
                "mesh_verdict_missing", "checkMesh produced no explicit Mesh OK verdict.",
                "Inspect the complete log and verify the installed checkMesh format.",
            ))
        result["mesh"] = {**mesh, "passed": exit_code == 0 and not issues}
    elif stage == "solver":
        result["solver"] = {"ended": analysis.ended, "last_time": analysis.last_time, "residuals": analysis.residuals}
        if exit_code == 0 and not issues and not analysis.ended:
            issues.append(diagnostic(
                "solver_end_missing", "Solver exited without the OpenFOAM End marker.",
                "Inspect the solver log and verify that time stepping reached the requested end.",
            ))

    expected = {"blockMesh": MESH_FILES, "solver": FINAL_FIELDS}.get(stage)
    if expected and exit_code == 0 and not issues:
        if missing := missing_outputs(case_dir, expected):
            issues.append(diagnostic(
                "mesh_output_missing" if stage == "blockMesh" else "solver_output_missing",
                "Missing or invalid output files: " + ", ".join(missing),
                "Inspect the stage log and verify the expected output was written to the run case.",
            ))
    result["status"] = "passed" if exit_code == 0 and not issues else "failed"


def run_attempt(run_dir: Path, index: int, files: dict[str, bytes], solver: str, commands: dict, timeout: int) -> dict:
    attempt_dir = run_dir / f"attempt-{index:02d}"
    case_dir = attempt_dir / "case"
    log_dir = attempt_dir / "logs"
    temp_dir = attempt_dir / "tmp"
    attempt = {"number": index, "status": "failed", "case": case_dir.relative_to(run_dir).as_posix(), "stages": []}
    try:
        log_dir.mkdir(parents=True)
        temp_dir.mkdir()
        for relative, data in files.items():
            target = case_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    except OSError as error:
        attempt["diagnostics"] = [diagnostic(
            "case_staging_failed", str(error), "Check the run directory permissions and available space."
        )]
        return attempt

    solver_args = ["-solver", "incompressibleFluid"] if solver == "foamRun" else []
    steps = (
        ("blockMesh", commands["blockMesh"], "01-blockMesh.log"),
        ("checkMesh", commands["checkMesh"], "02-checkMesh.log"),
        ("solver", [*commands[solver], *solver_args, "-noFunctionObjects"], f"03-{solver}.log"),
    )
    for stage, command, log_name in steps:
        result, analysis = execute(
            stage, [*command, "-case", "."], case_dir, log_dir / log_name, run_dir, temp_dir, timeout,
        )
        if stage == "blockMesh":
            attempt["block_mesh_version"] = analysis.version
        assess_stage(result, analysis, case_dir)
        attempt["stages"].append(result)
        if result["status"] == "failed":
            return attempt
    attempt["status"] = "passed"
    return attempt


def run_workflow(root: Path, args: argparse.Namespace) -> tuple[dict, Path]:
    run_dir = make_run_dir(root)
    started = utc_now()
    commands = find_commands()
    openfoam, files, issues = preflight(root, run_dir, args, commands)
    report = {
        "schema_version": 1, "case_template": TEMPLATE, "status": "failed", "started_at": started,
        "platform": platform.system(), "python_version": platform.python_version(),
        "stage_timeout_seconds": args.timeout, "max_retries": args.retries,
        "openfoam": openfoam, "diagnostics": issues, "attempts": [],
    }
    if not issues:
        for index in range(1, args.retries + 2):
            attempt = run_attempt(run_dir, index, files, openfoam["selected_solver"], commands, args.timeout)
            report["attempts"].append(attempt)
            if not openfoam["version"] and attempt.get("block_mesh_version"):
                openfoam.update(version=attempt["block_mesh_version"], version_source="blockMesh run log")
            if attempt["status"] == "passed":
                report["status"] = "passed"
                break
            if not attempt["stages"] or not attempt["stages"][-1]["retryable"]:
                break

    report["finished_at"] = utc_now()
    report_path = run_dir / "report.json"
    temporary_report = report_path.with_suffix(".tmp")
    temporary_report.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary_report.replace(report_path)
    return report, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solver", choices=("auto", *SOLVER_INPUTS), default="auto", help="auto prefers icoFoam")
    parser.add_argument("--timeout", type=int, default=120, metavar="SECONDS", help="per-stage timeout (1-3600, default 120)")
    parser.add_argument("--retries", type=int, choices=range(3), default=0, help="retry only a timed-out stage by restarting the case")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600:
        parser.error("--timeout must be between 1 and 3600 seconds")
    try:
        report, path = run_workflow(ROOT, args)
    except (OSError, ValueError) as error:
        print(f"Could not create or write the project run: {error}", file=sys.stderr)
        return 1
    print(f"Cavity workflow {report['status']}. Report: {path}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
