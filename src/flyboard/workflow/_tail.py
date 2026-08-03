from __future__ import annotations

import logging

from flyboard.errors import ServiceError
from flyboard.models import RunStatus

_LOGGER = logging.getLogger(__name__)


class TerminalTailMixin:
    def _run_terminal_tail(self, session) -> None:
        render_inputs = None
        with session.lifecycle_lock:
            if not session.report_attempted:
                session.report_attempted = True
                if (
                    self.report_renderer is not None
                    and session.summary.status
                    in {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ERRORS}
                    and session.final_project_data_list_committed
                    and session.final_project_data_list is not None
                ):
                    render_inputs = (
                        session.run_id,
                        session.final_project_data_list,
                        (
                            session.rule_check_result
                            if session.rule_check_result_committed
                            else None
                        ),
                        session.tool_findings if session.tool_findings_committed else None,
                        session.evidence_index if session.evidence_index_committed else None,
                    )
        try:
            if render_inputs is None:
                return
            markdown = self.report_renderer.render(*render_inputs)
            if not isinstance(markdown, str):
                raise TypeError("ReportRenderer.render must return str")
            try:
                with self.projects.mutate(session.project_id) as mutation:
                    mutation.get_current()
                    self.artifacts._replace_report(
                        session.project_id,
                        session.run_id,
                        markdown,
                    )
            except ServiceError as exc:
                _LOGGER.warning(
                    "terminal report skipped for %s: %s",
                    session.run_id,
                    exc.code,
                )
        except Exception:
            _LOGGER.exception("terminal report failed for %s", session.run_id)
        finally:
            self._release_session(session)
