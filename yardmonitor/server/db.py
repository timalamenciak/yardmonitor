"""SQLite persistence layer for the YardMonitor server."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Generator, Optional

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS deployments (
    id              TEXT PRIMARY KEY,
    sensor_type     TEXT NOT NULL,
    sensor_id       TEXT,
    location_name   TEXT,
    latitude        REAL,
    longitude       REAL,
    start_dt        TEXT,
    end_dt          TEXT,
    uploaded_at     TEXT NOT NULL,
    processed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'uploading',
    file_count      INTEGER DEFAULT 0,
    meta_json       TEXT
);

CREATE TABLE IF NOT EXISTS media (
    id              TEXT PRIMARY KEY,
    deployment_id   TEXT NOT NULL REFERENCES deployments(id),
    filename        TEXT NOT NULL,
    relative_path   TEXT NOT NULL,
    captured_at     TEXT,
    file_size       INTEGER,
    mime_type       TEXT,
    exif_json       TEXT,
    UNIQUE(deployment_id, filename)
);

CREATE TABLE IF NOT EXISTS observations (
    id              TEXT PRIMARY KEY,
    deployment_id   TEXT NOT NULL REFERENCES deployments(id),
    media_id        TEXT NOT NULL REFERENCES media(id),
    observed_at     TEXT,
    sensor_type     TEXT NOT NULL,
    detection_type  TEXT,
    scientific_name TEXT,
    common_name     TEXT,
    confidence      REAL,
    taxon_id        TEXT,
    detector_model  TEXT,
    classifier_model TEXT,
    bbox_json       TEXT,
    meta_json       TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    deployment_id   TEXT NOT NULL REFERENCES deployments(id),
    status          TEXT NOT NULL DEFAULT 'queued',
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT,
    log             TEXT,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_deployment  ON observations(deployment_id);
CREATE INDEX IF NOT EXISTS idx_obs_species     ON observations(scientific_name);
CREATE INDEX IF NOT EXISTS idx_obs_date        ON observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_type        ON observations(detection_type);
CREATE INDEX IF NOT EXISTS idx_media_dep       ON media(deployment_id);
CREATE INDEX IF NOT EXISTS idx_jobs_dep        ON jobs(deployment_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
"""


