from __future__ import annotations

from flyboard.models import RunSummaryResponse


class ServiceError(Exception):
    """Stable application/repository failure; HTTP mapping belongs to handlers."""

    def __init__(
        self,
        code: str,
        *,
        error_id: str | None = None,
        active_project_id: str | None = None,
        active_run_id: str | None = None,
        run_summary: RunSummaryResponse | None = None,
    ) -> None:
        super().__init__(code)
        active = (active_project_id, active_run_id, run_summary)
        if code == "active_run_exists":
            if any(value is None for value in active):
                raise ValueError("active_run_exists requires all active run fields")
        elif any(value is not None for value in active):
            raise ValueError("active run fields are only valid for active_run_exists")
        self.code = code
        self.error_id = error_id
        self.active_project_id = active_project_id
        self.active_run_id = active_run_id
        self.run_summary = run_summary
