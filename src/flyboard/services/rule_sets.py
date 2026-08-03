from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from flyboard.errors import ServiceError
from flyboard.models import (
    FieldTable,
    RuleSet,
    RuleSetSnapshot,
    RuleSetSummary,
    RuleSetValidationResult,
    ValidationMessage,
)
from flyboard.repositories._filesystem import (
    atomic_replace_bytes,
    ensure_no_reparse_points,
    json_bytes,
    managed_child,
    read_json_object,
    validate_identifier,
)
from flyboard.services._support import utc_now


class _RuleSetRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["1.0"]
    rule_sets: list[RuleSetSummary]


class RuleSetService:
    def __init__(
        self,
        storage_root: Path,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.storage_root = storage_root
        self.rule_sets_root = storage_root / "rule_sets"
        self.registry_path = self.rule_sets_root / "registry.json"
        self.rule_sets_root.mkdir(parents=True, exist_ok=True)
        ensure_no_reparse_points(self.storage_root, self.rule_sets_root)
        self.clock = clock
        self._mutex = threading.Lock()

    def _read_registry(self) -> _RuleSetRegistry:
        ensure_no_reparse_points(self.rule_sets_root, self.registry_path)
        if not self.registry_path.exists():
            return _RuleSetRegistry(schema_version="1.0", rule_sets=[])
        data = read_json_object(
            self.registry_path,
            missing_code="rule_set_registry_unavailable",
            invalid_code="rule_set_registry_invalid",
        )
        if "schema_version" not in data:
            raise ServiceError("artifact_schema_version_missing")
        if data["schema_version"] != "1.0":
            raise ServiceError("artifact_schema_version_unsupported")
        try:
            return _RuleSetRegistry.model_validate(data)
        except ValidationError as exc:
            raise ServiceError("rule_set_registry_invalid") from exc

    def list_rule_sets(self) -> list[RuleSetSummary]:
        with self._mutex:
            return [item.model_copy(deep=True) for item in self._read_registry().rule_sets]

    def get_rule_set_summary(self, rule_set_id: str) -> RuleSetSummary:
        with self._mutex:
            registry = self._read_registry()
            summary = next(
                (item for item in registry.rule_sets if item.rule_set_id == rule_set_id), None
            )
            if summary is None:
                raise ServiceError("rule_set_not_found")
            return summary.model_copy(deep=True)

    def register_file(self, rule_set_path: str, field_table: FieldTable) -> RuleSetValidationResult:
        with self._mutex:
            path = Path(rule_set_path)
            try:
                content = path.read_bytes()
            except OSError:
                return self._invalid("rule_set_source_unreadable", "规则库源文件无法读取", None)
            rule_set, errors = self._validate_content(content, field_table)
            if errors:
                return RuleSetValidationResult(
                    validation_status="invalid",
                    rule_set_summary=None,
                    errors=errors,
                    warnings=[],
                )
            assert rule_set is not None
            try:
                validate_identifier(rule_set.rule_set_id)
            except ServiceError:
                return self._invalid(
                    "rule_set_id_invalid",
                    "rule_set_id 不能用于受管存储标识",
                    "rule_set_id",
                )
            digest = hashlib.sha256(content).hexdigest()
            registry = self._read_registry()
            existing = next(
                (item for item in registry.rule_sets if item.rule_set_id == rule_set.rule_set_id),
                None,
            )
            now = self.clock()
            source_path = f"rule_sets/sources/{rule_set.rule_set_id}/{digest}.json"
            summary = RuleSetSummary(
                rule_set_id=rule_set.rule_set_id,
                rule_set_name=rule_set.rule_set_name,
                rule_set_version=rule_set.rule_set_version,
                schema_version=rule_set.schema_version,
                description=rule_set.description,
                source_kind="system_rule_set_file",
                source_path=source_path,
                content_sha256=digest,
                rule_count=len(rule_set.rules),
                registered_at=existing.registered_at if existing is not None else now,
                updated_at=now,
                last_validated_at=now,
            )
            source_target = managed_child(self.storage_root, source_path, locator=True)
            try:
                atomic_replace_bytes(source_target, content)
                updated = [
                    item for item in registry.rule_sets if item.rule_set_id != rule_set.rule_set_id
                ]
                if existing is None:
                    updated.append(summary)
                else:
                    position = next(
                        index
                        for index, item in enumerate(registry.rule_sets)
                        if item.rule_set_id == rule_set.rule_set_id
                    )
                    updated.insert(position, summary)
                atomic_replace_bytes(
                    self.registry_path,
                    json_bytes(_RuleSetRegistry(schema_version="1.0", rule_sets=updated)),
                )
            except ServiceError:
                raise
            except OSError as exc:
                raise ServiceError("rule_set_registry_write_failed") from exc
            return RuleSetValidationResult(
                validation_status="valid",
                rule_set_summary=summary,
                errors=[],
                warnings=[],
            )

    def prepare_snapshot(self, rule_set_id: str, field_table: FieldTable) -> RuleSetSnapshot:
        with self._mutex:
            registry = self._read_registry()
            summary = next(
                (item for item in registry.rule_sets if item.rule_set_id == rule_set_id), None
            )
            if summary is None:
                raise ServiceError("rule_set_not_found")
            source = managed_child(self.storage_root, summary.source_path, locator=True)
            try:
                content = source.read_bytes()
            except OSError as exc:
                raise ServiceError("rule_set_invalid") from exc
            rule_set, errors = self._validate_content(content, field_table)
            if errors or rule_set is None:
                raise ServiceError("rule_set_invalid")
            digest = hashlib.sha256(content).hexdigest()
            if digest != summary.content_sha256:
                raise ServiceError("rule_set_content_changed")
            if (
                rule_set.rule_set_id != summary.rule_set_id
                or rule_set.rule_set_name != summary.rule_set_name
                or rule_set.rule_set_version != summary.rule_set_version
                or rule_set.schema_version != summary.schema_version
            ):
                raise ServiceError("rule_set_content_changed")
            return RuleSetSnapshot(
                rule_set_id=summary.rule_set_id,
                rule_set_name=summary.rule_set_name,
                rule_set_version=summary.rule_set_version,
                schema_version=summary.schema_version,
                source_kind=summary.source_kind,
                source_path=summary.source_path,
                content_sha256=summary.content_sha256,
                snapshot_created_at=self.clock(),
                rules=[rule.model_copy(deep=True) for rule in rule_set.rules],
            )

    @staticmethod
    def _validate_content(
        content: bytes, field_table: FieldTable
    ) -> tuple[RuleSet | None, list[ValidationMessage]]:
        try:
            decoded = content.decode("utf-8")
            raw = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, [
                ValidationMessage(
                    code="rule_set_json_invalid",
                    message="规则库必须是 UTF-8 JSON 对象",
                    path=None,
                )
            ]
        if type(raw) is not dict:
            return None, [
                ValidationMessage(
                    code="rule_set_schema_invalid",
                    message="规则库顶层必须是对象",
                    path=None,
                )
            ]
        if "schema_version" not in raw:
            return None, [
                ValidationMessage(
                    code="rule_set_schema_version_missing",
                    message="规则库缺少 schema_version",
                    path="schema_version",
                )
            ]
        if raw["schema_version"] != "1.0":
            return None, [
                ValidationMessage(
                    code="rule_set_schema_version_unsupported",
                    message="规则库 schema_version 不受支持",
                    path="schema_version",
                )
            ]
        try:
            rule_set = RuleSet.model_validate(raw)
        except ValidationError as exc:
            messages = [
                ValidationMessage(
                    code="rule_set_schema_invalid",
                    message=error["msg"],
                    path=".".join(str(part) for part in error["loc"]) or None,
                )
                for error in exc.errors(include_url=False)
            ]
            return None, messages
        field_codes = {field.field_code for field in field_table.fields}
        errors: list[ValidationMessage] = []
        for index, rule in enumerate(rule_set.rules):
            if rule.target_field_code not in field_codes:
                errors.append(
                    ValidationMessage(
                        code="rule_target_field_unknown",
                        message="规则目标不在当前字段表中",
                        path=f"rules.{index}.target_field_code",
                    )
                )
        return (None, errors) if errors else (rule_set, [])

    @staticmethod
    def _invalid(code: str, message: str, path: str | None) -> RuleSetValidationResult:
        return RuleSetValidationResult(
            validation_status="invalid",
            rule_set_summary=None,
            errors=[ValidationMessage(code=code, message=message, path=path)],
            warnings=[],
        )
