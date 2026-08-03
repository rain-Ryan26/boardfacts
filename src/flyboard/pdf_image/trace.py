from __future__ import annotations

import json

from flyboard.errors import ServiceError
from flyboard.models import AccessLevel, ArtifactConsumer, ArtifactIndexEntry, PhaseCode
from flyboard.services._support import utc_now

from .models import (
    PdfBatchCapacity,
    PdfBatchSourceSpan,
    PdfBatchTraceEvent,
)


class PdfBatchTrace:
    def __init__(self, run_id: str, settings, source_ids, context) -> None:
        self._run_id = run_id
        self._settings = settings
        self._source_ids = list(source_ids)
        self._context = context
        self._events: list[PdfBatchTraceEvent] = []
        self._write_error = None
        self._target_failed = False

    def add(
        self,
        batch,
        event_type,
        *,
        attempt=1,
        outcome,
        artifact_paths=(),
        child_batch_ids=(),
        error_ids=(),
    ) -> None:
        first, last, kind = _source_span(batch.assets)
        self._events.append(
            PdfBatchTraceEvent(
                schema_version="1.0",
                run_id=self._run_id,
                timestamp=utc_now(),
                event_type=event_type,
                batch_id=batch.batch_id,
                parent_batch_id=batch.parent_batch_id,
                source_material_id=batch.source_material_id,
                asset_ids=[item.record.asset_id for item in batch.assets],
                source_span=PdfBatchSourceSpan(kind=kind, first_index=first, last_index=last),
                attempt=attempt,
                capacity=PdfBatchCapacity(
                    max_image_count=self._settings.max_image_count,
                    request_budget_units=self._settings.request_budget_units,
                    estimated_request_units=estimated_units(batch.assets),
                    page_text_char_limit=self._settings.page_text_char_limit,
                ),
                outcome=outcome,
                artifact_paths=list(artifact_paths),
                child_batch_ids=list(child_batch_ids),
                error_ids=list(error_ids),
            )
        )
        self._persist()

    def write(self):
        return self._write_error

    def _persist(self) -> None:
        if self._target_failed:
            return
        content = b"".join(
            json.dumps(
                event.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
            for event in self._events
        )
        entry = ArtifactIndexEntry(
            path="pdf/model/batch_trace.jsonl",
            kind="pdf_batch_trace",
            producer_phase=PhaseCode.BRANCH_ANALYSIS,
            consumers=[
                ArtifactConsumer.WORKFLOW,
                ArtifactConsumer.DEBUG,
                ArtifactConsumer.REGRESSION,
            ],
            access_level=AccessLevel.RESTRICTED_DEBUG,
            source_material_ids=self._source_ids,
        )
        try:
            self._context.artifact_writer.write_content(content, "application/octet-stream", entry)
        except ServiceError as exc:
            if exc.error_id is None:
                raise
            if self._write_error is None:
                self._write_error = self._context.diagnostics.resolve_error(exc.error_id)
            if exc.code != "artifact_index_update_failed":
                self._target_failed = True


def estimated_units(assets) -> int:
    return sum(
        len(item.content) + (len(item.page_text.text.encode("utf-8")) if item.page_text else 0)
        for item in assets
    )


def _source_span(assets):
    first = assets[0].record
    if first.source_kind == "pdf":
        indexes = [item.record.source_page_number for item in assets]
        return min(indexes), max(indexes), "pdf_pages"
    return first.source_image_index, first.source_image_index, "image"
