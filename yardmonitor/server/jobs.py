"""Background job queue — one worker thread per configured slot.

Jobs are persisted in the SQLite `jobs` table so their status survives restarts.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .db import Database

logger = logging.getLogger(__name__)


class JobQueue:
    """Thread-pool backed queue; job state written to DB after each status change."""

    def __init__(self, db: Database, runner: "ServerPipelineRunner", max_workers: int = 1):  # noqa: F821
        self._db = db
        self._runner = runner
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ym-worker")
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def submit(self, deployment_id: str) -> str:
        job_id = str(uuid.uuid4())
        self._db.create_job(job_id, deployment_id)
        future = self._executor.submit(self._execute, job_id, deployment_id)
        with self._lock:
            self._futures[job_id] = future
        logger.info("Job %s queued for deployment %s", job_id, deployment_id)
        return job_id

    def _execute(self, job_id: str, deployment_id: str) -> None:
        self._db.update_job(job_id, status="running", started_at=datetime.now().isoformat())
        self._db.update_deployment(deployment_id, status="processing")
        try:
            dep = self._db.get_deployment(deployment_id)
            if dep is None:
                raise ValueError(f"Deployment {deployment_id} not found")
            data_dir = Path(self._runner.data_dir)
            dep_dir = data_dir / "deployments" / deployment_id
            self._runner.run(job_id, deployment_id, dep_dir, dep["sensor_type"])
            self._db.update_job(
                job_id, status="complete", completed_at=datetime.now().isoformat()
            )
            self._db.update_deployment(
                deployment_id, status="complete", processed_at=datetime.now().isoformat()
            )
            logger.info("Job %s complete", job_id)
        except Exception as exc:
            logger.exception("Job %s failed: %s", job_id, exc)
            self._db.update_job(
                job_id,
                status="failed",
                completed_at=datetime.now().isoformat(),
                error=str(exc),
            )
            self._db.update_deployment(deployment_id, status="failed")
        finally:
            with self._lock:
                self._futures.pop(job_id, None)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
        logger.info("Job queue shut down")
