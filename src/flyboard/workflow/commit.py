from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

ResultT = TypeVar("ResultT")


class CommitContractError(RuntimeError):
    """Private programming-contract failure for an invalid commit call hierarchy."""


class RunCommitContext:
    __slots__ = ("_channel", "_owner_thread_id", "_valid")

    def __init__(self, channel: RunCommitChannel) -> None:
        self._channel = channel
        self._owner_thread_id = threading.get_ident()
        self._valid = True

    def assert_valid_for(self, channel: RunCommitChannel) -> None:
        if (
            not self._valid
            or self._channel is not channel
            or self._owner_thread_id != threading.get_ident()
        ):
            raise CommitContractError("invalid or expired run commit context")

    def _invalidate(self) -> None:
        self._valid = False


class RunCommitChannel:
    """One ordinary, non-reentrant commit lock for a single active run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner_thread_id: int | None = None
        self._owner_guard = threading.Lock()

    def execute(self, command: Callable[[RunCommitContext], ResultT]) -> ResultT:
        thread_id = threading.get_ident()
        with self._owner_guard:
            if self._owner_thread_id == thread_id:
                raise CommitContractError("nested entry into the same run commit channel")
        self._lock.acquire()
        context = RunCommitContext(self)
        with self._owner_guard:
            self._owner_thread_id = thread_id
        try:
            return command(context)
        finally:
            context._invalidate()
            with self._owner_guard:
                self._owner_thread_id = None
            self._lock.release()

    def assert_context(self, context: RunCommitContext) -> None:
        context.assert_valid_for(self)
