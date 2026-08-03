import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from flyboard.api import create_http_app
from flyboard.app import build_application
from flyboard.config import AppConfig
from flyboard.model_calls import ModelCallClient


def _client(tmp_path):
    application = build_application(
        AppConfig(tmp_path / "storage"),
        recover_on_startup=False,
    )
    return application, TestClient(create_http_app(application))


def test_gui_static_contract_uses_selected_project_workspace_and_light_theme(tmp_path):
    application, client = _client(tmp_path)
    try:
        with client:
            index = client.get("/")
            css = client.get("/static/app.css")
            profile_script = client.get("/static/layer-definition-profile.js")
            gerber_workspace_script = client.get("/static/gerber-workspace.js")
            workflow_script = client.get("/static/workflow-flow.js")
            material_upload_script = client.get("/static/material-upload.js")
            material_selection_script = client.get("/static/material-selection.js")
            data_sections_script = client.get("/static/project-data-sections.js")
            script = client.get("/static/app.js")

            assert (
                index.status_code
                == css.status_code
                == profile_script.status_code
                == gerber_workspace_script.status_code
                == workflow_script.status_code
                == material_upload_script.status_code
                == material_selection_script.status_code
                == data_sections_script.status_code
                == script.status_code
                == 200
            )
            assert 'href="/static/app.css?v=20260720-overview-workflow-priority"' in index.text
            assert (
                'src="/static/layer-definition-profile.js?v=20260720-layer-profile"' in index.text
            )
            assert 'src="/static/workflow-flow.js?v=20260720-total-duration"' in index.text
            assert 'src="/static/material-upload.js?v=20260720-upload-closure"' in index.text
            assert 'src="/static/material-selection.js?v=20260720-material-selection"' in index.text
            assert 'src="/static/project-data-sections.js?v=20260720-data-sections"' in index.text
            assert 'src="/static/gerber-workspace.js?v=20260721-gerber-closure"' in index.text
            assert 'src="/static/app.js?v=20260721-gerber-closure"' in index.text
            assert 'id="project-workspace" hidden' in index.text
            assert 'id="material-files" type="file" multiple hidden' in index.text
            assert (
                'id="material-folder" type="file" webkitdirectory directory multiple hidden'
                in index.text
            )
            assert 'id="material-drop-overlay"' in index.text
            assert 'id="material-path"' not in index.text
            assert "droppedUploadSelection" in material_upload_script.text
            assert "preserveMaterialSelection" in material_selection_script.text
            assert "mergeUploadedMaterialSelection" in material_selection_script.text
            assert "groupProjectDataRows" in data_sections_script.text
            assert "项目信息" in data_sections_script.text
            assert "板件设计信息" in data_sections_script.text
            assert "groupProjectDataRows(state.data, state.dataFilter)" in script.text
            assert ".data-section-heading" in css.text
            assert 'id="export-project-data"' in index.text
            assert 'class="data-print-header"' in index.text
            assert "window.print()" in script.text
            assert 'class="evidence-column"' in script.text
            assert 'class="evidence-actions"' in script.text
            assert "body.printing-project-data .evidence-column" in css.text
            assert "body.printing-project-data .evidence-actions" in css.text
            assert "@page { margin: 12mm; }" in css.text
            assert "上传完成，正在解压并识别资料" in script.text
            assert "项目列表刷新失败" in script.text
            assert "webkitGetAsEntry" in material_upload_script.text
            assert 'body.append("relative_paths", entry.relativePath)' in script.text
            assert ".material-drop-target" in css.text
            assert 'id="open-rule-set"' in index.text
            assert 'id="rule-set-dialog"' in index.text
            assert 'id="rule-set-form"' in index.text
            assert 'id="import-layer-profile"' in index.text
            assert 'id="layer-profile-file"' in index.text
            assert "buildDraftLayers" in profile_script.text
            assert "LayerDefinitionProfile.buildDraftLayers" in script.text
            assert "WorkflowFlow.render" in script.text
            assert "flow-connector-split" in workflow_script.text
            assert 'id="flow-total"' in index.text
            assert 'class="grid two overview-input-grid"' in index.text
            assert 'class="card flow-card"' in index.text
            assert 'id="run-problems-card"' in index.text
            assert 'activateTab("overview")' in script.text
            assert 'classList.toggle("has-run", Boolean(state.run))' in script.text
            assert "#tab-overview.has-run .flow-card" in css.text
            assert "运行问题与工具告警" in index.text
            assert "诊断与工具发现" not in index.text
            assert 'id="evidence-zoom-out"' in index.text
            assert 'id="evidence-zoom-reset"' in index.text
            assert 'id="evidence-zoom-in"' in index.text
            assert ".evidence-dialog > .dialog-close" in css.text
            assert "position: sticky" in css.text
            assert '<th class="evidence-column">证据</th>' in script.text
            assert "证据 / 诊断" not in script.text
            assert "const linked =" not in script.text
            assert 'aria-label="查看证据 ${index + 1}"' in script.text
            assert "releaseEvidenceObjectUrl" in script.text
            assert "closeEvidenceDialog" in script.text
            assert 'addEventListener("cancel", releaseEvidenceObjectUrl)' in script.text
            assert 'addEventListener("close", releaseEvidenceObjectUrl)' not in script.text
            assert 'api("/rule-sets", { method: "POST"' in script.text
            assert 'result.validation_status === "invalid"' in script.text
            assert "[hidden] { display: none !important; }" in css.text
            assert "color-scheme: light;" in css.text
            assert "--accent: #a98562;" in css.text
            assert ".flow-node.is-running" in css.text
            assert ".flow-node.is-completed" in css.text
            assert ".flow-node.is-error" in css.text
            assert "#3ee88f" not in script.text
            assert (
                '$("empty-state").hidden = true; $("project-workspace").hidden = false;'
                in script.text
            )
            assert "state.run = null; state.data = null;" in script.text
            assert "if (!force && renderKey === state.gerberRenderKey) return;" in script.text
            assert "resolveSelectedJob(state.selectedJob, state.preparations)" in script.text
            assert "本次运行暂无可用 Gerber 预览" in script.text
            assert "renderGerber(true);" in script.text
            assert "resolveReads(" in script.text
            assert "definitionReadOnly(" in script.text
    finally:
        application.close(0)


