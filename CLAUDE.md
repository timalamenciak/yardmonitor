# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -e ".[dev]"

# Run server (DGX)
python server.py

# Upload SD card from laptop
python client_upload.py --sensor-type camera_trap --location "backyard"

# Run local camera trap pipeline (standalone, no server)
python run_camera_trap.py --location "backyard"

# Run local AudioMoth pipeline (standalone, no server)
python -m yardmonitor.sensors.audiomoth.pipeline --location "pond"

# Run local dashboard (Flask, separate from server)
python run_dashboard.py

# Lint
ruff check .

# Tests
pytest
pytest tests/test_db.py          # single file
pytest -k "test_ingest"          # single test by name
```

## Architecture

The system is split into two execution modes:

**1. Server mode (DGX / `server.py`)** — for GPU-accelerated processing of already-uploaded files.  
`client_upload.py` detects the SD card, POSTs files to the FastAPI server, then triggers a job. The server stores everything in SQLite (`data/yardmonitor.db`) and serves a web UI.

**2. Local mode (`run_camera_trap.py`)** — legacy standalone pipeline that reads directly from an SD card, runs all AI stages, and writes Camtrap DP files. No server needed.

### Server package (`yardmonitor/server/`)
- `app.py` — FastAPI app; routes read from `request.app.state` (db, queue, models, data_dir) initialized in the `lifespan` context manager. Web UI routes render Jinja2 templates; API routes return JSON.
- `db.py` — All SQLite reads/writes. Every public method opens and closes its own connection (thread-safe). Schema in `SCHEMA` constant at top of file.
- `jobs.py` — `JobQueue` wraps `ThreadPoolExecutor`. Submit a deployment ID → worker calls `pipeline_runner.py` and writes job status back to DB.
- `pipeline_runner.py` — `ServerPipelineRunner` holds lazy-loaded model singletons. Dispatches to `_run_camera_trap` or `_run_audiomoth` based on sensor type, then calls the same stage modules used by the local pipeline.
- `models_registry.py` — reads `config/models.yaml`; provides `params(model_name)` used by the runner to configure each model. Upgrading a model = edit the YAML + restart.

### Sensor pipelines (`yardmonitor/sensors/`)
Both sensors follow the same pattern: `BasePipeline` (in `core/pipeline.py`) provides state-persistence helpers (`load_state`, `mark_step_complete`, `is_step_complete`) backed by `pipeline_state.json` so long jobs are resumable.

- **camera_trap** — stages: `ingest` → `exif` → `megadetector` → `speciesnet` → `timelapse` → `camtrap_dp`. Export standard: [Camtrap DP 1.0](https://tdwg.github.io/camtrap-dp/).
- **audiomoth** — stages: `ingest` → `birdnet` → `export`. Export standard: Darwin Core Archive. `birdnet.py` wraps `birdnetlib.Recording`; one `Analyzer` instance is reused across all files.

### AI model integration
Models are configured exclusively via `config/models.yaml` — no model names or versions are hardcoded in pipeline logic. `ModelsRegistry.params(name)` returns a dict that is spread directly into each wrapper class constructor, so adding a new parameter to YAML is all that's needed.

MegaDetector lives in `megadetector.py` (PytorchWildlife), SpeciesNet in `speciesnet.py` (tries Python API first, falls back to subprocess CLI), BirdNET in `sensors/audiomoth/birdnet.py` (birdnetlib).

### Data flow for a server upload
```
client_upload.py
  → POST /api/deployments          (creates DB row, makes media/ dir)
  → POST /api/deployments/{id}/upload  (per file, writes to media/)
  → POST /api/deployments/{id}/process (queues job)

JobQueue worker thread
  → pipeline_runner._run_camera_trap / _run_audiomoth
  → writes results to DB (observations table)
  → writes Camtrap DP CSVs or DwC-A ZIP to dep_dir/
  → updates job + deployment status
```

### Web UI
Templates extend `yardmonitor/server/templates/base.html`. Static CSS is at `yardmonitor/server/static/server.css`. The observations page filter form submits as a GET with URL params; the server re-renders the full page with filtered results (no HTMX/JS framework). The deployment detail page auto-refreshes every 8 s when a job is running.

### Naming conventions
- Deployment IDs: `{YYYYMMDD}_{location_slug}` (auto-generated) or user-supplied.
- Media IDs, observation IDs, job IDs: UUID4 strings.
- All datetimes stored as ISO 8601 strings in SQLite.
