from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
import uvicorn

from flyboard.api import create_http_app
from flyboard.app import build_application
from flyboard.config import AppConfig, load_local_env


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _request(args, method: str, path: str, body=None) -> int:
    try:
        with httpx.Client(base_url=args.url, timeout=args.timeout) as client:
            response = client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        _print({"kind": "cli_error", "code": "service_unavailable", "message": str(exc)})
        return 2
    try:
        payload = response.json()
    except ValueError:
        payload = {"status": response.status_code, "content": response.text}
    _print(payload)
    return 0 if response.is_success else 1


def _serve(args) -> int:
    try:
        config = (
            AppConfig(args.storage_root) if args.storage_root is not None else AppConfig.from_env()
        )
    except ValueError as exc:
        _print({"kind": "cli_error", "code": "configuration_invalid", "message": str(exc)})
        return 2
    application = build_application(config)
    try:
        uvicorn.run(
            create_http_app(application),
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
    finally:
        application.close()
    return 0


def _add_client_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="BoardFacts API 根地址")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 请求超时秒数")


def _resource_command(args, suffix: str) -> int:
    return _request(
        args,
        "GET",
        f"/projects/{args.project_id}/runs/{args.run_id}/{suffix}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="boardfacts", description="BoardFacts 产品 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="启动 API 与 GUI 工作台")
    serve.add_argument("--storage-root", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(action=_serve)

    projects = sub.add_parser("projects", help="列出项目")
    _add_client_options(projects)
    projects.set_defaults(action=lambda args: _request(args, "GET", "/projects"))

    create = sub.add_parser("create-project", help="创建项目")
    _add_client_options(create)
    create.add_argument("project_name")
    create.set_defaults(
        action=lambda args: _request(args, "POST", "/projects", {"project_name": args.project_name})
    )

    delete = sub.add_parser("delete-project", help="删除项目")
    _add_client_options(delete)
    delete.add_argument("project_id")
    delete.add_argument("--delete-artifacts", action="store_true")
    delete.set_defaults(
        action=lambda args: _request(
            args,
            "DELETE",
            f"/projects/{args.project_id}",
            {"delete_artifacts": args.delete_artifacts},
        )
    )

    materials = sub.add_parser("materials", help="读取项目资料")
    _add_client_options(materials)
    materials.add_argument("project_id")
    materials.set_defaults(
        action=lambda args: _request(args, "GET", f"/projects/{args.project_id}/materials")
    )

    register = sub.add_parser("register-materials", help="登记本机资料路径")
    _add_client_options(register)
    register.add_argument("project_id")
    register.add_argument("input_path")
    register.set_defaults(
        action=lambda args: _request(
            args,
            "POST",
            f"/projects/{args.project_id}/materials",
            {"input_path": args.input_path},
        )
    )

    rule_sets = sub.add_parser("rule-sets", help="列出规则库")
    _add_client_options(rule_sets)
    rule_sets.set_defaults(action=lambda args: _request(args, "GET", "/rule-sets"))

    register_rule_set = sub.add_parser("register-rule-set", help="登记规则库文件")
    _add_client_options(register_rule_set)
    register_rule_set.add_argument("rule_set_path")
    register_rule_set.set_defaults(
        action=lambda args: _request(
            args, "POST", "/rule-sets", {"rule_set_path": args.rule_set_path}
        )
    )

    start = sub.add_parser("start-run", help="创建并启动 run")
    _add_client_options(start)
    start.add_argument("project_id")
    start.add_argument("material_ids", nargs="+")
    start.add_argument("--rule-set-id")
    start.set_defaults(
        action=lambda args: _request(
            args,
            "POST",
            f"/projects/{args.project_id}/runs",
            {"material_ids": args.material_ids, "rule_set_id": args.rule_set_id},
        )
    )

    run = sub.add_parser("run", help="读取 run 摘要")
    _add_client_options(run)
    run.add_argument("project_id")
    run.add_argument("run_id")
    run.set_defaults(
        action=lambda args: _request(args, "GET", f"/projects/{args.project_id}/runs/{args.run_id}")
    )

    stop = sub.add_parser("stop-run", help="停止 run")
    _add_client_options(stop)
    stop.add_argument("project_id")
    stop.add_argument("run_id")
    stop.set_defaults(
        action=lambda args: _request(
            args, "POST", f"/projects/{args.project_id}/runs/{args.run_id}/stop", {}
        )
    )

    for name, suffix, help_text in (
        ("data-list", "data-list", "读取项目数据列表"),
        ("tool-findings", "tool-findings", "读取工具发现"),
        ("gerber-preparations", "gerber-preparations", "读取 Gerber preparations"),
        ("layer-definitions", "layer-definitions", "读取层定义"),
    ):
        command = sub.add_parser(name, help=help_text)
        _add_client_options(command)
        command.add_argument("project_id")
        command.add_argument("run_id")
        command.set_defaults(action=lambda args, current=suffix: _resource_command(args, current))
    return parser


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    args = build_parser().parse_args(argv)
    return int(args.action(args))


if __name__ == "__main__":
    sys.exit(main())
