from flyboard.models import (
    Evidence,
    EvidenceIndex,
    GerberArtifactEvidenceLocator,
    ProjectDataRow,
    RuleCheckResult,
    ToolFinding,
)
from flyboard.reports import MarkdownReportRenderer


def _row(**updates):
    values = {
        "index": 1,
        "field_code": "board_thickness_mm",
        "field_name": "成品板厚",
        "field_value": "1.6 | mm",
        "source": "PDF_1_page1",
        "evidence_ids": ["ev_gerber_001"],
        "tool_finding_ids": [],
        "error_ids": [],
        "conflict_candidates": [],
        "category": "板件设计参数",
        "field_state": "confirmed",
        "check_note": "首行\n次行 <安全>",
        "check_conclusion": "检查通过",
    }
    values.update(updates)
    return ProjectDataRow.model_validate(values)


def _evidence_index():
    return EvidenceIndex(
        schema_version="1.0",
        run_id="run_1",
        evidence={
            "ev_gerber_001": Evidence(
                evidence_id="ev_gerber_001",
                type="gerber_artifact",
                source_material_id="mat_1",
                file_path="sources/mat_1",
                page_index=None,
                asset_path="gerberscout/mat_1/result.json",
                region=None,
                note="工具结果",
            )
        },
        locators={
            "ev_gerber_001": GerberArtifactEvidenceLocator(
                kind="gerber_artifact", artifact_kind="result"
            )
        },
    )


def test_renderer_uses_fixed_sections_and_escapes_business_text():
    finding = ToolFinding(
        finding_id="finding_11111111-1111-1111-1111-111111111111",
        producer="gerberscout",
        code="geometry_gap",
        kind="coverage_gap",
        message="工具 | 提示\n第二行",
        source_material_ids=["mat_1"],
        evidence_ids=["ev_gerber_001"],
        error_id=None,
    )
    rule_result = RuleCheckResult(
        schema_version="1.0",
        run_id="run_1",
        status="skipped",
        reason="rule_set_not_selected",
        rule_set=None,
        executed_rule_ids=[],
        attempted_rule_count=0,
        errors=[],
    )
    markdown = MarkdownReportRenderer().render(
        "run_1",
        [
            _row(),
            _row(
                index=2,
                field_code="unknown_note_001",
                field_name="未知说明",
                field_value="未提供",
                field_state="unknown",
                tool_finding_ids=[finding.finding_id],
                check_note="需要人工确认",
                check_conclusion="待确认",
            ),
        ],
        rule_result,
        [finding],
        _evidence_index(),
    )

    assert markdown.index("## 项目数据列表") < markdown.index("## 需关注项")
    assert markdown.index("## 需关注项") < markdown.index("## 已确认字段明细")
    assert markdown.index("## 已确认字段明细") < markdown.index("## 规则检查结果")
    assert markdown.index("## 规则检查结果") < markdown.index("## 工具信息")
    assert "[OK] `confirmed`" in markdown
    assert "[?] `unknown`" in markdown
    assert "1.6 \\| mm" in markdown
    assert "首行<br>次行 &lt;安全&gt;" in markdown
    assert "(gerberscout/mat_1/result.json)" in markdown
    assert "最终运行状态" not in markdown
    assert "duration_ms" not in markdown


def test_renderer_omits_unavailable_optional_sections():
    markdown = MarkdownReportRenderer().render("run_1", [_row(evidence_ids=[])], None, None, None)

    assert "## 规则检查结果" not in markdown
    assert "## 工具信息" not in markdown
    assert "无工具发现" not in markdown
