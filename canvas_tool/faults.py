from __future__ import annotations

import os


class LostAck(RuntimeError):
    pass


class InjectedCrash(BaseException):
    pass


class FaultController:
    """One process gets one immutable fault mode; one-shot faults fire once."""

    def __init__(self, mode: str | None = None) -> None:
        self.mode = mode if mode is not None else os.environ.get("HW3_FAULT")
        self._fired: set[str] = set()

    def _once(self, name: str) -> bool:
        if self.mode == name and name not in self._fired:
            self._fired.add(name)
            return True
        return False

    def before_get(self) -> None:
        if self._once("timeout_get"):
            raise TimeoutError("injected GET timeout")

    def after_view(self, value):
        if self._once("malformed"):
            raise ValueError("injected malformed view")
        return value

    def after_intent(self) -> None:
        if self._once("crash_after_intent"):
            raise InjectedCrash("injected crash after intent")

    def after_post(self) -> None:
        if self._once("lost_ack"):
            raise LostAck("injected lost acknowledgement")
