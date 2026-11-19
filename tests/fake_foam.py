# SPDX-License-Identifier: GPL-3.0-or-later
"""Subprocess test double. The fixture performs no CFD calculations."""

import os
import sys
import time
from pathlib import Path


def main() -> int:
    stage, *args = sys.argv[1:]
    scenario = os.environ["AI_MESH_TEST_SCENARIO"]
    if args == ["-help"]:
        print("Using: OpenFOAM-v2512 (test fixture)")
        return 0

    assert args[-2:] == ["-case", "."]
    cwd = Path.cwd()
    assert cwd.name == "case"
    assert Path(os.environ["TMPDIR"]) == cwd.parent / "tmp"
    assert Path(os.environ["FOAM_JOB_DIR"]) == cwd.parent / "tmp"
    assert (cwd / "0" / "U").is_file()
    print("Version: v2512", flush=True)

    if stage == "blockMesh":
        if scenario == "mesh_exit":
            print("--> FOAM FATAL IO ERROR:\n\nTest invalid block specification", file=sys.stderr)
            return 3
        if scenario == "timeout_always" or (scenario == "timeout_once" and cwd.parent.name == "attempt-01"):
            (cwd / "timeout-marker").touch()
            print("Test command waiting", flush=True)
            time.sleep(30)
        assert not (cwd / "timeout-marker").exists()
        mesh = cwd / "constant" / "polyMesh"
        mesh.mkdir()
        for name in ("points", "faces", "owner", "neighbour", "boundary"):
            if scenario != "mesh_output_missing" or name != "points":
                (mesh / name).write_text("test mesh file\n", encoding="utf-8")
    elif stage == "checkMesh":
        assert (cwd / "constant" / "polyMesh" / "points").is_file()
        print("  cells: 400\nMax aspect ratio = 1 OK.")
        print("Mesh non-orthogonality Max: 0 average: 0\nMax skewness = 9.1e-15 OK.")
        if scenario in ("mesh_failed_verdict", "mesh_conflicting_verdict"):
            print("    ***High aspect ratio cells found, Max aspect ratio: 6.5e+10, number of cells 260")
            print("Failed 2 mesh checks.")
        if scenario not in ("mesh_failed_verdict", "mesh_unrecognized_verdict"):
            print("Mesh OK.")
        if scenario == "mesh_nonzero":
            return 5
    else:
        assert "-noFunctionObjects" in args
        constant = cwd / "constant"
        if stage == "foamRun":
            assert args[:2] == ["-solver", "incompressibleFluid"]
            assert (constant / "physicalProperties").is_file()
            assert (constant / "momentumTransport").is_file()
            assert not (constant / "transportProperties").exists()
        else:
            assert stage == "icoFoam"
            assert (constant / "transportProperties").is_file()
            assert (constant / "physicalProperties").is_file()
            assert not (constant / "momentumTransport").exists()
        print("trapFpe: Floating point exception trapping enabled")
        print("Time = 0.5" + ("s" if stage == "foamRun" else ""))
        if scenario != "no_residuals":
            print("smoothSolver: Solving for Ux, Initial residual = 1e-04, Final residual = 1e-06, No Iterations 2")
            print("PCG: Solving for p, Initial residual = 0.06, Final residual = 5e-07, No Iterations 6")
        if scenario == "floating_point":
            print("Foam::sigFpe::sigHandler(int)", file=sys.stderr)
            return 9
        if scenario == "nonfinite":
            print("PCG: Solving for p, Initial residual = nan, Final residual = inf, No Iterations 2")
            print("Courant Number mean: 0.01 max: 1e999")
        if scenario == "divergence":
            print("Detected instability: the solution has diverged.", file=sys.stderr)
        if scenario != "missing_output":
            final = cwd / "0.5"
            final.mkdir()
            for name in ("U", "p"):
                (final / name).write_text("test output field\n", encoding="utf-8")
        if scenario == "no_end":
            return 0
    print("End", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