def test_rule_set_registration_http_keeps_only_successfully_validated_resources(tmp_path):
    application, client = _client(tmp_path)
    source = tmp_path / "factory-rules.json"
    payload = {
        "schema_version": "1.0",
        "rule_set_id": "factory_rules",
        "rule_set_name": "工厂规则库",
        "rule_set_version": "2026.07",
        "description": "生产规则",
        "rules": [
            {
                "rule_id": "board_thickness",
                "name": "板厚检查",
                "target_field_code": "board_thickness_mm",
                "instruction": "检查板厚是否符合要求。",
            }
        ],
    }
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        with client:
            registered = client.post("/rule-sets", json={"rule_set_path": str(source)})
            assert registered.status_code == 200
            result = registered.json()
            assert result["validation_status"] == "valid"
            summary = result["rule_set_summary"]
            assert summary["rule_set_id"] == "factory_rules"
            assert client.get("/rule-sets").json() == [summary]

            payload["rule_set_version"] = "2026.08"
            payload["rules"][0]["target_field_code"] = "not_a_field"
            source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            rejected = client.post("/rule-sets", json={"rule_set_path": str(source)})
            assert rejected.status_code == 200
            assert rejected.json()["validation_status"] == "invalid"
            assert rejected.json()["errors"][0]["code"] == "rule_target_field_unknown"
            assert client.get("/rule-sets").json() == [summary]

            payload["rules"][0]["target_field_code"] = "board_thickness_mm"
            payload["rules"][0]["name"] = "板厚检查 board_thickness"
            source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            accepted_name = client.post("/rule-sets", json={"rule_set_path": str(source)})
            assert accepted_name.status_code == 200
            accepted_result = accepted_name.json()
            assert accepted_result["validation_status"] == "valid"
            assert accepted_result["errors"] == []
            assert accepted_result["rule_set_summary"]["rule_set_version"] == "2026.08"
            assert client.get("/rule-sets").json() == [accepted_result["rule_set_summary"]]
    finally:
        application.close(0)


