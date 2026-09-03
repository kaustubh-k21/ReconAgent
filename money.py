"""Parse money as VALID, MISSING, or INVALID. Keep invalid amounts out of reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Optional, Union

VALID = "VALID"
MISSING = "MISSING"
INVALID = "INVALID"

MONEY_QUANTUM = Decimal("0.01")

# Longest tokens first so "Rs." is not eaten as "Rs" leaving a stray ".".
_CURRENCY_TOKENS = (
    "INR", "inr", "Inr",
    "Rs.", "RS.", "rs.",
    "Rs", "RS", "rs",
    "₹", "$", "€", "£",
)


@dataclass(frozen=True)
class MoneyParse:
    status: str
    value: Optional[Decimal]
    reason: Optional[str]
    raw: str

    def canonical(self) -> str:
        if self.status != VALID or self.value is None:
            raise ValueError("canonical() requires VALID money")
        quantized = self.value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
        return format(quantized, "f")

    def __str__(self) -> str:
        if self.status == VALID:
            return f"VALID({self.canonical()})"
        if self.status == MISSING:
            return MISSING
        return f"INVALID({self.reason})"


def _missing(raw: str) -> MoneyParse:
    return MoneyParse(MISSING, None, None, raw)


def _invalid(raw: str, reason: str) -> MoneyParse:
    return MoneyParse(INVALID, None, reason, raw)


def _valid(raw: str, value: Decimal) -> MoneyParse:
    quantized = value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    return MoneyParse(VALID, quantized, None, raw)


def _strip_currency(text: str) -> tuple[str, bool]:
    stripped_any = False
    changed = True
    while changed:
        changed = False
        s = text.strip()
        for token in _CURRENCY_TOKENS:
            if s.startswith(token):
                s = s[len(token):].lstrip(" \t")
                stripped_any = True
                changed = True
                text = s
                break
            if s.endswith(token):
                s = s[:-len(token)].rstrip(" \t")
                stripped_any = True
                changed = True
                text = s
                break
        else:
            text = s
    return text, stripped_any


def _take_sign(text: str, sign: int) -> tuple[str, int]:
    text = text.strip()
    if text.startswith(("+", "−", "-")):
        if text[0] in ("-", "−"):
            sign = -sign
        return text[1:].strip(), sign
    return text, sign


def _normalize_numeric(body: str) -> Optional[str]:
    if not body:
        return None
    if body.startswith("."):
        body = "0" + body
    if body.endswith(".") or body.endswith(","):
        return None

    commas = body.count(",")
    periods = body.count(".")
    if commas + periods == 0:
        return body if body.isdigit() else None

    if commas and periods:
        last_comma = body.rfind(",")
        last_period = body.rfind(".")
        if last_period > last_comma:
            intpart = body[:last_period].replace(",", "")
            frac = body[last_period + 1:]
        else:
            intpart = body[:last_comma].replace(".", "")
            frac = body[last_comma + 1:]
        if not intpart.isdigit() or not frac.isdigit() or not frac:
            return None
        return intpart + "." + frac

    if commas:
        last = body.rfind(",")
        frac = body[last + 1:]
        intpart = body[:last]
        if not frac.isdigit():
            return None
        if len(frac) == 3:
            compacted = body.replace(",", "")
            return compacted if compacted.isdigit() else None
        if 1 <= len(frac) <= 2:
            compacted_int = intpart.replace(",", "")
            if not compacted_int.isdigit():
                return None
            return compacted_int + "." + frac
        return None

    last = body.rfind(".")
    frac = body[last + 1:]
    intpart = body[:last]
    if periods > 1:
        parts = body.split(".")
        if not all(p.isdigit() and p != "" for p in parts):
            return None
        if any(len(p) != 3 for p in parts[1:]):
            return None
        return "".join(parts)
    if not frac.isdigit() or not frac:
        return None
    if intpart == "":
        intpart = "0"
    if not intpart.isdigit():
        return None
    return intpart + "." + frac


def parse_money(raw: Union[str, int, float, Decimal, None]) -> MoneyParse:
    if raw is None:
        return _missing("")
    if isinstance(raw, bool):
        return _invalid(str(raw), "not_numeric")
    if isinstance(raw, Decimal):
        if not raw.is_finite():
            return _invalid(str(raw), "not_finite")
        return _valid(str(raw), raw)
    if isinstance(raw, int):
        return _valid(str(raw), Decimal(raw))
    if isinstance(raw, float):
        if raw != raw or raw in (float("inf"), float("-inf")):
            return _invalid(str(raw), "not_finite")
        return _valid(str(raw), Decimal(str(raw)))

    original = str(raw)
    text = original.strip().replace("\u00a0", " ").replace("\u202f", " ")
    if text == "":
        return _missing(original)

    lower = text.casefold()
    if lower in {"nan", "inf", "-inf", "+inf", "infinity", "-infinity"}:
        return _invalid(original, "not_finite")
    if "e" in lower:
        return _invalid(original, "scientific_notation")

    sign = 1
    accounting = False
    if text[0] in "({":
        if not (text.endswith(")") or text.endswith("}")):
            return _invalid(original, "malformed_currency")
        accounting = True
        text = text[1:-1].strip()
    text, sign = _take_sign(text, sign)
    body, stripped_currency = _strip_currency(text)
    body, sign = _take_sign(body, sign)
    body, stripped_again = _strip_currency(body)
    stripped_currency = stripped_currency or stripped_again
    body = body.replace(" ", "")
    if body == "":
        if stripped_currency:
            return _invalid(original, "empty_after_currency")
        return _invalid(original, "not_numeric")
    if any(ch.isalpha() for ch in body):
        if stripped_currency:
            return _invalid(original, "malformed_currency")
        return _invalid(original, "not_numeric")
    if any(ch not in "0123456789,." for ch in body):
        return _invalid(original, "not_numeric")

    normalized = _normalize_numeric(body)
    if normalized is None:
        return _invalid(original, "invalid_grouping")

    try:
        value = Decimal(normalized)
    except InvalidOperation:
        return _invalid(original, "not_numeric")
    if not value.is_finite():
        return _invalid(original, "not_finite")
    if accounting:
        sign = -sign
    if sign < 0:
        value = -value
    return _valid(original, value)
