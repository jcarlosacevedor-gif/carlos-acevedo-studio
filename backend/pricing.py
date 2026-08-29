"""Authoritative pricing for purchasable Custom Song configurations."""

from collections.abc import Mapping


class PricingError(ValueError):
    """Raised when a client configuration cannot be priced safely."""


CUSTOM_SONG_BASE_CENTS = 19_900
CURRENCY = "USD"
SOLO_ADD_ONS_CENTS = {
    "none": 0,
    "guitar-solo": 2_500,
    "piano-solo": 2_500,
}
ALLOWED_CONFIGURATION_FIELDS = frozenset({"product", "solo"})


def calculate_custom_song_price(configuration: Mapping[str, object]) -> dict[str, object]:
    """Validate a closed catalog configuration and calculate its server price."""
    if not isinstance(configuration, Mapping):
        raise PricingError("Configuration must be a JSON object.")

    unexpected_fields = set(configuration) - ALLOWED_CONFIGURATION_FIELDS
    if unexpected_fields:
        raise PricingError("Configuration contains unsupported fields.")

    if configuration.get("product") != "custom-song":
        raise PricingError("Unsupported product.")

    solo = configuration.get("solo")
    if not isinstance(solo, str) or solo not in SOLO_ADD_ONS_CENTS:
        raise PricingError("Unsupported solo option.")

    return {
        "product": "custom-song",
        "solo": solo,
        "amount_cents": CUSTOM_SONG_BASE_CENTS + SOLO_ADD_ONS_CENTS[solo],
        "currency": CURRENCY,
    }