def test_composition_uses_real_model_client_when_endpoint_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("FLYBOARD_QWEN_API_BASE", "https://example.test/v1")
    monkeypatch.setenv("FLYBOARD_QWEN_API_KEY", "synthetic-secret")
    application = build_application(
        AppConfig(tmp_path / "configured-storage"), recover_on_startup=False
    )
    try:
        assert isinstance(application.run_service.model_client, ModelCallClient)
    finally:
        application.close(0)


def test_composition_rejects_partial_model_endpoint_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("FLYBOARD_QWEN_API_BASE", "https://example.test/v1")
    monkeypatch.delenv("FLYBOARD_QWEN_API_KEY", raising=False)

    with pytest.raises(ValueError, match="FLYBOARD_QWEN_API_KEY"):
        build_application(AppConfig(tmp_path / "partial-storage"), recover_on_startup=False)


def test_project_material_and_structured_error_surface(tmp_path):
    application, client = _client(tmp_path)
    unsupported = tmp_path / "readme.txt"
    unsupported.write_text("not a PCB input", encoding="utf-8")
    try:
        with client:
            root = client.get("/")
            assert root.status_code == 200
            assert "BoardFacts 工程工作台" in root.text

            invalid = client.post("/projects", json={"project_name": "x", "extra": True})
            assert invalid.status_code == 400
            assert invalid.json()["kind"] == "api_problem"
            assert invalid.json()["code"] == "invalid_request"

            created = client.post("/projects", json={"project_name": "S7 API"})
            assert created.status_code == 200
            project = created.json()
            assert project["latest_run_id"] is None
            assert client.get("/projects").json() == [project]

            registered = client.post(
                f"/projects/{project['project_id']}/materials",
                json={"input_path": str(unsupported)},
            )
            assert registered.status_code == 200
            material = registered.json()["materials"][0]
            assert material["kind"] == "unsupported"
            assert registered.json()["failures"] == []
            assert client.get(f"/projects/{project['project_id']}/materials").json() == [material]

            missing = client.get("/projects/prj_missing/materials")
            assert missing.status_code == 404
            assert missing.json() == {
                "kind": "api_problem",
                "code": "project_not_found",
                "message": "项目不存在。",
                "status": 404,
                "error_id": None,
                "details": {"project_id": "prj_missing"},
            }
    finally:
        application.close(0)


def test_material_upload_http_registers_files_and_safe_zip(tmp_path):
    application, client = _client(tmp_path)
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("docs/note.txt", b"inside archive")
    try:
        with client:
            project = client.post("/projects", json={"project_name": "GUI upload"}).json()
            base = f"/projects/{project['project_id']}/materials/upload"

            uploaded = client.post(
                base,
                data={"input_mode": "files", "relative_paths": "readme.txt"},
                files={"files": ("ignored-client-name.txt", b"plain note", "text/plain")},
            )
            assert uploaded.status_code == 200
            first = uploaded.json()["materials"][0]
            assert first["display_name"] == "readme.txt"
            assert first["kind"] == "unsupported"

            archived = client.post(
                base,
                data={"input_mode": "archive", "relative_paths": "customer.zip"},
                files={"files": ("customer.zip", archive_bytes.getvalue(), "application/zip")},
            )
            assert archived.status_code == 200
            names = [item["display_name"] for item in archived.json()["materials"]]
            assert names == ["readme.txt", "note.txt"]
            assert "customer.zip" not in names
    finally:
        application.close(0)


def test_material_upload_http_preserves_directory_manifest(tmp_path):
    application, client = _client(tmp_path)
    try:
        with client:
            project = client.post("/projects", json={"project_name": "Folder upload"}).json()
            response = client.post(
                f"/projects/{project['project_id']}/materials/upload",
                files=[
                    ("input_mode", (None, "directory")),
                    ("relative_paths", (None, "docs/one.txt")),
                    ("relative_paths", (None, "drawings/two.txt")),
                    ("files", ("ignored-one.txt", b"one", "text/plain")),
                    ("files", ("ignored-two.txt", b"two", "text/plain")),
                ],
            )
            assert response.status_code == 200
            assert [item["display_name"] for item in response.json()["materials"]] == [
                "one.txt",
                "two.txt",
            ]
    finally:
        application.close(0)


