import unittest

from order_service import (
    InMemoryEventPublisher,
    InMemoryOrderRepository,
    InvalidOrder,
    OrderService,
    build_application,
)


class ItemValidationContractTest(unittest.TestCase):
    def setUp(self):
        self.service = OrderService(
            InMemoryOrderRepository(),
            InMemoryEventPublisher(),
        )

    def test_items_are_normalized(self):
        order = self.service.create_order("customer-1", [" sku-1 ", "sku-2"])
        self.assertEqual(("sku-1", "sku-2"), order.items)

    def test_blank_item_is_rejected(self):
        with self.assertRaises(InvalidOrder):
            self.service.create_order("customer-1", ["sku-1", "   "])

    def test_duplicate_after_normalization_is_rejected(self):
        with self.assertRaises(InvalidOrder):
            self.service.create_order("customer-1", ["sku-1", " sku-1 "])

    def test_api_maps_invalid_items_to_400(self):
        application = build_application()
        response = application.api.handle(
            "POST",
            "/orders",
            {"customer_id": "customer-1", "items": ["sku-1", " sku-1 "]},
        )
        self.assertEqual(400, response.status)


if __name__ == "__main__":
    unittest.main()