"""Export AudioMoth observations as a Darwin Core Archive (DwC-A).

A DwC-A is the standard exchange format for biodiversity occurrence data:
  https://dwc.tdwg.org/text/

The archive is a ZIP file containing:
  occurrence.csv  — one row per species detection
  meta.xml        — describes the CSV columns using Darwin Core terms
"""

from __future__ import annotations

import csv
import io
import logging
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DWC_META = """\
<?xml version="1.0" encoding="utf-8"?>
<archive xmlns="http://rs.tdwg.org/dwc/text/"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://rs.tdwg.org/dwc/text/
             http://rs.tdwg.org/dwc/text/tdwg_dwc_text.xsd">
  <core encoding="UTF-8"
        fieldsTerminatedBy=","
        linesTerminatedBy="\\n"
        fieldsEnclosedBy="&quot;"
        ignoreHeaderLines="1"
        rowType="http://rs.tdwg.org/dwc/terms/Occurrence">
    <files><location>occurrence.csv</location></files>
    <id index="0"/>
    <field index="1"  term="http://rs.tdwg.org/dwc/terms/basisOfRecord"/>
    <field index="2"  term="http://rs.tdwg.org/dwc/terms/eventDate"/>
    <field index="3"  term="http://rs.tdwg.org/dwc/terms/scientificName"/>
    <field index="4"  term="http://rs.tdwg.org/dwc/terms/vernacularName"/>
    <field index="5"  term="http://rs.tdwg.org/dwc/terms/identificationRemarks"/>
    <field index="6"  term="http://rs.tdwg.org/dwc/terms/identifiedBy"/>
    <field index="7"  term="http://rs.tdwg.org/dwc/terms/decimalLatitude"/>
    <field index="8"  term="http://rs.tdwg.org/dwc/terms/decimalLongitude"/>
    <field index="9"  term="http://rs.tdwg.org/dwc/terms/locationID"/>
    <field index="10" term="http://rs.tdwg.org/dwc/terms/locality"/>
    <field index="11" term="http://rs.tdwg.org/dwc/terms/datasetName"/>
    <field index="12" term="http://rs.tdwg.org/dwc/terms/associatedMedia"/>
  </core>
</archive>
"""

_OCCURRENCE_FIELDS = [
    "occurrenceID",
    "basisOfRecord",
    "eventDate",
    "scientificName",
    "vernacularName",
    "identificationRemarks",
    "identifiedBy",
    "decimalLatitude",
    "decimalLongitude",
    "locationID",
    "locality",
    "datasetName",
    "associatedMedia",
]


class DarwinCoreExporter:
    def __init__(self, config: dict):
        cfg = config.get("darwin_core", {})
        self.dataset_name = cfg.get("project_name", "YardMonitor")

    def export(
        self,
        dep_dir: Path,
        deployment: dict,
        observations: list[dict],
        model_label: str,
    ) -> Path:
        """
        Write a DwC-A ZIP to dep_dir/<dep_id>_dwca.zip.

        observations: list of dicts with keys:
          filename, scientific_name, common_name, confidence,
          start_time, end_time, observed_at
        """
        dep_id = deployment.get("id", "deployment")
        lat = deployment.get("latitude")
        lon = deployment.get("longitude")
        location = deployment.get("location_name", "")

        rows: list[dict] = []
        for obs in observations:
            conf = obs.get("confidence")
            conf_str = f"{conf:.4f}" if conf is not None else ""
            rows.append({
                "occurrenceID": str(uuid.uuid4()),
                "basisOfRecord": "MachineObservation",
                "eventDate": obs.get("observed_at", ""),
                "scientificName": obs.get("scientific_name", ""),
                "vernacularName": obs.get("common_name", ""),
                "identificationRemarks": f"confidence={conf_str}",
                "identifiedBy": model_label,
                "decimalLatitude": "" if lat is None else str(lat),
                "decimalLongitude": "" if lon is None else str(lon),
                "locationID": dep_id,
                "locality": location,
                "datasetName": self.dataset_name,
                "associatedMedia": obs.get("filename", ""),
            })

        out_path = dep_dir / f"{dep_id}_dwca.zip"
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("meta.xml", _DWC_META)
            zf.writestr("occurrence.csv", _rows_to_csv(rows))

        logger.info("Darwin Core Archive written → %s (%d records)", out_path, len(rows))
        return out_path


def _rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_OCCURRENCE_FIELDS, lineterminator="\n")
    writer.writeheader()
    if rows:
        writer.writerows(rows)
    return buf.getvalue()
