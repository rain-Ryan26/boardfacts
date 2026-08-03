from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from flyboard.models import (
    BranchField,
    BranchFieldState,
    FieldCategory,
    ImageEvidenceSeed,
    PdfPageEvidenceSeed,
)

from .models import PdfAcceptedField, PdfAcceptedUnknownNote
from .preparation import PreparedPdfAsset


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    asset: PreparedPdfAsset
    item_kind: Literal["field", "unknown_note"]
    item_index: int
    value: PdfAcceptedField | PdfAcceptedUnknownNote


def collect_drafts(accepted_assets, assets_by_id):
    drafts = []
    for accepted in accepted_assets:
        asset = assets_by_id[accepted.asset_id]
        drafts.extend(
            CandidateDraft(asset, "field", index, item)
            for index, item in enumerate(accepted.fields)
        )
        drafts.extend(
            CandidateDraft(asset, "unknown_note", index, item)
            for index, item in enumerate(accepted.unknown_notes)
        )
    return drafts


def materialize_drafts(drafts, snapshot, context):
    material_order = {item.material_id: item.material_order for item in snapshot.materials}
    field_order = {item.field_code: index for index, item in enumerate(snapshot.field_table.fields)}
    definitions = {item.field_code: item for item in snapshot.field_table.fields}
    ordered = sorted(
        drafts,
        key=lambda item: _draft_key(item, material_order, field_order),
    )
    fields = []
    notes = []
    seeds = []
    seed_ids = set()
    for draft in ordered:
        record = draft.asset.record
        value = draft.value
        if record.asset_path is None:
            raise ValueError("prepared candidate asset path is missing")
        evidence_type = "pdf_page" if record.source_kind == "pdf" else "image"
        evidence_id = context.evidence_id_helper.assign(
            evidence_type=evidence_type,
            source_material_id=record.source_material_id,
            file_path=record.source_ref,
            page_index=record.source_page_number,
            region=value.region,
            source_asset_path=record.asset_path,
            source_image_index=record.source_image_index,
        )
        if evidence_id not in seed_ids:
            seed_ids.add(evidence_id)
            seed_note = value.source_text or (
                value.field_value if draft.item_kind == "field" else value.text
            )
            if record.source_kind == "pdf":
                seeds.append(
                    PdfPageEvidenceSeed(
                        evidence_id=evidence_id,
                        type="pdf_page",
                        source_material_id=record.source_material_id,
                        file_path=record.source_ref,
                        page_index=record.source_page_number,
                        region=value.region,
                        note=seed_note,
                        source_asset_path=record.asset_path,
                    )
                )
            else:
                seeds.append(
                    ImageEvidenceSeed(
                        evidence_id=evidence_id,
                        type="image",
                        source_material_id=record.source_material_id,
                        file_path=record.source_ref,
                        page_index=None,
                        region=value.region,
                        note=seed_note,
                        source_asset_path=record.asset_path,
                        source_image_index=record.source_image_index,
                    )
                )
        source_ref = _branch_source_ref(record, snapshot)
        if draft.item_kind == "field":
            definition = definitions[value.field_code]
            fields.append(
                BranchField(
                    field_code=value.field_code,
                    field_name=definition.field_name,
                    field_value=value.field_value,
                    unit=value.unit,
                    category=definition.category,
                    branch_state=BranchFieldState(value.branch_state),
                    source="pdf_image_ai",
                    source_material_id=record.source_material_id,
                    source_ref=source_ref,
                    evidence_ids=[evidence_id],
                    tool_finding_ids=[],
                    error_ids=[],
                    check_note=value.check_note,
                )
            )
        else:
            notes.append(
                BranchField(
                    field_code="unknown_note_candidate",
                    field_name="未知说明候选",
                    field_value=value.text,
                    unit=None,
                    category=FieldCategory.BOARD,
                    branch_state=BranchFieldState.UNKNOWN,
                    source="pdf_image_ai",
                    source_material_id=record.source_material_id,
                    source_ref=source_ref,
                    evidence_ids=[evidence_id],
                    tool_finding_ids=[],
                    error_ids=[],
                    check_note=value.check_note,
                )
            )
    return fields, notes, seeds


def _branch_source_ref(record, snapshot):
    if record.source_kind == "pdf":
        number = snapshot.groups.pdf_inputs.index(record.source_material_id) + 1
        return f"PDF_{number} 第{record.source_page_number}页"
    return f"IMAGE_{record.source_image_index}"


def _draft_key(draft, material_order, field_order):
    record = draft.asset.record
    region = draft.value.region
    region_key = (
        (0, region.y, region.x, region.height, region.width)
        if region is not None
        else (1, 0, 0, 0, 0)
    )
    source_index = record.source_page_number or record.source_image_index
    candidate_order = (
        field_order[draft.value.field_code] if draft.item_kind == "field" else draft.item_index
    )
    return (
        material_order[record.source_material_id],
        source_index,
        region_key,
        candidate_order,
        draft.item_index,
    )
