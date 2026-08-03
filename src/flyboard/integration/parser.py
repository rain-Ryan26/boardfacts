from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from .models import FieldTask, ItemRejection, SubmittedItem


@dataclass(frozen=True, slots=True)
class ParsedBatch:
    envelope_locatable: bool
    accepted: tuple[SubmittedItem, ...]
    rejections: tuple[ItemRejection, ...]
    affected_field_codes: tuple[str, ...]


class IntegrationResponseParser:
    def parse(self, response_text: str, *, batch_id: str, tasks: list[FieldTask]) -> ParsedBatch:
        requested = {task.field_code: task for task in tasks}
        try:
            decoded = json.loads(response_text)
        except (json.JSONDecodeError, TypeError):
            return _envelope_failure(requested, "invalid_json", "整合响应不是完整 JSON。")
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"batch_id", "items"}
            or decoded.get("batch_id") != batch_id
            or not isinstance(decoded.get("items"), list)
        ):
            return _envelope_failure(
                requested, "invalid_envelope", "整合响应 envelope 或 batch_id 无效。"
            )

        raw_by_code: dict[str, list[object]] = {}
        rejections: list[ItemRejection] = []
        for raw in decoded["items"]:
            code = raw.get("field_code") if isinstance(raw, dict) else None
            if not isinstance(code, str) or code not in requested:
                rejections.append(
                    ItemRejection(
                        field_code=code if isinstance(code, str) else None,
                        reason_code="unexpected_item",
                        message="响应包含无法定位到当前批次的字段项。",
                    )
                )
                continue
            raw_by_code.setdefault(code, []).append(raw)

        accepted: list[SubmittedItem] = []
        affected: list[str] = []
        for field_code, task in requested.items():
            items = raw_by_code.get(field_code, [])
            if len(items) != 1:
                reason = "missing_item" if not items else "duplicate_item"
                rejections.append(
                    ItemRejection(
                        field_code=field_code,
                        reason_code=reason,
                        message="请求字段必须恰好返回一个结果项。",
                    )
                )
                affected.append(field_code)
                continue
            try:
                item = SubmittedItem.model_validate(items[0])
                _validate_item(item, task)
            except (ValidationError, ValueError, TypeError):
                rejections.append(
                    ItemRejection(
                        field_code=field_code,
                        reason_code="item_invalid",
                        message="字段项未通过关系、状态或值合同。",
                    )
                )
                affected.append(field_code)
                continue
            accepted.append(item)

        return ParsedBatch(
            envelope_locatable=True,
            accepted=tuple(accepted),
            rejections=tuple(rejections),
            affected_field_codes=tuple(affected),
        )


def _validate_item(item: SubmittedItem, task: FieldTask) -> None:
    _validate_display_text(item)
    if task.operation == "diagnostic":
        if (
            item.field_value is not None
            or item.candidate_relation is not None
            or item.suggested_field_state != "missing"
        ):
            raise ValueError("diagnostic task requires a missing result without a value")
        return

    if item.field_value is None or not item.field_value.strip():
        raise ValueError("candidate task requires a non-empty field value")
    count = len(task.ordered_candidates)
    states = {candidate.branch_state for candidate in task.ordered_candidates}
    if task.operation == "summarize" or count == 1:
        if item.candidate_relation is not None:
            raise ValueError("single candidate and summarize tasks require null relation")
    elif item.candidate_relation is None:
        raise ValueError("multi-candidate compare requires a relation")
    if item.candidate_relation == "different" and item.suggested_field_state != "conflict":
        raise ValueError("different requires conflict")
    if item.candidate_relation == "uncertain" and item.suggested_field_state != "unknown":
        raise ValueError("uncertain requires unknown")
    if (
        task.operation == "compare"
        and count > 1
        and item.candidate_relation == "equivalent"
        and item.suggested_field_state == "confirmed"
        and "clear_now" not in states
    ):
        raise ValueError("equivalent candidates need one clear input before confirmation")
    if "conflict" in states and item.suggested_field_state == "confirmed":
        raise ValueError("upstream conflict cannot be confirmed")
    if task.operation == "summarize" and "unknown" in states:
        if item.suggested_field_state == "confirmed":
            raise ValueError("unknown summarize input cannot be confirmed")
    if count == 1 and task.ordered_candidates[0].branch_state == "unknown":
        if item.suggested_field_state == "confirmed":
            raise ValueError("single unknown candidate cannot be confirmed")
    if item.suggested_field_state == "missing":
        raise ValueError("real candidate task cannot become missing")


def _validate_display_text(item: SubmittedItem) -> None:
    if not item.append_check_note.strip():
        raise ValueError("append_check_note must not be blank")
    if not item.check_conclusion.strip():
        raise ValueError("check_conclusion must not be blank")


def _envelope_failure(requested, reason_code, message) -> ParsedBatch:
    return ParsedBatch(
        envelope_locatable=False,
        accepted=(),
        rejections=(ItemRejection(field_code=None, reason_code=reason_code, message=message),),
        affected_field_codes=tuple(requested),
    )
