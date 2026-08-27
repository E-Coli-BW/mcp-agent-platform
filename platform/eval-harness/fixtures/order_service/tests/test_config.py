import unittest

from order_service.application import load_config


class ConfigTest(unittest.TestCase):
    def test_fixture_config(self):
        config = load_config("config/app.toml")
        self.assertEqual("order-service", config["service_name"])
        self.assertEqual(10, config["max_items_per_order"])


if __name__ == "__main__":
    unittest.main()