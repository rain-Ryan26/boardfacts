# Installation and configuration

## 1. Requirements

- Windows, macOS, or Linux
- Python 3.12 or newer
- `uv` for Python and dependency management

The repository's `.python-version` selects Python 3.14 by default. `uv` can install that interpreter automatically when Python downloads are enabled. To use another supported interpreter, pass it explicitly, for example `uv sync --python 3.12`.

Install `uv` using its official instructions: <https://docs.astral.sh/uv/getting-started/installation/>.

## 2. Install dependencies

Run commands from the repository root:

```powershell
uv sync
```

The default sync includes the development tools declared in `pyproject.toml`, including pytest and Ruff. For a runtime-only environment, use:

```powershell
uv sync --no-dev
```

## 3. Create local configuration

On PowerShell:

```powershell
Copy-Item .env.example .env
```

On macOS or Linux:

```bash
cp .env.example .env
```

BoardFacts reads `.env` from the current working directory and does not overwrite variables already exported by the shell.

Set this required value:

- `FLYBOARD_STORAGE_ROOT`: an absolute, writable directory outside the repository. Uploaded materials, run state, evidence, and reports are stored below it.

The model settings are optional for starting the GUI, but both are required for model-assisted analysis:

- `FLYBOARD_QWEN_API_BASE`: absolute HTTP(S) base URL for the compatible endpoint. BoardFacts appends `/chat/completions` when needed.
- `FLYBOARD_QWEN_API_KEY`: credential sent as a Bearer token.

The endpoint must support the model and request fields used by the current source. Do not commit `.env` or place runtime storage inside the repository.

## 4. Start the local service

With `FLYBOARD_STORAGE_ROOT` in `.env`:

```powershell
uv run boardfacts serve
```

Or override only the storage path on the command line:

```powershell
uv run boardfacts serve --storage-root C:\BoardFacts\data
```

The default address is `http://127.0.0.1:8000`. Use `--host`, `--port`, and `--log-level` when a different local binding is needed.

## 5. Verify the checkout

```powershell
uv run pytest
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
```

Tests use `.test-tmp`, and generated test, cache, build, coverage, runtime, and local-input paths are excluded by `.gitignore`.

## Configuration failures

- `FLYBOARD_STORAGE_ROOT is required`: copy `.env.example` to `.env`, run from the repository root, or pass `--storage-root`.
- `storage_root must be an absolute path`: replace a relative path with a full path.
- Model endpoint/key errors: configure both model variables or comment out both when only inspecting the GUI and local project functions.
