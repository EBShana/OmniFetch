from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    """Something the operator may care about.

    Types emitted by the scheduler:
      want.fallback    a candidate failed permanently, next candidate queued
      want.given_up    all candidates exhausted (payload: want_id, work_key)
      want.acquired    a download completed, want satisfied
      task.failed      terminal failure of a task without a want chain
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)


class Notifier(ABC):
    @abstractmethod
    def send(self, event: Event) -> None:
        raise NotImplementedError


class NullNotifier(Notifier):
    def send(self, event: Event) -> None:
        return None


class WebhookNotifier(Notifier):
    """POSTs each event as JSON to a webhook URL (generic receiver,
    dingtalk/telegram custom bots, etc.). Failures are swallowed — notifying
    must never break the pipeline."""

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def send(self, event: Event) -> None:
        import httpx

        try:
            httpx.post(self.url, json={"type": event.type, **event.payload}, timeout=self.timeout)
        except Exception:
            pass
