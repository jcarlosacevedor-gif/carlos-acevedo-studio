import unittest

from backend.pricing import PricingError, calculate_custom_song_price


class CustomSongPricingTests(unittest.TestCase):
    def test_base_price_is_authoritative(self):
        result = calculate_custom_song_price({"product": "custom-song", "solo": "none"})
        self.assertEqual(result["amount_cents"], 19_900)
        self.assertEqual(result["currency"], "USD")

    def test_guitar_solo_price(self):
        result = calculate_custom_song_price({"product": "custom-song", "solo": "guitar-solo"})
        self.assertEqual(result["amount_cents"], 22_400)

    def test_piano_solo_price(self):
        result = calculate_custom_song_price({"product": "custom-song", "solo": "piano-solo"})
        self.assertEqual(result["amount_cents"], 22_400)

    def test_invalid_or_client_pricing_fields_are_rejected(self):
        invalid_configurations = [
            {"product": "unknown", "solo": "none"},
            {"product": "custom-song", "solo": "triangle-solo"},
            {"product": "custom-song", "solo": ["guitar-solo", "piano-solo"]},
            {"product": "custom-song", "solo": "none", "amount": 1},
            {"product": "custom-song", "solo": "none", "price": 1},
            {"product": "custom-song", "solo": "none", "total": 1},
            {"product": "custom-song", "solo": "none", "quantity": -1},
            {"product": "custom-song", "solo": "none", "amount_cents": -1},
            {"product": "custom-song", "solo": "none", "solos": ["guitar-solo", "piano-solo"]},
            [],
        ]
        for configuration in invalid_configurations:
            with self.subTest(configuration=configuration):
                with self.assertRaises(PricingError):
                    calculate_custom_song_price(configuration)
