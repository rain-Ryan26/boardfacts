# BoardFacts

BoardFacts is a local-first workbench for reviewing PCB manufacturing documents. It registers project materials, analyzes XLSX, PDF/image, Gerber, and Drill inputs, preserves source evidence, applies a JSON rule set, and presents the latest run in a browser GUI.

## Current status

This repository is a development snapshot, not a hosted service. The local GUI, API, CLI, file registration, analysis pipeline, rule checks, evidence views, and Markdown reports are implemented. Model-assisted analysis requires a compatible external endpoint; the bundled samples are synthetic.

## Quick start

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). The repository pins Python 3.14 for the default development environment.

```powershell
uv sync
Copy-Item .env.example .env
```

Edit `.env` and set `FLYBOARD_STORAGE_ROOT` to an absolute directory outside this repository. Configure both model variables when you want to run model-assisted analysis.

```powershell
uv run boardfacts serve
```

Open `http://127.0.0.1:8000`. Create a project, then drag this synthetic material folder into the GUI:

```text
testsources/complex_gerber_pcb/materials
```

Its matching synthetic rule library is:

```text
testsources/complex_gerber_pcb/rules/PCB_MANUFACTURING_RULES_R3.json
```

## Documentation

- [Installation and configuration](docs/setup.md)
- [GUI workflow](docs/usage.md)
- [Synthetic sample policy](docs/sample-data-policy.md)

## Verification

```powershell
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

## License

No license is included in this snapshot. Default copyright restrictions apply.
