"""Validation for optional, client-rendered account profile metadata."""
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


def validate_avatar_url(value):
    """Store a public image URL without fetching or accepting embedded credentials."""
    if not value:
        return
    message = "Use an HTTPS image URL without credentials or a fragment."
    try:
        URLValidator(schemes=["https"])(value)
        parsed = urlsplit(value)
        if (parsed.username is not None or parsed.password is not None or parsed.fragment or
                "\\" in value or any(ord(character) < 33 or ord(character) == 127 for character in value)):
            raise ValueError
    except (ValidationError, ValueError):
        raise ValidationError(message, code="invalid_avatar_url")
