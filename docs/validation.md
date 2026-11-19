# Validation record

Verified on 2026-11-19 using Windows x64 and Python 3.12.10.

## OpenFOAM run

The run used the actual serial executables from the official OpenCFD OpenFOAM v2512 MinGW package. The runtime files were extracted to the workspace. `blockMesh -help` reported v2512, and automatic solver selection picked `icoFoam`. The reference case also passed in a checkout path containing spaces and non-ASCII characters. [Official v2512 release files](https://dl.openfoam.com/source/v2512/), [captured JSON report](cavity-v2512-report.json).

| Observation | Result |
| --- | --- |
| `blockMesh` | Exit 0 and all five mesh files written |
| `checkMesh` | Exit 0, `Mesh OK.`, 400 cells and 0 failed checks |
| Mesh metrics | Maximum aspect ratio 1, maximum non-orthogonality 0°, maximum skewness approximately 1.67 × 10⁻¹⁴ |
| `icoFoam` | Exit 0 after 100 logged time steps, last time 0.5 s, with an `End` marker |
| Final fields | `0.5/U` and `0.5/p` each contain 400 finite cell values |
| Parsed residual records | 100 each for `Ux` and `Uy`, 200 for `p` |

The captured report keeps the controller's relative paths. A new run produces the corresponding raw logs and case files in its local `runs/` directory.

## Failure tests with OpenFOAM

Each edit was applied to a disposable project copy. The CLI exited 1 in each trial and saved the relevant stage logs.

| Change | Observed behavior |
| --- | --- |
| Rename the `blocks` entry in `system/blockMeshDict` | `blockMesh` exited 1 with a fatal dictionary error. The report recorded `foam_fatal_io_error` and `mesh_generation_failed`. Execution stopped at meshing. |
| Set `simpleGrading (1e12 1 1)` in `system/blockMeshDict` | `blockMesh` exited 0. `checkMesh` also exited 0 and reported one failed check for excessive aspect ratio. The report recorded `mesh_quality_failed` and the problem excerpt. Execution stopped at the mesh check. |
| Set an unknown top-wall boundary condition type in `0/U` | Both mesh stages passed. `icoFoam` exited 1 with a fatal field dictionary error. The report recorded `foam_fatal_io_error` and `solver_failed`. |

## Python tests

```text
python -m unittest discover -s tests -v
Ran 17 tests
OK
```

The suite exercises CLI invocation, invalid options, repository-relative staging from another working directory, file and executable preflight, subprocess arguments, log capture, mesh verdicts, numerical diagnostics, final output checks, and timeout retries. Subprocess stand-ins exercise the solver selection logic and produce controlled failure logs.

## Pending validation

A real Foundation `foamRun -solver incompressibleFluid` run, additional OpenFOAM builds, Linux or WSL execution, and validation against a numerical cavity benchmark remain pending. The stand-in tests cover modular solver selection and flag handling, plus simulated divergence, floating-point failure and non-finite log values.
