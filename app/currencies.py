from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .money import money_decimal


DEFAULT_COUNTRY_CODE = "MX"
DEFAULT_CURRENCY_CODE = "MXN"
DEFAULT_LOCALE_CODE = "es_MX"

COUNTRY_OPTIONS = {
    "MX": ("México", "MXN", "es_MX"),
    "US": ("Estados Unidos", "USD", "en_US"),
    "ES": ("España", "EUR", "es_ES"),
    "CO": ("Colombia", "COP", "es_CO"),
    "CL": ("Chile", "CLP", "es_CL"),
    "PE": ("Perú", "PEN", "es_PE"),
}
SUPPORTED_CURRENCIES = frozenset({"MXN", "USD", "EUR", "COP", "CLP", "PEN"})
SUPPORTED_LOCALES = frozenset({value[2] for value in COUNTRY_OPTIONS.values()})
CURRENCY_DEFAULT_LOCALES = {
    "MXN": "es_MX",
    "USD": "en_US",
    "EUR": "es_ES",
    "COP": "es_CO",
    "CLP": "es_CL",
    "PEN": "es_PE",
}


def normalize_country_code(value) -> str:
    code = str(value or "").strip().upper()
    return code if code in COUNTRY_OPTIONS else DEFAULT_COUNTRY_CODE


def normalize_currency_code(value) -> str:
    code = str(value or "").strip().upper()
    return code if code in SUPPORTED_CURRENCIES else DEFAULT_CURRENCY_CODE


def normalize_locale_code(value, currency_code=None) -> str:
    locale = str(value or "").strip().replace("-", "_")
    if locale in SUPPORTED_LOCALES:
        return locale
    return CURRENCY_DEFAULT_LOCALES[normalize_currency_code(currency_code)]


def organization_money_context(organization) -> tuple[str, str]:
    if organization is None:
        return DEFAULT_CURRENCY_CODE, DEFAULT_LOCALE_CODE
    currency = normalize_currency_code(
        getattr(organization, "currency_code", None)
        or getattr(organization, "currency", None)
    )
    locale = normalize_locale_code(
        getattr(organization, "locale_code", None), currency
    )
    return currency, locale


def country_defaults(country_code) -> tuple[str, str]:
    country = normalize_country_code(country_code)
    _, currency, locale = COUNTRY_OPTIONS[country]
    return currency, locale


def _group_digits(number: str, separator: str) -> str:
    return separator.join(
        number[max(0, len(number) - offset - 3):len(number) - offset]
        for offset in range(((len(number) - 1) // 3) * 3, -1, -3)
    )


def format_currency(value, currency_code=DEFAULT_CURRENCY_CODE, locale_code=None) -> str:
    """Format organization money deterministically without float conversion."""
    currency = normalize_currency_code(currency_code)
    locale = normalize_locale_code(locale_code, currency)
    amount = money_decimal(value, nonnegative=False)
    negative = amount < 0
    amount = abs(amount)
    if currency in {"COP", "CLP"}:
        amount = amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    integer, fraction = format(amount, ".2f").split(".")
    european = locale in {"es_ES", "es_CO", "es_CL"}
    thousands = "." if european else ","
    decimal_separator = "," if european else "."
    grouped = _group_digits(integer, thousands)
    if currency in {"COP", "CLP"}:
        body = grouped
    else:
        body = f"{grouped}{decimal_separator}{fraction}"
    prefixes = {"EUR": "", "PEN": "S/ ", "MXN": "$", "USD": "$", "COP": "$", "CLP": "$"}
    suffixes = {
        "MXN": " MXN", "USD": " USD", "EUR": " €", "COP": " COP",
        "CLP": " CLP", "PEN": "",
    }
    rendered = f"{prefixes[currency]}{body}{suffixes[currency]}"
    return f"-{rendered}" if negative else rendered


def format_money(value, organization) -> str:
    currency, locale = organization_money_context(organization)
    return format_currency(value, currency, locale)


def parse_localized_decimal(value, currency_code, locale_code=None) -> Decimal:
    """Parse an explicit locale format and reject ambiguous separators."""
    currency = normalize_currency_code(currency_code)
    locale = normalize_locale_code(locale_code, currency)
    text = str(value or "").strip().replace("\u00a0", "")
    for token in ("MXN", "USD", "EUR", "COP", "CLP", "PEN", "S/", "$", "€"):
        text = text.replace(token, "")
    text = text.strip().replace(" ", "")
    if not text:
        return money_decimal("0")
    sign = ""
    if text[:1] in {"+", "-"}:
        sign, text = text[0], text[1:]
    if not text or any(character not in "0123456789,." for character in text):
        raise ValueError("ambiguous_number")

    decimal_separator = "," if locale in {"es_ES", "es_CO", "es_CL"} else "."
    grouping_separator = "." if decimal_separator == "," else ","

    def valid_grouped_integer(candidate):
        groups = candidate.split(grouping_separator)
        return (
            groups[0].isdigit()
            and 1 <= len(groups[0]) <= 3
            and all(group.isdigit() and len(group) == 3 for group in groups[1:])
        )

    if "," in text and "." in text:
        explicit_decimal = "," if text.rfind(",") > text.rfind(".") else "."
        explicit_grouping = "." if explicit_decimal == "," else ","
        if text.count(explicit_decimal) != 1:
            raise ValueError("ambiguous_number")
        whole, fraction = text.rsplit(explicit_decimal, 1)
        if not fraction.isdigit() or not 1 <= len(fraction) <= 2:
            raise ValueError("ambiguous_number")
        groups = whole.split(explicit_grouping)
        if not (
            groups[0].isdigit()
            and 1 <= len(groups[0]) <= 3
            and all(
                group.isdigit() and len(group) == 3
                for group in groups[1:]
            )
        ):
            raise ValueError("ambiguous_number")
        whole = whole.replace(explicit_grouping, "")
        text = f"{whole}.{fraction}"
    elif decimal_separator in text:
        if text.count(decimal_separator) != 1:
            raise ValueError("ambiguous_number")
        whole, fraction = text.rsplit(decimal_separator, 1)
        if (
            not whole.isdigit()
            or not fraction.isdigit()
            or not 1 <= len(fraction) <= 2
        ):
            raise ValueError("ambiguous_number")
        text = f"{whole}.{fraction}"
    elif grouping_separator in text:
        if not valid_grouped_integer(text):
            raise ValueError("ambiguous_number")
        text = text.replace(grouping_separator, "")
    elif not text.isdigit():
        raise ValueError("ambiguous_number")
    text = sign + text
    return money_decimal(text)