class Database:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "yardmonitor.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Deployments ───────────────────────────────────────────────────────

    def create_deployment(
        self,
        sensor_type: str,
        sensor_id: str = "",
        location_name: str = "",
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        deployment_id: Optional[str] = None,
    ) -> str:
        dep_id = deployment_id or str(uuid.uuid4())[:8] + "_" + (
            "".join(c if c.isalnum() else "_" for c in location_name)[:20].strip("_")
            or sensor_type
        )
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO deployments
                   (id, sensor_type, sensor_id, location_name, latitude, longitude,
                    uploaded_at, status, file_count)
                   VALUES (?,?,?,?,?,?,?,?,0)""",
                (dep_id, sensor_type, sensor_id, location_name,
                 latitude, longitude, datetime.now().isoformat(), "uploading"),
            )
        return dep_id

    def get_deployment(self, dep_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM deployments WHERE id = ?", (dep_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_deployments(
        self,
        sensor_type: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[Any] = []
        if sensor_type:
            clauses.append("sensor_type = ?")
            params.append(sensor_type)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM deployments {where} ORDER BY uploaded_at DESC LIMIT ?",
                params + [limit],
            ).fetchall()
        return [dict(r) for r in rows]

    def update_deployment(self, dep_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE deployments SET {sets} WHERE id = ?",
                list(kwargs.values()) + [dep_id],
            )

    def increment_file_count(self, dep_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE deployments SET file_count = file_count + 1 WHERE id = ?",
                (dep_id,),
            )

    # ── Media ─────────────────────────────────────────────────────────────

    def upsert_media(
        self,
        deployment_id: str,
        filename: str,
        relative_path: str,
        captured_at: Optional[str] = None,
        file_size: Optional[int] = None,
        mime_type: str = "",
        exif: Optional[dict] = None,
    ) -> str:
        media_id = str(uuid.uuid4())
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM media WHERE deployment_id = ? AND filename = ?",
                (deployment_id, filename),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE media SET captured_at=?, file_size=?, mime_type=?,
                       exif_json=? WHERE id=?""",
                    (captured_at, file_size, mime_type,
                     json.dumps(exif) if exif else None, existing["id"]),
                )
                return existing["id"]
            conn.execute(
                """INSERT INTO media
                   (id, deployment_id, filename, relative_path, captured_at,
                    file_size, mime_type, exif_json)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (media_id, deployment_id, filename, relative_path,
                 captured_at, file_size, mime_type,
                 json.dumps(exif) if exif else None),
            )
        return media_id

    def list_media(self, deployment_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM media WHERE deployment_id = ? ORDER BY captured_at",
                (deployment_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Observations ──────────────────────────────────────────────────────

    def insert_observation(
        self,
        deployment_id: str,
        media_id: str,
        sensor_type: str,
        detection_type: str,
        scientific_name: str = "",
        common_name: str = "",
        confidence: Optional[float] = None,
        taxon_id: str = "",
        detector_model: str = "",
        classifier_model: str = "",
        observed_at: Optional[str] = None,
        bbox: Optional[list] = None,
        meta: Optional[dict] = None,
    ) -> str:
        obs_id = str(uuid.uuid4())
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO observations
                   (id, deployment_id, media_id, observed_at, sensor_type,
                    detection_type, scientific_name, common_name, confidence,
                    taxon_id, detector_model, classifier_model, bbox_json, meta_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (obs_id, deployment_id, media_id,
                 observed_at or datetime.now().isoformat(),
                 sensor_type, detection_type, scientific_name, common_name,
                 confidence, taxon_id, detector_model, classifier_model,
                 json.dumps(bbox) if bbox else None,
                 json.dumps(meta) if meta else None),
            )
        return obs_id

    def delete_observations_for_deployment(self, deployment_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM observations WHERE deployment_id = ?", (deployment_id,)
            )

    def query_observations(
        self,
        sensor_type: Optional[str] = None,
        deployment_id: Optional[str] = None,
        scientific_name: Optional[str] = None,
        detection_type: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        min_confidence: Optional[float] = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict], int]:
        clauses: list[str] = []
        params: list[Any] = []

        if sensor_type:
            clauses.append("o.sensor_type = ?")
            params.append(sensor_type)
        if deployment_id:
            clauses.append("o.deployment_id = ?")
            params.append(deployment_id)
        if scientific_name:
            clauses.append("(o.scientific_name LIKE ? OR o.common_name LIKE ?)")
            params += [f"%{scientific_name}%", f"%{scientific_name}%"]
        if detection_type:
            clauses.append("o.detection_type = ?")
            params.append(detection_type)
        if from_date:
            clauses.append("o.observed_at >= ?")
            params.append(from_date)
        if to_date:
            clauses.append("o.observed_at <= ?")
            params.append(to_date + "T23:59:59")
        if min_confidence is not None:
            clauses.append("o.confidence >= ?")
            params.append(min_confidence)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        offset = (page - 1) * per_page

        with self._conn() as conn:
            total = conn.execute(
                f"""SELECT COUNT(*) FROM observations o
                    JOIN media m ON m.id = o.media_id
                    JOIN deployments d ON d.id = o.deployment_id
                    {where}""",
                params,
            ).fetchone()[0]

            rows = conn.execute(
                f"""SELECT o.*, m.filename, m.relative_path, m.captured_at AS media_ts,
                           d.location_name, d.sensor_type AS dep_sensor_type
                    FROM observations o
                    JOIN media m ON m.id = o.media_id
                    JOIN deployments d ON d.id = o.deployment_id
                    {where}
                    ORDER BY o.observed_at DESC
                    LIMIT ? OFFSET ?""",
                params + [per_page, offset],
            ).fetchall()

        return [dict(r) for r in rows], total

    def get_species_summary(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT scientific_name, common_name, COUNT(*) AS count,
                          MAX(observed_at) AS last_seen
                   FROM observations
                   WHERE scientific_name != '' AND detection_type = 'animal'
                   GROUP BY scientific_name
                   ORDER BY count DESC""",
            ).fetchall()
        return [dict(r) for r in rows]

    def get_dashboard_stats(self) -> dict:
        with self._conn() as conn:
            n_dep = conn.execute("SELECT COUNT(*) FROM deployments").fetchone()[0]
            n_media = conn.execute("SELECT COUNT(*) FROM media").fetchone()[0]
            n_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            n_species = conn.execute(
                "SELECT COUNT(DISTINCT scientific_name) FROM observations "
                "WHERE scientific_name != ''"
            ).fetchone()[0]
            recent = conn.execute(
                """SELECT o.*, m.filename, d.location_name
                   FROM observations o
                   JOIN media m ON m.id = o.media_id
                   JOIN deployments d ON d.id = o.deployment_id
                   WHERE o.detection_type IN ('animal','audio_species')
                   ORDER BY o.observed_at DESC LIMIT 20""",
            ).fetchall()
            top_species = conn.execute(
                """SELECT scientific_name, common_name, COUNT(*) AS count
                   FROM observations
                   WHERE scientific_name != ''
                   GROUP BY scientific_name
                   ORDER BY count DESC LIMIT 10""",
            ).fetchall()
        return {
            "deployments": n_dep,
            "media": n_media,
            "observations": n_obs,
            "species": n_species,
            "recent_observations": [dict(r) for r in recent],
            "top_species": [dict(r) for r in top_species],
        }

    def get_deployment_stats(self, dep_id: str) -> dict:
        with self._conn() as conn:
            n_media = conn.execute(
                "SELECT COUNT(*) FROM media WHERE deployment_id = ?", (dep_id,)
            ).fetchone()[0]
            n_obs = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE deployment_id = ?", (dep_id,)
            ).fetchone()[0]
            species = conn.execute(
                """SELECT scientific_name, common_name, COUNT(*) AS count
                   FROM observations
                   WHERE deployment_id = ? AND scientific_name != ''
                   GROUP BY scientific_name ORDER BY count DESC""",
                (dep_id,),
            ).fetchall()
        return {
            "media_count": n_media,
            "observation_count": n_obs,
            "species": [dict(r) for r in species],
        }

    # ── Jobs ──────────────────────────────────────────────────────────────

    def create_job(self, job_id: str, deployment_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO jobs (id, deployment_id, status, created_at)
                   VALUES (?,?,?,?)""",
                (job_id, deployment_id, "queued", datetime.now().isoformat()),
            )

    def update_job(self, job_id: str, **kwargs: Any) -> None:
        if not kwargs:
            return
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE jobs SET {sets} WHERE id = ?",
                list(kwargs.values()) + [job_id],
            )

    def append_job_log(self, job_id: str, line: str) -> None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT log FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            existing = row["log"] or "" if row else ""
            conn.execute(
                "UPDATE jobs SET log = ? WHERE id = ?",
                (existing + line + "\n", job_id),
            )

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT j.*, d.location_name, d.sensor_type
                   FROM jobs j JOIN deployments d ON d.id = j.deployment_id
                   WHERE j.id = ?""",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT j.*, d.location_name, d.sensor_type
                   FROM jobs j JOIN deployments d ON d.id = j.deployment_id
                   ORDER BY j.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                if d.get("started_at") and d.get("completed_at"):
                    delta = (
                        datetime.fromisoformat(d["completed_at"][:19])
                        - datetime.fromisoformat(d["started_at"][:19])
                    )
                    d["duration_s"] = int(delta.total_seconds())
                else:
                    d["duration_s"] = None
            except Exception:
                d["duration_s"] = None
            result.append(d)
        return result
