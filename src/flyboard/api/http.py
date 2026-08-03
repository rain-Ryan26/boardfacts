from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from flyboard.app import Application, build_application, register_rule_set
from flyboard.errors import ServiceError
from flyboard.ingestion import DEFAULT_UPLOAD_LIMITS, UploadedFileSource
from flyboard.models import (
    ApiProblem,
    LayerDefinitionItem,
    LayerDefinitionRequest,
)

_LOGGER = logging.getLogger(__name__)
_GUI_ROOT = Path(__file__).with_name("static")

_NOT_FOUND = {
    "project_not_found",
    "run_not_found",
    "rule_set_not_found",
    "evidence_not_found",
    "gerber_preview_not_found",
}
_BAD_REQUEST = {
    "invalid_project_name",
    "input_path_not_found",
    "input_path_invalid",
    "invalid_upload_manifest",
    "archive_invalid",
    "archive_unsafe",
    "invalid_material_selection",
    "duplicate_material_id",
    "material_not_found",
    "invalid_evidence_view",
}
_PAYLOAD_TOO_LARGE = {
    "upload_too_large",
    "archive_limit_exceeded",
}
_CONFLICT = {
    "active_run_exists",
    "run_state_conflict",
    "gerber_preparation_not_ready",
    "tool_findings_not_ready",
    "project_latest_run_conflict",
    "project_deletion_conflict",
    "rule_set_content_changed",
}
_MESSAGES = {
    "invalid_request": "请求结构或参数无效。",
    "invalid_project_name": "项目名称不能为空。",
    "project_not_found": "项目不存在。",
    "run_not_found": "当前项目不存在该产品运行。",
    "rule_set_not_found": "规则库不存在。",
    "active_run_exists": "当前已有运行中的任务。",
    "input_path_not_found": "输入路径不存在。",
    "input_path_invalid": "输入路径必须是可读文件或目录。",
    "invalid_upload_manifest": "上传文件、输入方式或相对路径无效。",
    "upload_too_large": "上传内容超过允许大小。",
    "upload_storage_failed": "服务端未能安全保存上传内容。",
    "archive_invalid": "ZIP 压缩包无效或已经损坏。",
    "archive_unsafe": "ZIP 压缩包包含不安全的路径、链接或成员。",
    "archive_limit_exceeded": "ZIP 压缩包超过允许的文件数或解压容量。",
    "invalid_material_selection": "必须选择至少一项资料。",
    "duplicate_material_id": "资料选择中存在重复标识。",
    "material_not_found": "选择的资料不存在。",
    "invalid_evidence_view": "证据 view 只允许 metadata 或 content。",
    "evidence_not_found": "证据不存在。",
    "gerber_preview_not_found": "Gerber 层预览不存在或不可读取。",
    "gerber_preparation_not_ready": "Gerber job 尚未形成可确认 preparation。",
    "run_state_conflict": "当前运行状态不接受该操作。",
    "tool_findings_not_ready": "工具发现尚未完成汇总。",
    "artifact_index_unavailable": "运行 artifact 索引不可用。",
    "project_data_list_unavailable": "项目数据列表不可用。",
    "tool_findings_unavailable": "工具发现结果不可用。",
    "evidence_index_unavailable": "证据索引不可用。",
    "artifact_content_unavailable": "受控证据内容不可用。",
    "gerber_preparations_unavailable": "Gerber preparation 结果不可用。",
    "layer_definitions_unavailable": "层定义结果不可用。",
    "artifact_schema_version_missing": "artifact 缺少 schema 版本。",
    "artifact_schema_version_unsupported": "artifact schema 版本不受支持。",
    "artifact_schema_invalid": "artifact 结构无效。",
    "internal_server_error": "服务端未能完成本次请求。",
}


class _RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateProjectRequest(_RequestModel):
    project_name: StrictStr


class DeleteProjectRequest(_RequestModel):
    delete_artifacts: StrictBool


