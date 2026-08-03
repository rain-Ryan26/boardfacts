# Sample-data policy

The public repository may contain only purpose-built synthetic samples.

- `testsources/synthetic_pcb/` and `testsources/complex_gerber_pcb/` were created for development and demonstration.
- Their names, drawings, spreadsheets, Gerber/Drill files, rules, expected results, and metadata are synthetic.
- Do not add customer files, supplier documents, real project identifiers, screenshots, rendered pages, extracted values, or copied fragments from real projects.
- Local real-world inputs belong outside the repository. The ignored `ref/`, `samples/`, `private/`, and `local-inputs/` paths are defense-in-depth only; ignored data must still never be included in a public snapshot or Git history.
- Before publishing a new snapshot, inspect both file contents and binary assets. Do not rely on `.gitignore` as a history-cleaning mechanism.
