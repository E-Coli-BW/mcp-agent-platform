from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class OrderStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FULFILLED = "fulfilled"


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    customer_id: str
    items: tuple[str, ...]
    status: OrderStatus = OrderStatus.PENDING

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "items": list(self.items),
            "status": self.status.value,
        }