class RegisterMaterialsRequest(_RequestModel):
    input_path: StrictStr


class RegisterRuleSetRequest(_RequestModel):
    rule_set_path: StrictStr


class CreateRunRequest(_RequestModel):
    material_ids: list[StrictStr]
    rule_set_id: StrictStr | None = None


class EmptyRequest(_RequestModel):
    pass


def _status_for(code: str) -> int:
    if code in _NOT_FOUND:
        return 404
    if code in _BAD_REQUEST:
        return 400
    if code in _PAYLOAD_TOO_LARGE:
        return 413
    if code in _CONFLICT:
        return 409
    return 500


def _problem(
    code: str,
    status: int,
    *,
    message: str | None = None,
    error_id: str | None = None,
    details: dict | None = None,
) -> ApiProblem:
    return ApiProblem(
        kind="api_problem",
        code=code,
        message=message or _MESSAGES.get(code, "服务端未能完成本次请求。"),
        status=status,
        error_id=error_id,
        details=details or {},
    )


def _json_problem(problem: ApiProblem) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json"),
    )


class _UploadBodyLimitMiddleware:
    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        limited = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and path.startswith("/projects/")
            and path.endswith("/materials/upload")
        )
        if not limited:
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self.max_bytes:
                    response = _json_problem(_problem("upload_too_large", 413))
                    await response(scope, receive, send)
                    return
            except ValueError:
                response = _json_problem(_problem("invalid_upload_manifest", 400))
                await response(scope, receive, send)
                return
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise ServiceError("upload_too_large")
            return message

        await self.app(scope, limited_receive, send)


def _request_details(request: Request) -> dict[str, str]:
    allowed = {"project_id", "run_id", "job_material_id", "layer_id", "evidence_id"}
    return {
        key: str(value)
        for key, value in request.path_params.items()
        if key in allowed and isinstance(value, str)
    }


def _validated_upload_form(
    form: FormData,
) -> tuple[str, list[StarletteUploadFile], list[str]]:
    items = list(form.multi_items())
    allowed = {"input_mode", "files", "relative_paths"}
    if any(key not in allowed for key, _value in items):
        raise ServiceError("invalid_upload_manifest")
    modes = [value for key, value in items if key == "input_mode"]
    files = [value for key, value in items if key == "files"]
    relative_paths = [value for key, value in items if key == "relative_paths"]
    if (
        len(modes) != 1
        or not isinstance(modes[0], str)
        or not files
        or len(files) > DEFAULT_UPLOAD_LIMITS.max_input_files
        or len(files) != len(relative_paths)
        or any(not isinstance(item, StarletteUploadFile) for item in files)
        or any(not isinstance(item, str) for item in relative_paths)
    ):
        raise ServiceError("invalid_upload_manifest")
    return modes[0], files, relative_paths


def _index_for(application: Application, project_id: str, run_id: str):
    summary = application.artifacts.read_run_summary(project_id, run_id)
    if summary.artifact_index_path is None:
        raise ServiceError("artifact_index_unavailable")
    return application.artifacts.read_artifact_index(
        project_id,
        run_id,
        summary.artifact_index_path,
    )


