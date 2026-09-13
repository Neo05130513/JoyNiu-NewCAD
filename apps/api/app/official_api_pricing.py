"""Versioned API-equivalent prices, independent of supplier subscription cost.

Prices verified against OpenAI's model and pricing pages on 2026-09-12.
An immutable copy is embedded in each billing policy; no network request can
change an in-flight or historical bill. All arithmetic uses exact rationals.
"""
from copy import deepcopy
from fractions import Fraction
import re

from .platform import ValidationError

CATALOG_VERSION = "openai-standard-2026-09-12"
SOURCE = "https://developers.openai.com/api/docs/pricing"
POINTS_PER_CNY = 100
CATALOG = {
    "version": CATALOG_VERSION, "verifiedAt": "2026-09-12", "currency": "USD",
    "sourceUrl": SOURCE, "serviceTier": "standard", "pointsPerCny": POINTS_PER_CNY,
    "models": [{"model": "gpt-6-astra", "inputUsdPerMillion": "10",
                "cachedInputUsdPerMillion": "1", "cacheWriteUsdPerMillion": "12.5",
                "outputUsdPerMillion": "50", "longContextThreshold": 272000,
                "longContextInputMultiplier": "2", "longContextOutputMultiplier": "1.5",
                "sourceUrl": "https://developers.openai.com/api/docs/models/gpt-6-astra"}],
    "missingDetailsPolicy": "waive_unconfirmed_surcharges",
}
USD_FIELDS = ("inputUsdPerMillion", "cachedInputUsdPerMillion", "cacheWriteUsdPerMillion", "outputUsdPerMillion")
POINT_FIELDS = ("inputUnitsPerMillion", "cachedInputUnitsPerMillion", "cacheWriteUnitsPerMillion", "outputUnitsPerMillion")


def decimal_value(value, label, *, positive=True, maximum=100):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValidationError(f"{label}须填写有效数字")
    text = str(value).strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d{1,6})?", text):
        raise ValidationError(f"{label}须为最多6位小数的非负数字")
    number = Fraction(text)
    if number > maximum or number < 0 or (positive and number == 0):
        raise ValidationError(f"{label}须大于0且不超过{maximum}")
    return exact_decimal(number)


def exact_decimal(number):
    """Fraction with decimal inputs has a finite decimal representation."""
    sign = "-" if number < 0 else ""
    numerator, denominator = abs(number.numerator), number.denominator
    whole, rem = divmod(numerator, denominator)
    digits = []
    while rem:
        digit, rem = divmod(rem * 10, denominator)
        digits.append(str(digit))
    return sign + str(whole) + ("." + "".join(digits) if digits else "")


def catalog():
    return deepcopy(CATALOG)


def normalize_api_pricing(value, bindings):
    if not isinstance(value, dict) or set(value) - {"catalogVersion", "usdCnyRate", "multiplier", "pointsPerCny"}:
        raise ValidationError("官方API折算配置字段不正确")
    if value.get("catalogVersion") != CATALOG_VERSION:
        raise ValidationError("官方价格目录已变化，请刷新并核对后保存")
    if value.get("pointsPerCny", POINTS_PER_CNY) != POINTS_PER_CNY:
        raise ValidationError("充值换算固定为10元1000积分")
    fx = decimal_value(value.get("usdCnyRate"), "美元兑人民币结算汇率")
    multiplier = decimal_value(value.get("multiplier"), "收费倍率")
    saved = {"catalogVersion": CATALOG_VERSION, "usdCnyRate": fx, "multiplier": multiplier,
             "pointsPerCny": POINTS_PER_CNY, "currency": "USD", "serviceTier": "standard",
             "sourceUrl": SOURCE, "verifiedAt": CATALOG["verifiedAt"],
             "missingDetailsPolicy": CATALOG["missingDetailsPolicy"]}
    models = {row["model"]: row for row in CATALOG["models"]}
    rates = []
    for binding in bindings:
        if set(binding) != {"provider", "model"} or binding["model"] not in models:
            raise ValidationError("请选择已核对官方价格的模型；美元基价由服务端提供")
        original = models[binding["model"]]
        scale = Fraction(fx) * Fraction(multiplier) * POINTS_PER_CNY
        rates.append({**binding, **deepcopy(original),
                      **{point: exact_decimal(Fraction(original[usd]) * scale) for usd, point in zip(USD_FIELDS, POINT_FIELDS)}})
    return saved, rates


def price_call(row, rate, pricing):
    """Price known tokens; waive unknown write/long-context premiums explicitly.

    Codex CLI turn totals may cover several model requests. Never apply a
    long-context surcharge to an aggregate total as if it were one request.
    """
    incoming, cached, outgoing = (row.get(key) for key in ("inputTokens", "cachedInputTokens", "outputTokens"))
    if any(type(x) is not int or x < 0 for x in (incoming, cached, outgoing)) or cached > incoming:
        return None
    writes = row.get("cacheWriteTokens")
    notes = []
    if writes is None:
        writes = 0
        if incoming > cached:
            notes.append("cache_write_premium_waived")
    if type(writes) is not int or writes < 0 or cached + writes > incoming:
        return None
    single = row.get("usageScope") == "request"
    long_context = single and incoming > rate["longContextThreshold"]
    if not single and incoming > rate["longContextThreshold"]:
        notes.append("unconfirmed_long_context_premium_waived")
    input_multiplier = Fraction(rate["longContextInputMultiplier"]) if long_context else Fraction(1)
    output_multiplier = Fraction(rate["longContextOutputMultiplier"]) if long_context else Fraction(1)
    usd = (((incoming-cached-writes) * Fraction(rate["inputUsdPerMillion"])
            + cached * Fraction(rate["cachedInputUsdPerMillion"])
            + writes * Fraction(rate["cacheWriteUsdPerMillion"])) * input_multiplier
           + outgoing * Fraction(rate["outputUsdPerMillion"]) * output_multiplier) / 1_000_000
    points = usd * Fraction(pricing["usdCnyRate"]) * pricing["pointsPerCny"] * Fraction(pricing["multiplier"])
    return points, {"callId": row["callId"], "model": row["model"], "provider": row["provider"],
                    "inputTokens": incoming, "cachedInputTokens": cached, "cacheWriteTokens": row.get("cacheWriteTokens"),
                    "outputTokens": outgoing, "contextBand": "long" if long_context else "standard",
                    "apiEquivalentUsd": exact_decimal(usd), "creditUnitsExact": exact_decimal(points), "notes": notes}
