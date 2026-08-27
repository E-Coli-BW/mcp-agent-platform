import unittest

from order_service import (
    InMemoryEventPublisher,
    InMemoryOrderRepository,
    InvalidTransition,
    OrderService,
    OrderStatus,
    build_application,
)


class CancellationContractTest(unittest.TestCase):
    def setUp(self):
        self.publisher = InMemoryEventPublisher()
        self.service = OrderService(InMemoryOrderRepository(), self.publisher)

    def test_pending_and_confirmed_orders_can_be_cancelled(self):
        pending = self.service.create_order("customer-1", ["sku-1"])
        self.assertEqual(OrderStatus.CANCELLED, self.service.cancel_order(pending.id).status)

        confirmed = self.service.create_order("customer-2", ["sku-2"])
        self.service.confirm_order(confirmed.id)
        self.assertEqual(OrderStatus.CANCELLED, self.service.cancel_order(confirmed.id).status)

    def test_cancellation_is_idempotent_and_event_is_published_once(self):
        order = self.service.create_order("customer-1", ["sku-1"])
        self.service.cancel_order(order.id)
        self.service.cancel_order(order.id)

        cancelled = [event for event in self.publisher.events if event.name == "order.cancelled"]
        self.assertEqual(1, len(cancelled))

    def test_fulfilled_order_cannot_be_cancelled(self):
        order = self.service.create_order("customer-1", ["sku-1"])
        self.service.confirm_order(order.id)
        self.service.fulfill_order(order.id)

        with self.assertRaises(InvalidTransition):
            self.service.cancel_order(order.id)

    def test_api_exposes_cancel_endpoint(self):
        application = build_application()
        created = application.api.handle(
            "POST",
            "/orders",
            {"customer_id": "customer-1", "items": ["sku-1"]},
        )
        order_id = str(created.body["id"])

        cancelled = application.api.handle("POST", f"/orders/{order_id}/cancel")

        self.assertEqual(200, cancelled.status)
        self.assertEqual("cancelled", cancelled.body["status"])


if __name__ == "__main__":
    unittest.main()