def create_http_app(
    application: Application | None = None,
    *,
    upload_body_limit_bytes: int = DEFAULT_UPLOAD_LIMITS.max_multipart_body_bytes,
) -> FastAPI:
    owned_application = application is None
    runtime = application or build_application()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owned_application:
                runtime.close()

    app = FastAPI(title="BoardFacts", version="0.1.0", lifespan=lifespan)
    app.add_middleware(_UploadBodyLimitMiddleware, max_bytes=upload_body_limit_bytes)
    app.state.flyboard = runtime
    app.mount("/static", StaticFiles(directory=_GUI_ROOT), name="static")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = {
            "errors": [
                {
                    "location": [str(part) for part in error["loc"]],
                    "type": error["type"],
                    "message": error["msg"],
                }
                for error in exc.errors()
            ]
        }
        code = (
            "invalid_upload_manifest"
            if request.method == "POST" and request.url.path.endswith("/materials/upload")
            else "invalid_request"
        )
        return _json_problem(_problem(code, 400, details=details))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        status = exc.status_code
        if status == 400:
            code, message = "invalid_request", "请求结构或参数无效。"
        elif status == 404:
            code, message = "resource_not_found", "请求资源不存在。"
        elif status == 405:
            code, message = "method_not_allowed", "请求方法不受支持。"
        elif 400 <= status < 500:
            code, message = "invalid_request", "请求无法处理。"
        else:
            code, message = "internal_server_error", "服务端未能完成本次请求。"
        return _json_problem(_problem(code, status, message=message))

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        details: dict = _request_details(request)
        if exc.code == "active_run_exists":
            details = {
                "active_project_id": exc.active_project_id,
                "active_run_id": exc.active_run_id,
                "run_summary": exc.run_summary.model_dump(mode="json"),
            }
        return _json_problem(
            _problem(
                exc.code,
                _status_for(exc.code),
                error_id=exc.error_id,
                details=details,
            )
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        _LOGGER.exception("unhandled API error for %s", request.url.path, exc_info=exc)
        return _json_problem(_problem("internal_server_error", 500))

    @app.get("/", include_in_schema=False)
    def workbench():
        return FileResponse(_GUI_ROOT / "index.html", media_type="text/html")

    @app.get("/rule-sets")
    def list_rule_sets():
        return runtime.rule_set_service.list_rule_sets()

    @app.post("/rule-sets")
    def create_rule_set(request: RegisterRuleSetRequest):
        return register_rule_set(runtime, request.rule_set_path)

    @app.get("/rule-sets/{rule_set_id}")
    def get_rule_set(rule_set_id: str):
        return runtime.rule_set_service.get_rule_set_summary(rule_set_id)

    @app.post("/projects")
    def create_project(request: CreateProjectRequest):
        return runtime.project_service.create_project(request.project_name)

    @app.get("/projects")
    def list_projects():
        return runtime.project_service.list_projects()

    @app.delete("/projects/{project_id}")
    def delete_project(project_id: str, request: DeleteProjectRequest):
        return runtime.project_service.delete_project(project_id, request.delete_artifacts)

    @app.get("/projects/{project_id}/materials")
    def get_materials(project_id: str):
        return runtime.project_service.get_materials(project_id)

    @app.post("/projects/{project_id}/materials")
    def register_materials(project_id: str, request: RegisterMaterialsRequest):
        return runtime.material_service.register_materials(project_id, request.input_path)

    @app.post("/projects/{project_id}/materials/upload")
    async def upload_materials(
        request: Request,
        project_id: str,
        input_mode: Annotated[str, Form()],
        files: Annotated[list[UploadFile], File()],
        relative_paths: Annotated[list[str], Form()],
    ):
        form = await request.form()
        closed_mode, closed_files, closed_paths = _validated_upload_form(form)
        if (
            closed_mode != input_mode
            or closed_files != list(files)
            or closed_paths != list(relative_paths)
        ):
            raise ServiceError("invalid_upload_manifest")
        sources = [
            UploadedFileSource(relative_path=relative_path, stream=upload.file)
            for upload, relative_path in zip(closed_files, closed_paths, strict=True)
        ]
        return await run_in_threadpool(
            runtime.material_service.register_uploaded_materials,
            project_id,
            closed_mode,
            sources,
        )

    @app.post("/projects/{project_id}/runs")
    def create_run(project_id: str, request: CreateRunRequest):
        return runtime.run_service.create_run(
            project_id,
            list(request.material_ids),
            request.rule_set_id,
        )

    @app.post("/projects/{project_id}/runs/{run_id}/stop")
    def stop_run(project_id: str, run_id: str, _request: EmptyRequest):
        return runtime.run_service.request_stop(project_id, run_id)

    @app.get("/projects/{project_id}/runs/{run_id}")
    def get_run(project_id: str, run_id: str):
        return runtime.run_service.get_summary(project_id, run_id)

    @app.get("/projects/{project_id}/runs/{run_id}/data-list")
    def get_data_list(project_id: str, run_id: str):
        return runtime.run_service.get_data_list_view(project_id, run_id)

    @app.get("/projects/{project_id}/runs/{run_id}/tool-findings")
    def get_tool_findings(project_id: str, run_id: str):
        return runtime.run_service.get_tool_findings_view(project_id, run_id)

    @app.get("/projects/{project_id}/runs/{run_id}/evidence/{evidence_id}")
    def get_evidence(
        project_id: str,
        run_id: str,
        evidence_id: str,
        view: str = Query(default="metadata"),
    ):
        if view not in {"metadata", "content"}:
            raise ServiceError("invalid_evidence_view")
        with runtime.projects.guard_product_run(project_id, run_id):
            index = _index_for(runtime, project_id, run_id)
            entries = [entry for entry in index.entries if entry.kind == "evidence_index"]
            if len(entries) != 1:
                raise ServiceError("evidence_index_unavailable")
            evidence_index = runtime.artifacts.read_evidence_index(
                project_id,
                run_id,
                entries[0],
            )
            if view == "metadata":
                return runtime.evidence_service.get(
                    project_id,
                    run_id,
                    evidence_index,
                    evidence_id,
                )
            content, media_type = runtime.evidence_service.get_content(
                project_id,
                run_id,
                index,
                evidence_index,
                evidence_id,
            )
            return Response(content=content, media_type=media_type)

    @app.get("/projects/{project_id}/runs/{run_id}/gerber-preparations")
    def get_gerber_preparations(project_id: str, run_id: str):
        with runtime.projects.guard_product_run(project_id, run_id):
            index = _index_for(runtime, project_id, run_id)
            return runtime.artifacts.read_gerber_preparations(project_id, run_id, index)

    @app.get(
        "/projects/{project_id}/runs/{run_id}/gerber-preparations/"
        "{job_material_id}/layers/{layer_id}/preview"
    )
    def get_gerber_preview(
        project_id: str,
        run_id: str,
        job_material_id: str,
        layer_id: str,
    ):
        with runtime.projects.guard_product_run(project_id, run_id):
            index = _index_for(runtime, project_id, run_id)
            content, media_type = runtime.artifacts.read_gerber_layer_preview(
                project_id,
                run_id,
                index,
                job_material_id,
                layer_id,
            )
            return Response(content=content, media_type=media_type)

    @app.get("/projects/{project_id}/runs/{run_id}/layer-definitions")
    def get_layer_definitions(project_id: str, run_id: str):
        with runtime.projects.guard_product_run(project_id, run_id):
            index = _index_for(runtime, project_id, run_id)
            return runtime.artifacts.read_layer_definitions(project_id, run_id, index)

    @app.put("/projects/{project_id}/runs/{run_id}/layer-definitions/{job_material_id}")
    def save_layer_definitions(
        project_id: str,
        run_id: str,
        job_material_id: str,
        request: LayerDefinitionRequest,
    ):
        if (
            request.project_id != project_id
            or request.run_id != run_id
            or request.job_material_id != job_material_id
        ):
            raise ServiceError("run_state_conflict")
        return runtime.run_service.save_layer_definition_draft(
            project_id,
            run_id,
            job_material_id,
            [LayerDefinitionItem.model_validate(item) for item in request.layers],
        )

    @app.post("/projects/{project_id}/runs/{run_id}/layer-definitions/{job_material_id}/confirm")
    def confirm_layer_definitions(
        project_id: str,
        run_id: str,
        job_material_id: str,
        _request: EmptyRequest = Body(...),
    ):
        return runtime.run_service.confirm_layer_definition(
            project_id,
            run_id,
            job_material_id,
        )

    return app
