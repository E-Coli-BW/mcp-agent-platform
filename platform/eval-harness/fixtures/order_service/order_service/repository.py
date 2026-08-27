from __future__ import annotations

from typing import Protocol

from .models import Order


class OrderRepository(Protocol):
    def save(self, order: Order) -> Order: ...

    def get(self, order_id: str) -> Order | None: ...

    def list_all(self) -> list[Order]: ...


class InMemoryOrderRepository:
    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def save(self, order: Order) -> Order:
        self._orders[order.id] = order
        return order

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def list_all(self) -> list[Order]:
        return list(self._orders.values())