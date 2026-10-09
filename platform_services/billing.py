"""Provider-neutral billing boundary. No provider SDK or charge is enabled."""
from dataclasses import dataclass


class BillingNotConfigured(Exception):
    pass


@dataclass(frozen=True)
class BillingAdapter:
    name: str

    def create_checkout(self, *args, **kwargs):
        raise BillingNotConfigured("Payment collection requires an approved, configured provider integration.")


PROVIDERS = {name: BillingAdapter(name) for name in ["placeholder", "stripe", "paddle", "adyen", "braintree"]}
