from __future__ import annotations

import threading

from flyboard.models import Region


class RunEvidenceIdHelper:
    """Run-local deterministic EvidenceSeed id allocation and key consistency guard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids_by_key: dict[tuple, tuple[str, tuple]] = {}
        self._counts = {"pdf_page": 0, "image": 0, "gerber_artifact": 0}

    def assign(
        self,
        *,
        evidence_type: str,
        source_material_id: str,
        file_path: str,
        page_index: int | None,
        region: Region | None,
        source_asset_path: str | None = None,
        source_image_index: int | None = None,
        artifact_kind: str | None = None,
        artifact_path: str | None = None,
    ) -> str:
        region_key = (
            None
            if region is None
            else (region.x, region.y, region.width, region.height, region.unit)
        )
        if evidence_type == "pdf_page":
            if page_index is None or source_asset_path is None:
                raise ValueError("pdf_page id allocation requires page and source asset")
            if (
                source_image_index is not None
                or artifact_kind is not None
                or artifact_path is not None
            ):
                raise ValueError("pdf_page id allocation contains forbidden locator fields")
            key = (evidence_type, source_material_id, page_index, region_key)
            fingerprint = (file_path, source_asset_path)
            prefix = "ev_pdf_"
        elif evidence_type == "image":
            if page_index is not None or source_asset_path is None or source_image_index is None:
                raise ValueError("image id allocation requires source asset and image index")
            if artifact_kind is not None or artifact_path is not None:
                raise ValueError("image id allocation contains forbidden locator fields")
            key = (evidence_type, source_material_id, source_image_index, region_key)
            fingerprint = (file_path, source_asset_path)
            prefix = "ev_image_"
        elif evidence_type == "gerber_artifact":
            if (
                page_index is not None
                or region is not None
                or source_asset_path is not None
                or source_image_index is not None
                or artifact_kind not in {"result", "adapter_summary"}
                or artifact_path is None
            ):
                raise ValueError("gerber id allocation requires its complete artifact locator")
            key = (evidence_type, source_material_id, artifact_kind, artifact_path)
            fingerprint = (file_path,)
            prefix = "ev_gerber_"
        else:
            raise ValueError("unsupported evidence type")

        with self._lock:
            existing = self._ids_by_key.get(key)
            if existing is not None:
                evidence_id, existing_fingerprint = existing
                if existing_fingerprint != fingerprint:
                    raise ValueError("same evidence key has conflicting source locator")
                return evidence_id
            self._counts[evidence_type] += 1
            evidence_id = f"{prefix}{self._counts[evidence_type]:03d}"
            self._ids_by_key[key] = (evidence_id, fingerprint)
            return evidence_id
