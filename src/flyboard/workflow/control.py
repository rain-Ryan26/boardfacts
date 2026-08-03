from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import Literal


class RunStopRequested(RuntimeError):
    """Private cooperative-control exception raised at a natural work boundary."""


class StopSignal:
    __slots__ = ("_control",)

    def __init__(self, control: RunControl) -> None:
        self._control = control

    def is_requested(self) -> bool:
        return self._control.is_requested()

    def check_boundary(self) -> None:
        self._control.check_boundary()


class RunControl:
    """Thread-safe, process-local stop intent, wake signal, and worker registry."""

    def __init__(self) -> None:
        self._gate = threading.Lock()
        self._wake = threading.Event()
        self._reason: Literal["user_requested", "service_shutdown"] | None = None
        self._accepting_tasks = True
        self._accepting_business = True
        self._tasks: dict[str, Future[object] | None] = {}
        self._stop_signal = StopSignal(self)

    @property
    def stop_signal(self) -> StopSignal:
        return self._stop_signal

    @property
    def reason(self) -> Literal["user_requested", "service_shutdown"] | None:
        with self._gate:
            return self._reason

    def is_requested(self) -> bool:
        with self._gate:
            return self._reason is not None

    def check_boundary(self) -> None:
        with self._gate:
            if self._reason is not None or not self._accepting_business:
                raise RunStopRequested()

    def accepts_business(self) -> bool:
        with self._gate:
            return self._accepting_business and self._reason is None

    def request_user_stop(self) -> None:
        with self._gate:
            self._reason = "user_requested"
            self._accepting_tasks = False
            self._accepting_business = False
        self._wake.set()

    def request_service_shutdown(self) -> None:
        with self._gate:
            if self._reason != "user_requested":
                self._reason = "service_shutdown"
            self._accepting_tasks = False
            self._accepting_business = False
        self._wake.set()

    def close_business(self) -> None:
        with self._gate:
            self._accepting_tasks = False
            self._accepting_business = False
        self._wake.set()

    def reserve_task(self, key: str) -> bool:
        with self._gate:
            if not self._accepting_tasks or self._reason is not None:
                return False
            if key in self._tasks:
                raise RuntimeError(f"duplicate worker task key: {key}")
            self._tasks[key] = None
            return True

    def bind_task(self, key: str, handle: Future[object]) -> bool:
        with self._gate:
            if key not in self._tasks:
                return False
            self._tasks[key] = handle
            return True

    def release_task(self, key: str) -> None:
        with self._gate:
            self._tasks.pop(key, None)
        self._wake.set()

    def task_count(self) -> int:
        with self._gate:
            return len(self._tasks)

    def task_keys(self) -> tuple[str, ...]:
        with self._gate:
            return tuple(self._tasks)

    def wake(self) -> None:
        self._wake.set()

    def wait(self, timeout: float | None = None) -> bool:
        signaled = self._wake.wait(timeout)
        self._wake.clear()
        return signaled
