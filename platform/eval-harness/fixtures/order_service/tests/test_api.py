import unittest

from order_service import build_application


class OrderAPITest(unittest.TestCase):
    def setUp(self):
        self.application = build_application()

    def test_create_read_and_confirm(self):
        created = self.application.api.handle(
            "POST",
            "/orders",
            {"customer_id": "customer-1", "items": ["sku-1"]},
        )
        self.assertEqual(201, created.status)

        order_id = str(created.body["id"])
        found = self.application.api.handle("GET", f"/orders/{order_id}")
        self.assertEqual(200, found.status)

        confirmed = self.application.api.handle("POST", f"/orders/{order_id}/confirm")
        self.assertEqual(200, confirmed.status)
        self.assertEqual("confirmed", confirmed.body["status"])

    def test_unknown_order_is_404(self):
        response = self.application.api.handle("GET", "/orders/missing")
        self.assertEqual(404, response.status)


if __name__ == "__main__":
    unittest.main()