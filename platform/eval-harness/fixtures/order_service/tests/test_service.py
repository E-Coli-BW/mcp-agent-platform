import unittest

from order_service import (
    InMemoryEventPublisher,
    InMemoryOrderRepository,
    InvalidTransition,
    OrderService,
    OrderStatus,
)


class OrderServiceTest(unittest.TestCase):
    def setUp(self):
        self.repository = InMemoryOrderRepository()
        self.publisher = InMemoryEventPublisher()
        self.service = OrderService(self.repository, self.publisher)

    def test_order_lifecycle(self):
        order = self.service.create_order("customer-1", ["sku-1"])
        self.assertEqual(OrderStatus.PENDING, order.status)

        order = self.service.confirm_order(order.id)
        self.assertEqual(OrderStatus.CONFIRMED, order.status)

        order = self.service.fulfill_order(order.id)
        self.assertEqual(OrderStatus.FULFILLED, order.status)
        self.assertEqual(
            ["order.created", "order.confirmed", "order.fulfilled"],
            [event.name for event in self.publisher.events],
        )

    def test_pending_order_cannot_be_fulfilled(self):
        order = self.service.create_order("customer-1", ["sku-1"])

        with self.assertRaises(InvalidTransition):
            self.service.fulfill_order(order.id)


if __name__ == "__main__":
    unittest.main()