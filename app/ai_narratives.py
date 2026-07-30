"""Controlled OpenAI narratives for verified PATIA metrics.

PATIA calculates every metric. The model may only explain the compact,
tenant-scoped payload it receives. The feature is disabled by default and
always has a deterministic fallback.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from flask import current_app
from sqlalchemy import func

from app import db
from app.models import AiNarrativeRun


OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "what_happened": {"type": "string"},
        "why_it_matters": {"type": "string"},
        "recommended_actions": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
        "limitations": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 3,
        },
        "data_period": {"type": "string"},
    },
    "required": [
        "summary",
        "what_happened",
        "why_it_matters",
        "recommended_actions",
        "limitations",
        "data_period",
    ],
}

SENSITIVE_KEYS = {
    "address",
    "card",
    "customer_name",
    "email",
    "password",
    "phone",
    "postal_code",
    "secret",
    "stripe",
    "token",
}
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
NUMBER_PATTERN = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)?")


class AiNarrativeError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _bool_config(name, default=False):
    value = current_app.config.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def ai_is_enabled():
    return _bool_config("PATIA_AI_ENABLED", False)


def _assert_aggregated_payload(value, path="metrics"):
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if any(sensitive in normalized for sensitive in SENSITIVE_KEYS):
                raise AiNarrativeError("sensitive_payload")
            _assert_aggregated_payload(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_aggregated_payload(child, path)
    elif value is not None and not isinstance(
        value, (str, int, float, bool, Decimal)
    ):
        raise AiNarrativeError("unsupported_payload")


def _canonical_data(feature, language, period, metrics):
    _assert_aggregated_payload(metrics)
    payload = {
        "feature": feature,
        "language": language,
        "data_period": period,
        "metrics": metrics,
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return payload, sha256(serialized.encode("utf-8")).hexdigest()


def _response_text(response):
    for item in response.get("output") or []:
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                return content["text"]
    raise AiNarrativeError("missing_output")


def _post_response(api_key, payload, timeout):
    request = Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _call_openai(payload):
    api_key = str(current_app.config.get("OPENAI_API_KEY") or "").strip()
    model = str(
        current_app.config.get("PATIA_AI_MODEL") or "gpt-5-mini"
    ).strip()
    if not api_key:
        raise AiNarrativeError("missing_api_key")
    timeout = float(current_app.config.get("PATIA_AI_TIMEOUT_SECONDS", 12))
    body = {
        "model": model,
        "store": False,
        "max_output_tokens": int(
            current_app.config.get("PATIA_AI_MAX_OUTPUT_TOKENS", 500)
        ),
        "reasoning": {"effort": "minimal"},
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "patia_business_narrative",
                "strict": True,
                "schema": OUTPUT_SCHEMA,
            },
        },
        "instructions": (
            "You write concise business guidance for a small-business owner. "
            "Use only the verified metrics supplied by PATIA. Never calculate "
            "new values, infer identities, invent figures or claim causality "
            "that the metrics do not prove. Use the requested language."
        ),
        "input": json.dumps(payload, ensure_ascii=False, default=str),
    }
    for attempt in range(2):
        try:
            return _post_response(api_key, body, timeout), model
        except HTTPError as error:
            if error.code not in TRANSIENT_STATUS or attempt:
                raise AiNarrativeError(f"http_{error.code}") from error
        except (TimeoutError, socket.timeout):
            if attempt:
                raise AiNarrativeError("timeout")
        except URLError as error:
            if attempt:
                raise AiNarrativeError("network") from error
    raise AiNarrativeError("unavailable")


def _valid_text(value, *, max_length=900):
    return isinstance(value, str) and 0 < len(value.strip()) <= max_length


def _normalized_numbers(value):
    return {
        token.replace(",", ".").lstrip("+")
        for token in NUMBER_PATTERN.findall(
            json.dumps(value, ensure_ascii=False, default=str)
        )
    }


def _validate_output(value, source_payload):
    if not isinstance(value, dict) or set(value) != set(
        OUTPUT_SCHEMA["required"]
    ):
        raise AiNarrativeError("invalid_schema")
    for key in ("summary", "what_happened", "why_it_matters"):
        if not _valid_text(value[key]):
            raise AiNarrativeError("invalid_schema")
    for key in ("recommended_actions", "limitations"):
        if (
            not isinstance(value[key], list)
            or len(value[key]) > 3
            or any(not _valid_text(item, max_length=400) for item in value[key])
        ):
            raise AiNarrativeError("invalid_schema")
    if value["data_period"] != source_payload["data_period"]:
        raise AiNarrativeError("invalid_period")
    allowed_numbers = _normalized_numbers(source_payload)
    output_numbers = _normalized_numbers(value)
    if not output_numbers.issubset(allowed_numbers):
        raise AiNarrativeError("unverified_number")
    return {
        "summary": value["summary"].strip(),
        "what_happened": value["what_happened"].strip(),
        "why_it_matters": value["why_it_matters"].strip(),
        "recommended_actions": [
            item.strip() for item in value["recommended_actions"]
        ],
        "limitations": [item.strip() for item in value["limitations"]],
        "data_period": value["data_period"],
    }


def _price_config():
    try:
        input_rate = Decimal(
            str(current_app.config["PATIA_AI_INPUT_USD_PER_MILLION"])
        )
        output_rate = Decimal(
            str(current_app.config["PATIA_AI_OUTPUT_USD_PER_MILLION"])
        )
    except (KeyError, InvalidOperation):
        raise AiNarrativeError("pricing_not_configured")
    if input_rate < 0 or output_rate < 0:
        raise AiNarrativeError("pricing_not_configured")
    return input_rate, output_rate


def _estimated_microusd(input_tokens, output_tokens):
    input_rate, output_rate = _price_config()
    return int(
        (Decimal(input_tokens) * input_rate)
        + (Decimal(output_tokens) * output_rate)
    )


def _budget_available(organization_id):
    month_start = datetime.utcnow().replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    spent_global = (
        db.session.query(
            func.coalesce(func.sum(AiNarrativeRun.estimated_cost_microusd), 0)
        )
        .filter(
            AiNarrativeRun.created_at >= month_start,
            AiNarrativeRun.status == "SUCCESS",
        )
        .scalar()
    )
    spent_organization = (
        db.session.query(
            func.coalesce(func.sum(AiNarrativeRun.estimated_cost_microusd), 0)
        )
        .filter(
            AiNarrativeRun.organization_id == organization_id,
            AiNarrativeRun.created_at >= month_start,
            AiNarrativeRun.status == "SUCCESS",
        )
        .scalar()
    )
    global_limit = int(
        Decimal(
            str(current_app.config.get("PATIA_AI_GLOBAL_MONTHLY_USD", "0"))
        )
        * Decimal("1000000")
    )
    organization_limit = int(
        Decimal(
            str(
                current_app.config.get(
                    "PATIA_AI_ORGANIZATION_MONTHLY_USD", "0"
                )
            )
        )
        * Decimal("1000000")
    )
    return (
        global_limit > 0
        and organization_limit > 0
        and int(spent_global or 0) < global_limit
        and int(spent_organization or 0) < organization_limit
    )


def _persist(
    *,
    organization_id,
    feature,
    language,
    data_hash,
    period,
    model,
    status,
    output,
    input_tokens=0,
    output_tokens=0,
    cost=0,
    latency_ms=0,
    error_code=None,
    ttl_hours=24,
):
    record = AiNarrativeRun.query.filter_by(
        organization_id=organization_id,
        feature_name=feature,
        language=language,
        data_hash=data_hash,
    ).first()
    if record is None:
        record = AiNarrativeRun(
            organization_id=organization_id,
            feature_name=feature,
            language=language,
            data_hash=data_hash,
            data_period=period,
        )
        db.session.add(record)
    record.model = model
    record.status = status
    record.output_json = json.dumps(output, ensure_ascii=False)
    record.input_tokens = input_tokens
    record.output_tokens = output_tokens
    record.estimated_cost_microusd = cost
    record.latency_ms = latency_ms
    record.error_code = error_code
    record.created_at = datetime.utcnow()
    record.expires_at = datetime.utcnow() + timedelta(hours=ttl_hours)
    db.session.flush()
    return record


def controlled_narrative(
    *,
    organization_id,
    feature,
    language,
    period,
    metrics,
    fallback,
    ttl_hours=24,
):
    """Return a validated narrative and its source (ai/cache/fallback).

    The caller owns the surrounding database transaction.
    """
    source_payload, data_hash = _canonical_data(
        feature, language, period, metrics
    )
    cached = AiNarrativeRun.query.filter_by(
        organization_id=organization_id,
        feature_name=feature,
        language=language,
        data_hash=data_hash,
        status="SUCCESS",
    ).first()
    if cached and cached.expires_at > datetime.utcnow():
        return json.loads(cached.output_json), "cache"
    if not ai_is_enabled():
        return fallback, "fallback"
    today = datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if feature == "pulse" and AiNarrativeRun.query.filter(
        AiNarrativeRun.organization_id == organization_id,
        AiNarrativeRun.feature_name == feature,
        AiNarrativeRun.language == language,
        AiNarrativeRun.status.in_(("SUCCESS", "FAILED", "LIMITED")),
        AiNarrativeRun.created_at >= today,
    ).first():
        return fallback, "daily_limit"
    try:
        _price_config()
        if not _budget_available(organization_id):
            raise AiNarrativeError("budget_limit")
        started = time.perf_counter()
        response, model = _call_openai(source_payload)
        latency_ms = round((time.perf_counter() - started) * 1000)
        raw_output = json.loads(_response_text(response))
        output = _validate_output(raw_output, source_payload)
        usage = response.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost = _estimated_microusd(input_tokens, output_tokens)
        _persist(
            organization_id=organization_id,
            feature=feature,
            language=language,
            data_hash=data_hash,
            period=period,
            model=model,
            status="SUCCESS",
            output=output,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            latency_ms=latency_ms,
            ttl_hours=ttl_hours,
        )
        return output, "ai"
    except (AiNarrativeError, json.JSONDecodeError) as error:
        code = (
            error.code
            if isinstance(error, AiNarrativeError)
            else "invalid_json"
        )
        current_app.logger.warning(
            "Controlled AI narrative fallback feature=%s code=%s",
            feature,
            code,
        )
        _persist(
            organization_id=organization_id,
            feature=feature,
            language=language,
            data_hash=data_hash,
            period=period,
            model=str(current_app.config.get("PATIA_AI_MODEL") or ""),
            status=(
                "LIMITED"
                if code in {"budget_limit", "daily_limit"}
                else "FAILED"
            ),
            output=fallback,
            error_code=code,
            ttl_hours=1,
        )
        return fallback, "fallback"
