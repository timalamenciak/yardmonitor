# YardMonitor

Ecological monitoring platform for trail cameras and AudioMoth recorders.  Plug in an SD card on your laptop — files upload automatically to a processing server (GPU recommended), AI models identify wildlife, and results appear in a searchable web interface with standards-compliant data exports.

**Sensors supported**
- Trail cameras (JPEG/RAW images) — detection via MegaDetector v5, species ID via Google SpeciesNet
- AudioMoth recorders (WAV files) — species ID via Cornell BirdNET-Analyzer (6362 species)

**Outputs**
- Camera trap: [Camtrap DP 1.0](https://tdwg.github.io/camtrap-dp/) package
- AudioMoth: [Darwin Core Archive](https://dwc.tdwg.org/text/) ZIP

---

## Requirements

| Dependency | Purpose | Install |
|---|---|---|
| Python ≥ 3.10 | Runtime | [python.org](https://www.python.org) |
| [ExifTool](https://exiftool.org) | Image metadata extraction | Platform installer |
| [ffmpeg](https://ffmpeg.org) | Timelapse video generation | Platform installer |
| CUDA GPU (optional) | Faster AI inference | Recommended for the server |

---

## Installation

```bash
git clone https://github.com/your-org/yardmonitor.git
cd yardmonitor
pip install -e .
```

For development extras:

```bash
pip install -e ".[dev]"
```

---

## Quick start

### Server (run on DGX or any GPU machine)

```bash
python server.py
```

Default: binds to `0.0.0.0:8000`.  Open `http://<server-ip>:8000` in a browser.

```
Options:
  --config PATH   Server config YAML   (default: config/server.yaml)
  --models PATH   Model registry YAML  (default: config/models.yaml)
  --host HOST     Bind address
  --port PORT     TCP port
```

API documentation is auto-generated at `http://<server-ip>:8000/docs`.

#### Running as a systemd service (recommended for DGX / always-on servers)

A service unit is included at `deploy/yardmonitor.service`. Edit the `User`, `WorkingDirectory`, and `ExecStart` paths if your username or venv location differs, then install it:

```bash
sudo cp deploy/yardmonitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yardmonitor
```

Useful commands:

```bash
sudo systemctl status yardmonitor      # check running state
sudo systemctl restart yardmonitor     # restart after a code update
journalctl -u yardmonitor -f           # live log tail
```

---

### Client upload (run on your laptop when you plug in an SD card)

```bash
# Trail camera
python client_upload.py --sensor-type camera_trap --location "backyard"

# AudioMoth
python client_upload.py --sensor-type audiomoth --location "pond"
```

The tool auto-detects the SD card, uploads all files to the server, and queues an AI processing job. A link to the deployment page is printed when done.

```
Options:
  --server URL       YardMonitor server URL  (default: http://spark-1267:8000)
  --drive PATH       SD card path            (auto-detected if omitted)
  --sensor-type      camera_trap | audiomoth
  --location NAME    Human-readable location name
  --sensor-id ID     Camera or AudioMoth serial / label
  --lat FLOAT        Deployment latitude
  --lon FLOAT        Deployment longitude
  --deployment-id ID Append to an existing deployment
  --no-process       Upload files only, do not trigger AI
  --dry-run          List files without uploading
```

---

### Standalone local pipeline (no server needed)

Camera trap:
```bash
python run_camera_trap.py --location "backyard"
python run_camera_trap.py --sd-card /media/SD_CARD --location "front gate" --lat 45.5 --lon -73.6
```

AudioMoth:
```bash
python -m yardmonitor.sensors.audiomoth.pipeline --location "pond"
```

Both pipelines are resumable — re-running the same command skips completed stages.

---

## Configuration

### `config/server.yaml` — server settings

```yaml
server:
  host: "0.0.0.0"
  port: 8000
  data_dir: "data"
  pipeline_workers: 1    # worker threads (1 per GPU recommended)
```

### `config/models.yaml` — AI model versions

Edit this file to upgrade or swap models; restart the server to apply changes.  The server exposes the active configuration at `GET /api/models` and on the `/models` page.

```yaml
models:
  megadetector:
    version: "v5a"          # "v5a" | "v5b"
    params:
      device: "cuda"        # "cuda" | "cpu" | "mps"
      batch_size: 32
      confidence_threshold: 0.1

  speciesnet:
    version: "1.0"
    params:
      country: "CA"         # ISO 3166-1 alpha-2 — improves accuracy

  birdnet:
    version: "2.4"
    params:
      min_confidence: 0.1
      lat: null             # deployment latitude for seasonal filtering
      lon: null
```

---

## Web interface

| Page | URL | Description |
|---|---|---|
| Dashboard | `/` | Stats, recent observations, top species, job queue |
| Deployments | `/deployments` | All deployments with status and filter by sensor |
| Deployment detail | `/deployments/{id}` | Media grid, species list, download exports |
| Observations | `/observations` | Query by species, sensor, deployment, date, confidence |
| Jobs | `/jobs` | Processing queue with live log output |
| Models | `/models` | Active AI model versions and parameters |
| API docs | `/docs` | Auto-generated OpenAPI documentation |

---

## Data exports

Download buttons appear on each deployment's detail page once processing is complete.

| Sensor | Standard | Endpoint |
|---|---|---|
| Camera trap | Camtrap DP 1.0 | `GET /download/{id}/camtrap-dp` |
| AudioMoth | Darwin Core Archive | `GET /download/{id}/darwin-core` |

Raw data files (CSVs, JSON caches) are written to `data/deployments/{id}/`.

---

## Adding a new sensor

1. Create `yardmonitor/sensors/<sensor>/pipeline.py` extending `BasePipeline`
2. Add `yardmonitor/sensors/<sensor>/__init__.py`
3. Add an entry in `pipeline_runner.py` → `run()` dispatch
4. Add a model block in `config/models.yaml` if needed
5. Add an entry point in `pyproject.toml`

---

## License

[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) — data outputs.  
Source code: see `LICENSE` file.