def test_material_upload_http_rejects_unsafe_manifest_and_invalid_zip(tmp_path):
    application, client = _client(tmp_path)
    try:
        with client:
            project = client.post("/projects", json={"project_name": "Unsafe upload"}).json()
            base = f"/projects/{project['project_id']}/materials/upload"

            unsafe = client.post(
                base,
                data={"input_mode": "directory", "relative_paths": "../outside.txt"},
                files={"files": ("outside.txt", b"escape", "text/plain")},
            )
            assert unsafe.status_code == 400
            assert unsafe.json()["code"] == "invalid_upload_manifest"

            invalid_zip = client.post(
                base,
                data={"input_mode": "archive", "relative_paths": "bad.zip"},
                files={"files": ("bad.zip", b"not a zip", "application/zip")},
            )
            assert invalid_zip.status_code == 400
            assert invalid_zip.json()["code"] == "archive_invalid"
            assert client.get(f"/projects/{project['project_id']}/materials").json() == []
    finally:
        application.close(0)


def test_material_upload_http_rejects_non_closed_forms_and_zip_in_files(tmp_path):
    application, client = _client(tmp_path)
    try:
        with client:
            project = client.post("/projects", json={"project_name": "Closed upload"}).json()
            base = f"/projects/{project['project_id']}/materials/upload"

            duplicate_mode = client.post(
                base,
                files=[
                    ("input_mode", (None, "files")),
                    ("input_mode", (None, "files")),
                    ("relative_paths", (None, "note.txt")),
                    ("files", ("note.txt", b"note", "text/plain")),
                ],
            )
            unknown_field = client.post(
                base,
                files=[
                    ("input_mode", (None, "files")),
                    ("relative_paths", (None, "note.txt")),
                    ("unexpected", (None, "ignored")),
                    ("files", ("note.txt", b"note", "text/plain")),
                ],
            )
            missing_path = client.post(
                base,
                data={"input_mode": "files"},
                files={"files": ("note.txt", b"note", "text/plain")},
            )
            zip_as_file = client.post(
                base,
                data={"input_mode": "files", "relative_paths": "customer.zip"},
                files={"files": ("customer.zip", b"not inspected", "application/zip")},
            )

            for response in (duplicate_mode, unknown_field, missing_path, zip_as_file):
                assert response.status_code == 400
                assert response.json()["code"] == "invalid_upload_manifest"
            assert client.get(f"/projects/{project['project_id']}/materials").json() == []
    finally:
        application.close(0)


def test_malformed_multipart_is_invalid_request_not_method_not_allowed(tmp_path):
    application, client = _client(tmp_path)
    try:
        with client:
            project = client.post("/projects", json={"project_name": "Malformed upload"}).json()
            response = client.post(
                f"/projects/{project['project_id']}/materials/upload",
                content=b"missing multipart boundary",
                headers={"content-type": "multipart/form-data"},
            )
            assert response.status_code == 400
            assert response.json()["code"] == "invalid_request"
    finally:
        application.close(0)


def test_material_upload_http_enforces_body_limit_before_registration(tmp_path):
    application = build_application(
        AppConfig(tmp_path / "storage"),
        recover_on_startup=False,
    )
    client = TestClient(create_http_app(application, upload_body_limit_bytes=64))
    try:
        with client:
            project = client.post("/projects", json={"project_name": "Limited upload"}).json()
            response = client.post(
                f"/projects/{project['project_id']}/materials/upload",
                data={"input_mode": "files", "relative_paths": "large.txt"},
                files={"files": ("large.txt", b"x" * 128, "text/plain")},
            )
            assert response.status_code == 413
            assert response.json()["code"] == "upload_too_large"
            assert client.get(f"/projects/{project['project_id']}/materials").json() == []
    finally:
        application.close(0)


