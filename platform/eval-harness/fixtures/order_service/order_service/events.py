from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class OrderEvent:
    name: str
    order_id: str


class EventPublisher(Protocol):
    def publish(self, event: OrderEvent) -> None: ...


class InMemoryEventPublisher:
    def __init__(self) -> None:
        self.events: list[OrderEvent] = []

    def publish(self, event: OrderEvent) -> None:
        self.events.append(event)