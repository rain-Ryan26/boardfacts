# GUI workflow

1. Complete [installation and configuration](setup.md).
2. Start the service with `uv run boardfacts serve`.
3. Open `http://127.0.0.1:8000` and create a project.
4. Drag `testsources/complex_gerber_pcb/materials` into the upload area. A ZIP of that folder is also accepted.
5. To run rule checks, register the absolute path of `testsources/complex_gerber_pcb/rules/PCB_MANUFACTURING_RULES_R3.json`.
6. Start a run. Gerber jobs may pause for layer review and confirmation before processing continues.
7. Review the latest project data, source evidence, tool findings, Gerber workspace, and generated report.

All bundled inputs are synthetic. Full XLSX, PDF/image, integration, and rule analysis requires the model endpoint described in [setup.md](setup.md).
