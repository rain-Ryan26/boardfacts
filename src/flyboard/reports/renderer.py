from __future__ import annotations

import html
import logging
from collections import defaultdict
from urllib.parse import quote

from flyboard.models import (
    EvidenceIndex,
    FieldState,
    ProjectDataRow,
    RuleCheckResult,
    ToolFinding,
)

_LOGGER = logging.getLogger(__name__)

_STATUS_MARKERS = {
    FieldState.CONFIRMED: "[OK]",
    FieldState.CONFLICT: "[!]",
    FieldState.UNKNOWN: "[?]",
    FieldState.MISSING: "[X]",
}


def _text(value: object) -> str:
    if value is None:
        return "—"
    return html.escape(str(value), quote=True).replace("|", "\\|").replace("\n", "<br>")


def _evidence_lines(evidence_ids: list[str], evidence_index: EvidenceIndex | None) -> list[str]:
    if evidence_index is None:
        return []
    lines: list[str] = []
    for evidence_id in evidence_ids:
        evidence = evidence_index.evidence.get(evidence_id)
        if evidence is None:
            continue
        label = evidence_id
        if evidence.page_index is not None:
            label = f"{label}（第 {evidence.page_index} 页）"
        lines.append(f"- [{_text(label)}]({quote(evidence.asset_path, safe='/')})")
    return lines


class MarkdownReportRenderer:
    """Deterministic terminal report derived only from committed in-memory results."""

    def render(
        self,
        run_id: str,
        project_data_list: list[ProjectDataRow],
        rule_check_result: RuleCheckResult | None,
        tool_findings: list[ToolFinding] | None,
        evidence_index: EvidenceIndex | None,
    ) -> str:
        rows = sorted(project_data_list, key=lambda item: item.index)
        findings = {item.finding_id: item for item in tool_findings or []}
        sections = ["# BoardFacts 项目数据报告", "", f"run_id: `{_text(run_id)}`", ""]
        sections.extend(self._data_list(rows))
        sections.extend(self._attention(rows, findings, evidence_index))
        sections.extend(self._confirmed(rows, evidence_index))
        if rule_check_result is not None:
            sections.extend(self._rules(rule_check_result))
        else:
            _LOGGER.warning("report %s omitted unavailable rule-check result", run_id)
        if tool_findings is not None:
            sections.extend(self._findings(tool_findings, evidence_index))
        else:
            _LOGGER.warning("report %s omitted unavailable tool findings", run_id)
        if evidence_index is None:
            _LOGGER.warning("report %s omitted unavailable evidence details", run_id)
        return "\n".join(sections).rstrip() + "\n"

    @staticmethod
    def _data_list(rows: list[ProjectDataRow]) -> list[str]:
        grouped: dict[str, list[ProjectDataRow]] = defaultdict(list)
        for row in rows:
            grouped[row.category.value].append(row)
        ordered = sorted(grouped.items(), key=lambda item: min(row.index for row in item[1]))
        result = ["## 项目数据列表", ""]
        for category, category_rows in ordered:
            result.extend(
                [
                    f"### {_text(category)}",
                    "",
                    "| 状态 | 字段 | 值 | 来源 | 检查结论 |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for row in category_rows:
                marker = _STATUS_MARKERS[row.field_state]
                result.append(
                    f"| {marker} `{row.field_state.value}` | {_text(row.field_name)} | "
                    f"{_text(row.field_value)} | {_text(row.source)} | "
                    f"{_text(row.check_conclusion)} |"
                )
            result.append("")
        return result

    @staticmethod
    def _attention(
        rows: list[ProjectDataRow],
        findings: dict[str, ToolFinding],
        evidence_index: EvidenceIndex | None,
    ) -> list[str]:
        attention = [row for row in rows if row.field_state != FieldState.CONFIRMED]
        result = ["## 需关注项", ""]
        if not attention:
            return result + ["无。", ""]
        for row in attention:
            marker = _STATUS_MARKERS[row.field_state]
            result.extend(
                [
                    f"<details><summary>{marker} {_text(row.field_name)}</summary>",
                    "",
                    f"- 字段编码：`{_text(row.field_code)}`",
                    f"- 字段状态：`{row.field_state.value}`",
                    f"- 检查备注：{_text(row.check_note)}",
                    f"- 检查结论：{_text(row.check_conclusion)}",
                ]
            )
            for finding_id in row.tool_finding_ids:
                finding = findings.get(finding_id)
                if finding is not None:
                    result.append(f"- 工具发现 `{_text(finding_id)}`：{_text(finding.message)}")
            if row.conflict_candidates:
                result.extend(["", "冲突候选：", ""])
                for index, candidate in enumerate(row.conflict_candidates):
                    prefix = "主显示候选" if index == 0 else f"候选 {index + 1}"
                    result.append(
                        f"- {prefix}：{_text(candidate.field_value)}；来源："
                        f"{_text(candidate.source_ref)}"
                    )
                    result.extend(_evidence_lines(candidate.evidence_ids, evidence_index))
            else:
                evidence = _evidence_lines(row.evidence_ids, evidence_index)
                if evidence:
                    result.extend(["", "证据：", "", *evidence])
            result.extend(["", "</details>", ""])
        return result

    @staticmethod
    def _confirmed(rows: list[ProjectDataRow], evidence_index: EvidenceIndex | None) -> list[str]:
        confirmed = [row for row in rows if row.field_state == FieldState.CONFIRMED]
        result = ["## 已确认字段明细", ""]
        grouped: dict[str, list[ProjectDataRow]] = defaultdict(list)
        for row in confirmed:
            grouped[row.category.value].append(row)
        ordered = sorted(grouped.items(), key=lambda item: min(row.index for row in item[1]))
        for category, category_rows in ordered:
            result.extend([f"<details><summary>{_text(category)}</summary>", ""])
            for row in category_rows:
                result.extend(
                    [
                        f"- **{_text(row.field_name)}** (`{_text(row.field_code)}`)："
                        f"{_text(row.check_note)}",
                        *_evidence_lines(row.evidence_ids, evidence_index),
                    ]
                )
            result.extend(["", "</details>", ""])
        if not confirmed:
            result.extend(["无。", ""])
        return result

    @staticmethod
    def _rules(result: RuleCheckResult) -> list[str]:
        lines = ["## 规则检查结果", "", f"- 执行结果：`{result.status.value}`"]
        if result.rule_set is not None:
            lines.extend(
                [
                    f"- 规则库：`{_text(result.rule_set.rule_set_id)}`",
                    f"- 规则库版本：`{_text(result.rule_set.rule_set_version)}`",
                ]
            )
        if result.reason is not None:
            lines.append(f"- 原因：`{_text(result.reason)}`")
        if result.executed_rule_ids:
            lines.extend(
                [
                    "- 已进入处理的规则：",
                    *[f"  - `{_text(item)}`" for item in result.executed_rule_ids],
                ]
            )
        else:
            lines.append("- 已进入处理的规则：无")
        return [*lines, ""]

    @staticmethod
    def _findings(findings: list[ToolFinding], evidence_index: EvidenceIndex | None) -> list[str]:
        result = ["## 工具信息", ""]
        if not findings:
            return result + ["无工具发现。", ""]
        for finding in findings:
            result.append(
                f"- `{_text(finding.finding_id)}` [{_text(finding.kind.value)}] "
                f"{_text(finding.message)}"
            )
            result.extend(_evidence_lines(finding.evidence_ids, evidence_index))
        result.append("")
        return result
