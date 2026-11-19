# SPDX-License-Identifier: GPL-3.0-or-later
"""Recognize OpenFOAM diagnostics and summarize plain-text logs."""

import math
import re
from dataclasses import dataclass, field
from typing import Iterable


RESIDUAL = re.compile(
    r"\bSolving\s+for\s+([^,\s]+),\s+Initial residual\s*=\s*([^,\s]+),"
    r"\s+Final residual\s*=\s*([^,\s]+),\s+No Iterations\s+(\d+)",
    re.IGNORECASE,
)
TIME_STEP = re.compile(r"^Time\s*=\s*(\S+)$")
SOLVER_NUMBERS = (
    re.compile(r"^Courant Number mean:\s*(\S+)\s+max:\s*(\S+)", re.IGNORECASE),
    re.compile(
        r"time step continuity errors\s*:\s*sum local\s*=\s*([^,\s]+),"
        r"\s*global\s*=\s*([^,\s]+),\s*cumulative\s*=\s*([^,\s]+)",
        re.IGNORECASE,
    ),
)
MESH_METRICS = {
    "max_aspect_ratio": re.compile(r"\bMax aspect ratio\s*[:=]\s*([^,\s]+)"),
    "max_non_orthogonality_deg": re.compile(r"^Mesh non-orthogonality Max:\s*([^,\s]+)"),
    "max_skewness": re.compile(r"^Max skewness\s*=\s*([^,\s]+)"),
}
MESH_FAILURE = re.compile(r"^Failed\s+(\d+)\s+mesh checks?\.?$", re.IGNORECASE)
FATAL = re.compile(r"\bFOAM FATAL(?:\s+(IO))?\s+ERROR\b", re.IGNORECASE)
FLOATING_POINT = re.compile(
    r"\bfloating point exception\b(?!\s+trapping\b)|Foam::sigFpe::sigHandler",
    re.IGNORECASE,
)
DIVERGENCE = re.compile(
    r"\b(?:divergence detected|(?:solution|solver|simulation)\s+"
    r"(?:(?:has|is)\s+)?diverg(?:ed|ing))\b",
    re.IGNORECASE,
)
VERSION = re.compile(
    r"^(?:Version\s*:\s*(\S+)|(?:Using:\s*)?OpenFOAM[- ](v?\d[\w.-]*))",
    re.IGNORECASE,
)
NUMERICAL_ADVICE = "Inspect the first unstable time step, mesh quality and boundary conditions. Review deltaT before rerunning."


def diagnostic(code: str, message: str, suggestion: str) -> dict[str, str]:
    return {"code": code, "message": message, "suggestion": suggestion}


def finite_number(token: str) -> float | None:
    try:
        value = float(token)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


@dataclass
class LogAnalysis:
    version: str | None = None
    ended: bool = False
    last_time: float | None = None
    mesh: dict = field(default_factory=lambda: {"ok_marker": False, "failed_checks": None})
    residuals: dict = field(default_factory=dict)
    diagnostics: dict[str, dict[str, str]] = field(default_factory=dict)

    def flag(self, code: str, message: str, suggestion: str) -> None:
        if code not in self.diagnostics:
            self.diagnostics[code] = diagnostic(code, message[:320], suggestion)


def parse_mesh_line(line: str, result: LogAnalysis) -> None:
    if line in ("Mesh OK.", "Mesh OK"):
        result.mesh["ok_marker"] = True
    if match := MESH_FAILURE.fullmatch(line):
        result.mesh["failed_checks"] = max(result.mesh["failed_checks"] or 0, int(match[1]))
    if match := re.fullmatch(r"cells:\s*(\d+)", line):
        result.mesh["cells"] = int(match[1])
    if line.startswith("***"):
        problems = result.mesh.setdefault("problem_samples", [])
        if len(problems) < 5:
            problems.append(line.lstrip("* ").strip()[:320])
    for name, pattern in MESH_METRICS.items():
        if match := pattern.search(line):
            value = finite_number(match[1])
            if value is not None:
                result.mesh[name] = value
            else:
                result.flag("invalid_mesh_metric", line, "Inspect the full checkMesh log and the generated mesh.")


def parse_solver_line(line: str, result: LogAnalysis) -> None:
    if match := RESIDUAL.search(line):
        variable, initial_token, final_token, iterations = match.groups()
        initial, final = finite_number(initial_token), finite_number(final_token)
        if initial is None or final is None:
            result.flag("invalid_solver_number", line, NUMERICAL_ADVICE)
        else:
            summary = result.residuals.setdefault(variable, {"records": 0})
            summary["records"] += 1
            summary.setdefault("first_initial", initial)
            summary["last_initial"] = initial
            summary["last_final"] = final
            summary["last_iterations"] = int(iterations)
    if match := TIME_STEP.fullmatch(line):
        time = finite_number(match[1].removesuffix("s"))
        if time is None:
            result.flag("invalid_solver_number", line, NUMERICAL_ADVICE)
        else:
            result.last_time = time
    for pattern in SOLVER_NUMBERS:
        if (match := pattern.search(line)) and any(finite_number(token) is None for token in match.groups()):
            result.flag("invalid_solver_number", line, NUMERICAL_ADVICE)
    if FLOATING_POINT.search(line):
        result.flag("floating_point_exception", line, NUMERICAL_ADVICE)
    if DIVERGENCE.search(line):
        result.flag("solver_divergence", line, NUMERICAL_ADVICE)


def parse_log(lines: Iterable[str], stage: str) -> LogAnalysis:
    result = LogAnalysis()
    pending_fatal = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if pending_fatal is not None:
            result.diagnostics[pending_fatal]["message"] += "\n" + line[:320]
            pending_fatal = None
        if result.version is None and (match := VERSION.match(line)):
            result.version = match[1] or match[2]
        if match := FATAL.search(line):
            code = "foam_fatal_io_error" if match[1] else "foam_fatal_error"
            if code not in result.diagnostics:
                result.flag(code, line, "Open the stage log and inspect the OpenFOAM message and any referenced dictionary.")
                pending_fatal = code
        if line == "End":
            result.ended = True
        if stage == "checkMesh":
            parse_mesh_line(line, result)
        if stage == "solver":
            parse_solver_line(line, result)

    if result.mesh["ok_marker"] and result.mesh["failed_checks"] is None:
        result.mesh["failed_checks"] = 0
    return result
