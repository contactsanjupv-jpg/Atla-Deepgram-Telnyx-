"""
Validation of what a customer may ask for when buying a phone number. One definition, used by the request schema
(so a bad value is a 422 before anything is written or rate-limited) AND by the provisioning service (so no other
caller, such as the automatic post-checkout purchase, can write a half-made provisioning row from a bad value).
"""
import re

_COUNTRY = re.compile(r"[A-Z]{2}")
# North American area code: 3 ASCII digits, first digit 2-9. N11 (211, 311 ... 911) are service codes, not area codes.
# [0-9], not \d: \d also matches Arabic-Indic and other Unicode digits that int() happily converts.
_AREA_CODE = re.compile(r"[2-9][0-9]{2}")
_N11 = re.compile(r"[2-9]11")

COUNTRY_MESSAGE = "Country must be a two-letter code, like US."
AREA_CODE_MESSAGE = "Enter a 3-digit area code, like 415, or leave it blank."


class InvalidPhoneInput(ValueError):
    pass


def normalize_country(value: str | None) -> str:
    country = (value or "").strip().upper()
    if not _COUNTRY.fullmatch(country):
        raise InvalidPhoneInput(COUNTRY_MESSAGE)
    return country


def normalize_area_code(value: str | None) -> str | None:
    """None for "no preference" (missing or blank); otherwise exactly 3 ASCII digits."""
    if value is None or not str(value).strip():
        return None
    code = str(value).strip()
    if not _AREA_CODE.fullmatch(code) or _N11.fullmatch(code):
        raise InvalidPhoneInput(AREA_CODE_MESSAGE)
    return code
