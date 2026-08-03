from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone

import pytest

from flyboard.models import build_current_field_table
from flyboard.services import RuleSetService, ServiceError

NOW = datetime(2026, 7, 18, tzinfo=timezone.utc)


def rule_set_payload(
    rule_set_id: str = "factory_rules", rule_set_version: str = "2026.07"
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "rule_set_id": rule_set_id,
        "rule_set_name": f"规则 {rule_set_id}",
        "rule_set_version": rule_set_version,
        "description": "测试规则库",
        "rules": [
            {
                "rule_id": "rule_1",
                "name": "板厚检查",
                "target_field_code": "board_thickness_mm",
                "instruction": "检查板厚是否符合要求。",
            }
        ],
    }


def test_rule_set_registration_is_atomic_and_snapshot_revalidates_content(tmp_path) -> None:
    source = tmp_path / "rules.json"
    source.write_text(json.dumps(rule_set_payload(), ensure_ascii=False), encoding="utf-8")
    service = RuleSetService(tmp_path, clock=lambda: NOW)
    result = service.register_file(str(source), build_current_field_table())
    assert result.validation_status == "valid"
    assert service.list_rule_sets()[0].rule_set_id == "factory_rules"

    snapshot = service.prepare_snapshot("factory_rules", build_current_field_table())
    assert snapshot.rules[0].rule_id == "rule_1"

    summary = result.rule_set_summary
    assert summary is not None
    managed = tmp_path.joinpath(*summary.source_path.split("/"))
    changed = rule_set_payload()
    changed["description"] = "内容已变化"
    managed.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ServiceError) as error:
        service.prepare_snapshot("factory_rules", build_current_field_table())
    assert error.value.code == "rule_set_content_changed"


def test_invalid_rule_set_does_not_create_registry(tmp_path) -> None:
    source = tmp_path / "rules.json"
    payload = rule_set_payload()
    payload["rules"][0]["target_field_code"] = "not_a_field"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    service = RuleSetService(tmp_path, clock=lambda: NOW)
    result = service.register_file(str(source), build_current_field_table())
    assert result.validation_status == "invalid"
    assert result.errors[0].code == "rule_target_field_unknown"
    assert not (tmp_path / "rule_sets" / "registry.json").exists()


def test_rule_set_registration_treats_non_empty_name_as_opaque_text(tmp_path) -> None:
    source = tmp_path / "rules.json"
    payload = rule_set_payload()
    payload["rules"][0]["name"] = "板厚检查 rule_1"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    service = RuleSetService(tmp_path, clock=lambda: NOW)

    result = service.register_file(str(source), build_current_field_table())

    assert result.validation_status == "valid"
    assert result.errors == []
    assert result.rule_set_summary is not None


def test_rule_name_check_does_not_treat_short_id_as_arbitrary_substring(tmp_path) -> None:
    source = tmp_path / "rules.json"
    payload = rule_set_payload()
    payload["rules"][0]["rule_id"] = "a"
    payload["rules"][0]["name"] = "Panel check"
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    service = RuleSetService(tmp_path, clock=lambda: NOW)

    result = service.register_file(str(source), build_current_field_table())

    assert result.validation_status == "valid"


def test_rule_set_registry_reader_rejects_reparse_target(tmp_path) -> None:
    source = tmp_path / "rules.json"
    source.write_text(json.dumps(rule_set_payload(), ensure_ascii=False), encoding="utf-8")
    service = RuleSetService(tmp_path, clock=lambda: NOW)
    service.register_file(str(source), build_current_field_table())
    external = tmp_path / "external-registry.json"
    external.write_bytes(service.registry_path.read_bytes())
    service.registry_path.unlink()
    try:
        service.registry_path.symlink_to(external)
    except OSError:
        pytest.skip("file symlinks are unavailable on this Windows host")

    with pytest.raises(ServiceError) as error:
        service.list_rule_sets()
    assert error.value.code == "managed_path_reparse_point"


def test_registry_atomic_update_failure_preserves_previous_summary(tmp_path, monkeypatch) -> None:
    import flyboard.services.rule_sets as rule_sets_module

    source = tmp_path / "rules.json"
    source.write_text(json.dumps(rule_set_payload(), ensure_ascii=False), encoding="utf-8")
    service = RuleSetService(tmp_path, clock=lambda: NOW)
    first = service.register_file(str(source), build_current_field_table())
    previous = first.rule_set_summary
    assert previous is not None

    source.write_text(
        json.dumps(rule_set_payload(rule_set_version="2026.08"), ensure_ascii=False),
        encoding="utf-8",
    )
    original_replace = rule_sets_module.atomic_replace_bytes

    def fail_registry(path, content):
        if path == service.registry_path:
            raise OSError("registry replace interrupted")
        return original_replace(path, content)

    monkeypatch.setattr(rule_sets_module, "atomic_replace_bytes", fail_registry)

    with pytest.raises(ServiceError) as error:
        service.register_file(str(source), build_current_field_table())

    assert error.value.code == "rule_set_registry_write_failed"
    current = service.get_rule_set_summary("factory_rules")
    assert current == previous


def test_concurrent_rule_set_registrations_do_not_lose_updates(tmp_path) -> None:
    service = RuleSetService(tmp_path, clock=lambda: NOW)
    field_table = build_current_field_table()
    paths = []
    for rule_set_id in ("rules_a", "rules_b"):
        path = tmp_path / f"{rule_set_id}.json"
        path.write_text(
            json.dumps(rule_set_payload(rule_set_id=rule_set_id), ensure_ascii=False),
            encoding="utf-8",
        )
        paths.append(path)

    gate = threading.Barrier(3)
    results = []
    failures = []

    def register(path):
        try:
            gate.wait()
            results.append(service.register_file(str(path), field_table))
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=register, args=(path,)) for path in paths]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(2)

    assert failures == []
    assert len(results) == 2
    assert all(result.validation_status == "valid" for result in results)
    assert {item.rule_set_id for item in service.list_rule_sets()} == {"rules_a", "rules_b"}


def test_concurrent_registration_and_snapshot_return_one_complete_version(tmp_path) -> None:
    field_table = build_current_field_table()
    service = RuleSetService(tmp_path, clock=lambda: NOW)
    source_v1 = tmp_path / "rules-v1.json"
    source_v1.write_text(
        json.dumps(rule_set_payload(rule_set_version="2026.07"), ensure_ascii=False),
        encoding="utf-8",
    )
    first = service.register_file(str(source_v1), field_table)
    first_summary = first.rule_set_summary
    assert first_summary is not None

    source_v2 = tmp_path / "rules-v2.json"
    source_v2.write_text(
        json.dumps(rule_set_payload(rule_set_version="2026.08"), ensure_ascii=False),
        encoding="utf-8",
    )
    v2_digest = hashlib.sha256(source_v2.read_bytes()).hexdigest()
    gate = threading.Barrier(3)
    snapshots = []
    updates = []
    failures = []

    def snapshot():
        try:
            gate.wait()
            snapshots.append(service.prepare_snapshot("factory_rules", field_table))
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    def update():
        try:
            gate.wait()
            updates.append(service.register_file(str(source_v2), field_table))
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=snapshot), threading.Thread(target=update)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(2)

    assert failures == []
    assert len(snapshots) == 1
    assert len(updates) == 1
    snapshot_result = snapshots[0]
    assert (
        snapshot_result.rule_set_version,
        snapshot_result.content_sha256,
    ) in {
        (first_summary.rule_set_version, first_summary.content_sha256),
        ("2026.08", v2_digest),
    }
    assert service.get_rule_set_summary("factory_rules").rule_set_version == "2026.08"
