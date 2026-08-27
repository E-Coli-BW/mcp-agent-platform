from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from .events import EventPublisher, OrderEvent
from .models import Order, OrderStatus
from .repository import OrderRepository


class OrderNotFound(LookupError):
    pass


class InvalidTransition(ValueError):
    pass


class InvalidOrder(ValueError):
    pass


class OrderService:
    def __init__(
        self,
        repository: OrderRepository,
        publisher: EventPublisher,
        max_items_per_order: int = 10,
    ) -> None:
        self._repository = repository
        self._publisher = publisher
        self._max_items = max_items_per_order

    def create_order(self, customer_id: str, items: list[str]) -> Order:
        if not customer_id.strip():
            raise InvalidOrder("customer_id is required")
        if not items:
            raise InvalidOrder("at least one item is required")
        if len(items) > self._max_items:
            raise InvalidOrder(f"an order supports at most {self._max_items} items")

        order = Order(
            id=str(uuid4()),
            customer_id=customer_id,
            items=tuple(items),
        )
        self._repository.save(order)
        self._publisher.publish(OrderEvent("order.created", order.id))
        return order

    def get_order(self, order_id: str) -> Order:
        order = self._repository.get(order_id)
        if order is None:
            raise OrderNotFound(order_id)
        return order

    def confirm_order(self, order_id: str) -> Order:
        order = self.get_order(order_id)
        if order.status is not OrderStatus.PENDING:
            raise InvalidTransition(
                f"cannot confirm an order in status {order.status.value}"
            )
        return self._transition(order, OrderStatus.CONFIRMED, "order.confirmed")

    def fulfill_order(self, order_id: str) -> Order:
        order = self.get_order(order_id)
        if order.status is not OrderStatus.CONFIRMED:
            raise InvalidTransition(
                f"cannot fulfill an order in status {order.status.value}"
            )
        return self._transition(order, OrderStatus.FULFILLED, "order.fulfilled")

    def _transition(self, order: Order, status: OrderStatus, event_name: str) -> Order:
        updated = replace(order, status=status)
        self._repository.save(updated)
        self._publisher.publish(OrderEvent(event_name, order.id))
        return updated