def test_uploaded_material_enters_new_run_snapshot_and_later_upload_does_not_change_it(tmp_path):
    application, client = _client(tmp_path)
    try:
        with client:
            project = client.post("/projects", json={"project_name": "Upload snapshot"}).json()
            upload_url = f"/projects/{project['project_id']}/materials/upload"
            first_upload = client.post(
                upload_url,
                data={"input_mode": "files", "relative_paths": "first.txt"},
                files={"files": ("first.txt", b"first", "text/plain")},
            )
            first_material = first_upload.json()["materials"][0]
            created = client.post(
                f"/projects/{project['project_id']}/runs",
                json={"material_ids": [first_material["material_id"]], "rule_set_id": None},
            )
            assert created.status_code == 200
            run_id = created.json()["run_id"]
            snapshot_path = (
                tmp_path
                / "storage"
                / "projects"
                / project["project_id"]
                / "runs"
                / run_id
                / "run_input_snapshot.json"
            )
            before = snapshot_path.read_bytes()
            snapshot = json.loads(before.decode("utf-8"))
            assert [item["material_id"] for item in snapshot["materials"]] == [
                first_material["material_id"]
            ]

            second_upload = client.post(
                upload_url,
                data={"input_mode": "files", "relative_paths": "second.txt"},
                files={"files": ("second.txt", b"second", "text/plain")},
            )
            assert second_upload.status_code == 200
            assert len(second_upload.json()["materials"]) == 2
            assert snapshot_path.read_bytes() == before
    finally:
        application.close(0)


def test_run_response_is_projection_and_data_list_is_structured(tmp_path):
    application, client = _client(tmp_path)
    unsupported = tmp_path / "unknown.bin"
    unsupported.write_bytes(b"unsupported")
    try:
        with client:
            project = client.post("/projects", json={"project_name": "不可处理资料"}).json()
            material = client.post(
                f"/projects/{project['project_id']}/materials",
                json={"input_path": str(unsupported)},
            ).json()["materials"][0]
            response = client.post(
                f"/projects/{project['project_id']}/runs",
                json={"material_ids": [material["material_id"]], "rule_set_id": None},
            )
            assert response.status_code == 200
            run = response.json()
            assert "artifact_index_path" not in run
            assert "project_data_list_path" not in run

            deadline = time.monotonic() + 3
            while run["status"] not in {"failed", "stopped", "completed", "completed_with_errors"}:
                assert time.monotonic() < deadline
                run = client.get(f"/projects/{project['project_id']}/runs/{run['run_id']}").json()
                time.sleep(0.01)
            assert run["status"] == "failed"
            data = client.get(f"/projects/{project['project_id']}/runs/{run['run_id']}/data-list")
            assert data.status_code == 200
            assert data.json()["kind"] == "data_list_unavailable"
            assert data.json()["reason_code"] == "no_processable_materials"
    finally:
        application.close(0)


def test_openapi_contains_only_the_formal_product_resources(tmp_path):
    application, client = _client(tmp_path)
    expected = {
        "/rule-sets",
        "/rule-sets/{rule_set_id}",
        "/projects",
        "/projects/{project_id}",
        "/projects/{project_id}/materials",
        "/projects/{project_id}/materials/upload",
        "/projects/{project_id}/runs",
        "/projects/{project_id}/runs/{run_id}/stop",
        "/projects/{project_id}/runs/{run_id}",
        "/projects/{project_id}/runs/{run_id}/data-list",
        "/projects/{project_id}/runs/{run_id}/tool-findings",
        "/projects/{project_id}/runs/{run_id}/evidence/{evidence_id}",
        "/projects/{project_id}/runs/{run_id}/gerber-preparations",
        "/projects/{project_id}/runs/{run_id}/gerber-preparations/{job_material_id}/layers/{layer_id}/preview",
        "/projects/{project_id}/runs/{run_id}/layer-definitions",
        "/projects/{project_id}/runs/{run_id}/layer-definitions/{job_material_id}",
        "/projects/{project_id}/runs/{run_id}/layer-definitions/{job_material_id}/confirm",
    }
    try:
        with client:
            assert set(client.get("/openapi.json").json()["paths"]) == expected
    finally:
        application.close(0)
