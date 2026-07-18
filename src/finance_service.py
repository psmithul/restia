"""Typed V3 personal-finance records on Restia's canonical Life graph.

This module is deliberately a record, read, and bounded-analysis surface.  It
does not connect to banks or execute transfers, purchases, investments,
payments, or subscription cancellations.  Any future real-world finance
mutation must remain a separately reviewed Level 6 action proposal.
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from core.database import Account, LifeEntity, LifeSource
from src.life_graph import (
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    delete_life_entity,
    get_life_entity,
    list_life_entity_versions,
    serialize_life_entity,
    update_life_entity,
)


FINANCE_SCHEMA_VERSION = 1
FINANCE_ENTITY_TYPE = "finance_record"
FINANCE_SCAN_LIMIT = 750
FINANCE_ACTION_RISK_LEVEL = 6
FINANCE_ANALYSIS_NOTICE = (
    "This is a bounded summary of records supplied to Restia. It is not "
    "financial, tax, legal, or investment advice."
)

FINANCE_RECORD_TYPES = frozenset({
    "account",
    "balance_observation",
    "income",
    "expense",
    "subscription",
    "investment_observation",
    "loan",
    "tax_item",
    "bill",
    "receivable",
    "budget_goal",
    "document_reference",
})

_SCOPES = frozenset({"personal", "business"})
_SENSITIVITIES = frozenset({"private", "restricted"})
_STATUSES = frozenset({"active", "archived"})
_SOURCE_KINDS = frozenset({
    "manual", "user_observation", "import", "statement", "receipt",
    "invoice", "tax_document", "provider",
})
_IMPORT_SOURCE_KINDS = frozenset({
    "import", "statement", "receipt", "invoice", "tax_document", "provider",
})
_GROUPINGS = frozenset({"day", "week", "month"})
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_LONG_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d[\d -]{10,25}\d(?!\d)")
_URL_CREDENTIAL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.I)

_SECRET_KEY_PARTS = (
    "password", "passwd", "passcode", "secret", "token", "credential",
    "cookie", "authorization", "auth_header", "private_key", "seed_phrase",
    "recovery_phrase", "pin", "cvv", "cvc", "card_number", "account_number",
    "routing_number", "iban", "swift_code", "bank_login", "online_banking",
)
_EXECUTION_KEYS = frozenset({
    "action", "execute", "executor", "tool_call", "transfer", "wire",
    "purchase", "buy", "sell", "trade", "payment", "pay_now",
    "cancel_subscription", "withdraw", "deposit_funds", "place_order",
})
_NON_FINANCIAL_IDENTIFIER_FIELDS = frozenset({
    "account_entity_id",
    "related_entity_id",
    "source_id",
    "external_id",
    "content_sha256",
})

_MONETARY_TYPES = FINANCE_RECORD_TYPES - {"account", "document_reference"}
_STRICTLY_POSITIVE_TYPES = frozenset({
    "income", "expense", "subscription", "loan", "tax_item", "bill",
    "receivable", "budget_goal",
})
_DUE_TYPES = frozenset({"subscription", "loan", "tax_item", "bill", "receivable"})

_DETAIL_FIELDS: dict[str, frozenset[str]] = {
    "account": frozenset({
        "account_kind", "institution", "display_name", "reference_last4",
    }),
    "balance_observation": frozenset({
        "balance_kind", "account_entity_id", "reference_last4",
    }),
    "income": frozenset({
        "category", "counterparty", "transaction_status", "account_entity_id",
        "reference_last4",
    }),
    "expense": frozenset({
        "category", "merchant", "transaction_status", "account_entity_id",
        "reference_last4", "category_source", "category_confidence",
        "category_rule",
    }),
    "subscription": frozenset({
        "provider", "category", "cadence", "subscription_status",
        "account_entity_id", "reference_last4",
    }),
    "investment_observation": frozenset({
        "asset_label", "symbol", "quantity", "quantity_unit",
        "account_entity_id", "reference_last4",
    }),
    "loan": frozenset({
        "lender", "loan_kind", "original_amount", "interest_rate_percent",
        "loan_status", "account_entity_id", "reference_last4",
    }),
    "tax_item": frozenset({
        "jurisdiction", "tax_year", "item_kind", "tax_status",
        "reference_last4",
    }),
    "bill": frozenset({
        "payee", "category", "bill_status", "account_entity_id",
        "reference_last4",
    }),
    "receivable": frozenset({
        "payer", "category", "receivable_status", "account_entity_id",
        "reference_last4",
    }),
    "budget_goal": frozenset({
        "target_kind", "category", "actual_amount", "goal_status",
    }),
    "document_reference": frozenset({
        "document_kind", "storage_ref", "content_sha256", "related_entity_id",
        "reference_last4",
    }),
}

_EXPENSE_CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("transport", ("bus", "train", "metro", "taxi", "uber", "ola", "fuel", "petrol", "diesel")),
    ("travel", ("flight", "airline", "hotel", "hostel", "booking", "visa")),
    ("food", ("cafe", "coffee", "restaurant", "swiggy", "zomato", "grocery", "meal")),
    ("housing", ("rent", "landlord", "maintenance", "mortgage")),
    ("utilities", ("electric", "water", "internet", "broadband", "mobile", "utility")),
    ("health", ("pharmacy", "hospital", "clinic", "doctor", "medical", "fitness", "gym")),
    ("education", ("course", "tuition", "university", "college", "book", "paper")),
    ("software", ("software", "hosting", "cloud", "domain", "subscription", "saas")),
    ("entertainment", ("movie", "cinema", "game", "music", "streaming")),
    ("shopping", ("amazon", "flipkart", "store", "shop", "clothing")),
    ("fees", ("fee", "charge", "interest", "penalty", "commission")),
)


def _text(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool = False,
    preserve_lines: bool = False,
) -> str:
    raw = str(value or "").strip()
    normalized = raw if preserve_lines else " ".join(raw.split())
    if required and not normalized:
        raise LifeGraphError(f"{field} is required")
    if len(normalized) > limit:
        raise LifeGraphError(f"{field} must not exceed {limit} characters")
    _assert_no_long_financial_number(normalized, field=field)
    if _URL_CREDENTIAL_RE.search(normalized):
        raise LifeGraphError(f"{field} must not contain embedded credentials")
    return normalized


def _identifier(value: object, *, field: str) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise LifeGraphError(
            f"{field} must be a lowercase identifier using letters, numbers, _, -, or ."
        )
    return normalized


def _datetime(value: object | None, *, field: str, required: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime") from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _aware_as_of(value: object, *, field: str = "as_of") -> tuple[datetime, str]:
    if not isinstance(value, (str, datetime)):
        raise LifeGraphError(f"{field} must be an offset-aware ISO-8601 datetime")
    raw = value
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            raw = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise LifeGraphError(
                f"{field} must be an offset-aware ISO-8601 datetime"
            ) from exc
    assert isinstance(raw, datetime)
    if raw.tzinfo is None or raw.utcoffset() is None:
        raise LifeGraphError(f"{field} must include an explicit UTC offset")
    utc = raw.astimezone(timezone.utc).replace(tzinfo=None)
    return utc, raw.isoformat()


def classify_expense_category(
    *, title: object, merchant: object = "",
) -> dict[str, Any]:
    """Return one deterministic category suggestion without a model call."""

    text_value = " ".join(
        str(value or "").strip().casefold() for value in (title, merchant)
    )
    for category, keywords in _EXPENSE_CATEGORY_RULES:
        matched = next((keyword for keyword in keywords if keyword in text_value), None)
        if matched:
            return {
                "category": category,
                "category_source": "deterministic_v1",
                "category_confidence": 85,
                "category_rule": f"keyword:{matched}",
            }
    return {
        "category": "uncategorized",
        "category_source": "deterministic_v1",
        "category_confidence": 0,
        "category_rule": "no_rule_match",
    }


def _classification_ready_details(
    *, record_type: object, title: object, details: object | None,
) -> object | None:
    if str(record_type or "").strip().lower() != "expense":
        return details
    if details is None:
        clean: dict[str, Any] = {}
    elif isinstance(details, Mapping):
        clean = dict(details)
    else:
        return details
    if str(clean.get("category") or "").strip():
        return clean
    clean.update(classify_expense_category(
        title=title, merchant=clean.get("merchant")
    ))
    return clean


def _iso(value: object | None, *, field: str) -> str | None:
    parsed = _datetime(value, field=field)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal(value: object, *, field: str, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be a finite decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LifeGraphError(f"{field} must be a finite decimal number") from exc
    if not number.is_finite():
        raise LifeGraphError(f"{field} must be a finite decimal number")
    exponent = number.as_tuple().exponent
    if exponent < -4:
        raise LifeGraphError(f"{field} must not have more than 4 decimal places")
    if (
        len(number.as_tuple().digits) > 18
        or len(number.as_tuple().digits) + max(exponent, 0) > 18
    ):
        raise LifeGraphError(f"{field} is too large")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _currency(value: object | None, *, required: bool) -> str | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError("currency is required")
        return None
    normalized = str(value).strip().upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise LifeGraphError("currency must be a three-letter ISO-4217 code")
    return normalized


def _walk_items(value: object, path: str = "") -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            current = f"{path}.{key}" if path else str(key)
            items.append((current, nested))
            items.extend(_walk_items(nested, current))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            items.extend(_walk_items(nested, f"{path}[{index}]"))
    return items


def _assert_no_long_financial_number(value: str, *, field: str) -> None:
    for match in _LONG_DIGIT_RUN_RE.finditer(value):
        digits = re.sub(r"\D", "", match.group(0))
        if 12 <= len(digits) <= 19:
            raise LifeGraphError(
                f"{field} must not contain a full card or financial account number; "
                "store only a masked last-four reference"
            )


def assert_safe_finance_payload(value: object) -> None:
    """Reject credentials, full financial identifiers, and executor-shaped data."""
    for path, nested in _walk_items(value):
        key = path.rsplit(".", 1)[-1].split("[", 1)[0].strip().lower()
        if any(part in key for part in _SECRET_KEY_PARTS):
            raise LifeGraphError("Finance records must not contain credentials or secrets")
        if key in _EXECUTION_KEYS:
            raise LifeGraphError(
                "Finance records cannot execute transfers, payments, purchases, "
                "investments, trades, or subscription cancellations"
            )
        if isinstance(nested, str):
            if key not in _NON_FINANCIAL_IDENTIFIER_FIELDS:
                _assert_no_long_financial_number(nested, field=path)
            if _URL_CREDENTIAL_RE.search(nested):
                raise LifeGraphError("Finance records must not contain embedded credentials")


def _json_object(value: object | None, *, field: str, max_bytes: int = 24_000) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LifeGraphError(f"{field} must be an object")
    result = dict(value)
    assert_safe_finance_payload(result)
    try:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise LifeGraphError(f"{field} must not exceed {max_bytes} bytes")
    return result


def _masked_last4(value: object | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"[\s*•-]", "", raw)
    if not re.fullmatch(r"[A-Za-z0-9]{2,4}", compact):
        raise LifeGraphError("reference_last4 must contain only the final 2 to 4 characters")
    return f"••••{compact.upper()}"


def _entity_id(value: object | None, *, field: str) -> str | None:
    normalized = _text(value, field=field, limit=36) or None
    if normalized and not re.fullmatch(r"[A-Za-z0-9-]{1,36}", normalized):
        raise LifeGraphError(f"{field} is not a valid local entity identifier")
    return normalized


def _source(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("source must be an object")
    assert_safe_finance_payload(value)
    allowed = {"kind", "label", "reference", "external_id", "source_id", "observed_at"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported source fields: {', '.join(unknown)}")
    kind = _identifier(value.get("kind"), field="source.kind")
    if kind not in _SOURCE_KINDS:
        raise LifeGraphError(
            "source.kind must be manual, user_observation, import, statement, "
            "receipt, invoice, tax_document, or provider"
        )
    return {
        "kind": kind,
        "label": _text(value.get("label"), field="source.label", limit=240, required=True),
        "reference": _text(value.get("reference"), field="source.reference", limit=1_000) or None,
        "external_id": _text(value.get("external_id"), field="source.external_id", limit=256) or None,
        "source_id": _entity_id(value.get("source_id"), field="source.source_id"),
        "observed_at": _iso(value.get("observed_at"), field="source.observed_at"),
    }


def _bounded_choice(value: object, *, field: str, choices: frozenset[str], required: bool = True) -> str | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
    normalized = _identifier(value, field=field)
    if normalized not in choices:
        raise LifeGraphError(f"{field} must be one of: {', '.join(sorted(choices))}")
    return normalized


def _details(value: object | None, *, record_type: str, currency: str | None) -> dict[str, Any]:
    details = _json_object(value, field="details")
    unknown = sorted(set(details) - _DETAIL_FIELDS[record_type])
    if unknown:
        raise LifeGraphError(
            f"Unsupported {record_type} detail fields: {', '.join(unknown)}"
        )

    def text_field(name: str, limit: int = 240, required: bool = False) -> None:
        if name in details or required:
            details[name] = _text(
                details.get(name), field=f"details.{name}", limit=limit, required=required
            )

    if "reference_last4" in details:
        details["reference_last4"] = _masked_last4(details.get("reference_last4"))
    if "account_entity_id" in details:
        details["account_entity_id"] = _entity_id(
            details.get("account_entity_id"), field="details.account_entity_id"
        )

    if record_type == "account":
        details["account_kind"] = _bounded_choice(
            details.get("account_kind"), field="details.account_kind",
            choices=frozenset({"cash", "bank", "card", "wallet", "investment", "loan", "other"}),
        )
        text_field("display_name", required=True)
        text_field("institution")
    elif record_type == "balance_observation":
        details["balance_kind"] = _bounded_choice(
            details.get("balance_kind"), field="details.balance_kind",
            choices=frozenset({"cash", "account", "card_available", "card_owed", "investment", "loan"}),
        )
    elif record_type in {"income", "expense"}:
        text_field("category", required=True)
        text_field("counterparty" if record_type == "income" else "merchant")
        if record_type == "expense" and "category_source" in details:
            details["category_source"] = _bounded_choice(
                details.get("category_source"), field="details.category_source",
                choices=frozenset({"user", "import", "deterministic_v1"}),
            )
            try:
                confidence = int(details.get("category_confidence"))
            except (TypeError, ValueError) as exc:
                raise LifeGraphError(
                    "details.category_confidence must be an integer from 0 to 100"
                ) from exc
            if confidence < 0 or confidence > 100:
                raise LifeGraphError(
                    "details.category_confidence must be an integer from 0 to 100"
                )
            details["category_confidence"] = confidence
            text_field("category_rule", limit=120, required=True)
        details["transaction_status"] = _bounded_choice(
            details.get("transaction_status") or "posted",
            field="details.transaction_status",
            choices=frozenset({"pending", "posted", "refunded", "voided"}),
        )
    elif record_type == "subscription":
        text_field("provider", required=True)
        text_field("category")
        details["cadence"] = _bounded_choice(
            details.get("cadence"), field="details.cadence",
            choices=frozenset({"weekly", "monthly", "quarterly", "annual", "custom"}),
        )
        details["subscription_status"] = _bounded_choice(
            details.get("subscription_status") or "active",
            field="details.subscription_status",
            choices=frozenset({"active", "paused", "cancelled", "expired"}),
        )
    elif record_type == "investment_observation":
        text_field("asset_label", required=True)
        if "symbol" in details:
            details["symbol"] = _text(
                details.get("symbol"), field="details.symbol", limit=32
            ).upper()
        details["quantity"] = _decimal(
            details.get("quantity"), field="details.quantity", required=True
        )
        if Decimal(details["quantity"]) < 0:
            raise LifeGraphError("details.quantity must not be negative")
        details["quantity_unit"] = _bounded_choice(
            details.get("quantity_unit"), field="details.quantity_unit",
            choices=frozenset({"shares", "units", "grams", "ounces"}),
        )
    elif record_type == "loan":
        text_field("lender", required=True)
        details["loan_kind"] = _bounded_choice(
            details.get("loan_kind"), field="details.loan_kind",
            choices=frozenset({"mortgage", "student", "vehicle", "personal", "business", "other"}),
        )
        details["loan_status"] = _bounded_choice(
            details.get("loan_status") or "active", field="details.loan_status",
            choices=frozenset({"active", "deferred", "paid", "closed"}),
        )
        if "original_amount" in details:
            details["original_amount"] = _decimal(
                details.get("original_amount"), field="details.original_amount", required=True
            )
            if Decimal(details["original_amount"]) <= 0:
                raise LifeGraphError("details.original_amount must be greater than zero")
        if "interest_rate_percent" in details:
            details["interest_rate_percent"] = _decimal(
                details.get("interest_rate_percent"), field="details.interest_rate_percent", required=True
            )
            rate = Decimal(details["interest_rate_percent"])
            if rate < 0 or rate > 100:
                raise LifeGraphError("details.interest_rate_percent must be between 0 and 100")
    elif record_type == "tax_item":
        text_field("jurisdiction", required=True)
        text_field("item_kind", required=True)
        try:
            year = int(details.get("tax_year"))
        except (TypeError, ValueError) as exc:
            raise LifeGraphError("details.tax_year must be a valid year") from exc
        if year < 1900 or year > 2200:
            raise LifeGraphError("details.tax_year must be between 1900 and 2200")
        details["tax_year"] = year
        details["tax_status"] = _bounded_choice(
            details.get("tax_status") or "open", field="details.tax_status",
            choices=frozenset({"open", "filed", "paid", "closed"}),
        )
    elif record_type == "bill":
        text_field("payee", required=True)
        text_field("category")
        details["bill_status"] = _bounded_choice(
            details.get("bill_status") or "due", field="details.bill_status",
            choices=frozenset({"due", "scheduled", "paid", "voided", "disputed"}),
        )
    elif record_type == "receivable":
        text_field("payer", required=True)
        text_field("category")
        details["receivable_status"] = _bounded_choice(
            details.get("receivable_status") or "due",
            field="details.receivable_status",
            choices=frozenset({"due", "invoiced", "received", "voided", "disputed"}),
        )
    elif record_type == "budget_goal":
        details["target_kind"] = _bounded_choice(
            details.get("target_kind"), field="details.target_kind",
            choices=frozenset({"spending_limit", "savings_goal", "debt_reduction", "income_goal"}),
        )
        text_field("category")
        details["goal_status"] = _bounded_choice(
            details.get("goal_status") or "active", field="details.goal_status",
            choices=frozenset({"active", "met", "missed", "retired"}),
        )
        if "actual_amount" in details:
            details["actual_amount"] = _decimal(
                details.get("actual_amount"), field="details.actual_amount", required=True
            )
    elif record_type == "document_reference":
        details["document_kind"] = _bounded_choice(
            details.get("document_kind"), field="details.document_kind",
            choices=frozenset({"receipt", "invoice", "statement", "tax", "contract", "other"}),
        )
        text_field("storage_ref", limit=1_000, required=True)
        if "content_sha256" in details:
            digest = str(details.get("content_sha256") or "").strip()
            if not _SHA256_RE.fullmatch(digest):
                raise LifeGraphError("details.content_sha256 must be a 64-character SHA-256 digest")
            details["content_sha256"] = digest.lower()
        if "related_entity_id" in details:
            details["related_entity_id"] = _entity_id(
                details.get("related_entity_id"), field="details.related_entity_id"
            )
    return details


def _provenance(value: object | None, *, source: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _json_object(value, field="provenance", max_bytes=16_000)
    supplied_source_id = str(provenance.get("source_id") or "").strip() or None
    if supplied_source_id and supplied_source_id != source.get("source_id"):
        raise LifeGraphError("provenance.source_id must match source.source_id")
    provenance["source_kind"] = source["kind"]
    provenance["source_label"] = source["label"]
    if source.get("source_id"):
        provenance["source_id"] = source["source_id"]
    else:
        provenance.pop("source_id", None)
    return provenance


def validate_finance_properties(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("finance properties must be an object")
    assert_safe_finance_payload(value)
    allowed = {
        "finance_schema_version", "record_type", "scope", "effective_at",
        "due_at", "period_start", "period_end", "amount", "currency",
        "unit", "details", "source",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported finance property fields: {', '.join(unknown)}")
    record_type = _identifier(value.get("record_type"), field="record_type")
    if record_type not in FINANCE_RECORD_TYPES:
        raise LifeGraphError(
            "record_type must be one of: " + ", ".join(sorted(FINANCE_RECORD_TYPES))
        )
    scope = _bounded_choice(value.get("scope"), field="scope", choices=_SCOPES)
    effective_at = _datetime(value.get("effective_at"), field="effective_at", required=True)
    due_at = _datetime(value.get("due_at"), field="due_at")
    period_start = _datetime(value.get("period_start"), field="period_start")
    period_end = _datetime(value.get("period_end"), field="period_end")
    if period_start and period_end and period_end < period_start:
        raise LifeGraphError("period_end must not be before period_start")
    if record_type == "budget_goal" and (period_start is None or period_end is None):
        raise LifeGraphError("budget_goal records require period_start and period_end")
    amount_required = record_type in _MONETARY_TYPES
    amount = _decimal(value.get("amount"), field="amount", required=amount_required)
    currency = _currency(value.get("currency"), required=amount_required or record_type == "account")
    if amount is not None:
        numeric = Decimal(amount)
        if record_type in _STRICTLY_POSITIVE_TYPES and numeric <= 0:
            raise LifeGraphError(f"{record_type} amount must be greater than zero")
        if record_type == "investment_observation" and numeric < 0:
            raise LifeGraphError("investment_observation amount must not be negative")
    if record_type in {"bill", "receivable"} and due_at is None:
        raise LifeGraphError(f"{record_type} records require due_at")
    unit = str(value.get("unit") or ("currency" if amount is not None else "metadata")).strip().lower()
    if unit not in {"currency", "metadata"}:
        raise LifeGraphError("unit must be currency or metadata")
    if amount is not None and unit != "currency":
        raise LifeGraphError("monetary amounts must use the currency unit")
    source = _source(value.get("source"))
    details = _details(value.get("details"), record_type=record_type, currency=currency)
    if (
        record_type == "subscription"
        and details.get("subscription_status") == "active"
        and due_at is None
    ):
        raise LifeGraphError("active subscription records require due_at")
    return {
        "finance_schema_version": FINANCE_SCHEMA_VERSION,
        "record_type": record_type,
        "scope": scope,
        "effective_at": _iso(effective_at, field="effective_at"),
        "due_at": _iso(due_at, field="due_at"),
        "period_start": _iso(period_start, field="period_start"),
        "period_end": _iso(period_end, field="period_end"),
        "amount": amount,
        "currency": currency,
        "unit": unit,
        "details": details,
        "source": source,
    }


def is_typed_finance_payload(entity_type: object, properties: object | None = None) -> bool:
    normalized_type = str(entity_type or "").strip().lower()
    if normalized_type == FINANCE_ENTITY_TYPE:
        return True
    return isinstance(properties, Mapping) and (
        properties.get("finance_schema_version") is not None
        or properties.get("record_type") in FINANCE_RECORD_TYPES
    )


def _owned_finance_record(db, owner_id: str, entity_id: object) -> LifeEntity:
    entity = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
    if entity.entity_type != FINANCE_ENTITY_TYPE:
        raise LifeGraphNotFound("Finance record not found")
    validate_finance_properties(entity.properties or {})
    return entity


def _validate_source_authority(db, *, owner_id: str, source: Mapping[str, Any]) -> None:
    source_id = source.get("source_id")
    if source_id and db.query(LifeSource.id).filter(
        LifeSource.id == source_id, LifeSource.owner_id == owner_id,
    ).scalar() is None:
        raise LifeGraphNotFound("Finance source not found")


def _validate_detail_authority(
    db, *, owner_id: str, properties: Mapping[str, Any]
) -> None:
    details = properties.get("details")
    if not isinstance(details, Mapping):
        return
    account_entity_id = details.get("account_entity_id")
    if account_entity_id:
        account_entity = get_life_entity(
            db, owner_id=owner_id, entity_id=account_entity_id
        )
        if account_entity.entity_type != FINANCE_ENTITY_TYPE:
            raise LifeGraphNotFound("Finance account record not found")
        account_properties = validate_finance_properties(
            account_entity.properties or {}
        )
        if account_properties["record_type"] != "account":
            raise LifeGraphNotFound("Finance account record not found")
    related_entity_id = details.get("related_entity_id")
    if related_entity_id:
        get_life_entity(db, owner_id=owner_id, entity_id=related_entity_id)


def create_finance_record(
    db,
    *,
    account: Account,
    record_type: object,
    title: object,
    scope: object,
    effective_at: object,
    source: object,
    amount: object | None = None,
    currency: object | None = None,
    unit: object | None = None,
    due_at: object | None = None,
    period_start: object | None = None,
    period_end: object | None = None,
    details: object | None = None,
    note: object = "",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
    import_mode: bool = False,
) -> tuple[LifeEntity, bool]:
    normalized_title = _text(title, field="title", limit=240, required=True)
    normalized_note = _text(note, field="note", limit=20_000, preserve_lines=True)
    normalized_sensitivity = _identifier(sensitivity, field="sensitivity")
    if normalized_sensitivity not in _SENSITIVITIES:
        raise LifeGraphError("Finance record sensitivity must be private or restricted")
    properties = validate_finance_properties({
        "record_type": record_type,
        "scope": scope,
        "effective_at": effective_at,
        "due_at": due_at,
        "period_start": period_start,
        "period_end": period_end,
        "amount": amount,
        "currency": currency,
        "unit": unit,
        "details": _classification_ready_details(
            record_type=record_type, title=normalized_title, details=details,
        ),
        "source": source,
    })
    if import_mode:
        if properties["source"]["kind"] not in _IMPORT_SOURCE_KINDS:
            raise LifeGraphError("Imported finance records require an external source kind")
        if not str(idempotency_key or "").strip():
            raise LifeGraphError("Imported finance records require idempotency_key")
        if not any(properties["source"].get(key) for key in ("external_id", "reference", "source_id")):
            raise LifeGraphError("Imported finance records require an external source reference")
    _validate_source_authority(db, owner_id=account.id, source=properties["source"])
    _validate_detail_authority(db, owner_id=account.id, properties=properties)
    normalized_provenance = _provenance(provenance, source=properties["source"])
    entity, created = create_life_entity(
        db,
        account=account,
        entity_type=FINANCE_ENTITY_TYPE,
        title=normalized_title,
        summary=normalized_note,
        status="active",
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=_datetime(effective_at, field="effective_at", required=True),
        due_at=_datetime(due_at, field="due_at"),
        idempotency_key=idempotency_key,
        reason="Finance record captured",
    )
    return entity, created


def import_finance_records(
    db, *, account: Account, records: Sequence[Mapping[str, Any]]
) -> tuple[list[LifeEntity], int]:
    if not records or len(records) > 100:
        raise LifeGraphError("Finance import must contain between 1 and 100 records")
    keys: set[str] = set()
    for record in records:
        key = str(record.get("idempotency_key") or "").strip()
        if not key:
            raise LifeGraphError("Imported finance records require idempotency_key")
        if key in keys:
            raise LifeGraphError("Finance import idempotency_key values must be unique")
        keys.add(key)
        # Validate the full import shape before the first write so predictable
        # validation failures never leave even a transient partial batch.
        properties = validate_finance_properties({
            field: record.get(field) for field in (
                "record_type", "scope", "effective_at", "due_at", "period_start",
                "period_end", "amount", "currency", "unit", "details", "source",
            )
        } | {
            "details": _classification_ready_details(
                record_type=record.get("record_type"),
                title=record.get("title"),
                details=record.get("details"),
            )
        })
        if properties["source"]["kind"] not in _IMPORT_SOURCE_KINDS:
            raise LifeGraphError("Imported finance records require an external source kind")
        if not any(properties["source"].get(field) for field in ("external_id", "reference", "source_id")):
            raise LifeGraphError("Imported finance records require an external source reference")
        _text(record.get("title"), field="title", limit=240, required=True)
        _text(record.get("note", ""), field="note", limit=20_000, preserve_lines=True)
        sensitivity = _identifier(record.get("sensitivity", "private"), field="sensitivity")
        if sensitivity not in _SENSITIVITIES:
            raise LifeGraphError("Finance record sensitivity must be private or restricted")
        _provenance(record.get("provenance"), source=properties["source"])
        _validate_source_authority(db, owner_id=account.id, source=properties["source"])
        _validate_detail_authority(db, owner_id=account.id, properties=properties)
    entities: list[LifeEntity] = []
    created_count = 0
    # A batch savepoint makes conflict and uniqueness failures atomic even for
    # direct service callers that catch the exception and reuse their session.
    with db.begin_nested():
        for record in records:
            entity, created = create_finance_record(
                db, account=account, import_mode=True, **dict(record)
            )
            entities.append(entity)
            created_count += int(created)
    return entities, created_count


def update_finance_record(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_finance_record(db, account.id, entity_id)
    allowed = {
        "title", "scope", "effective_at", "due_at", "period_start", "period_end",
        "amount", "currency", "unit", "details", "source", "note", "provenance",
        "confidence", "sensitivity", "status",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported finance fields: {', '.join(unknown)}")
    assert_safe_finance_payload(changes)
    current = validate_finance_properties(entity.properties or {})
    merged = dict(current)
    for field in (
        "scope", "effective_at", "due_at", "period_start", "period_end", "amount",
        "currency", "unit", "details", "source",
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_finance_properties(merged)
    _validate_source_authority(db, owner_id=account.id, source=properties["source"])
    _validate_detail_authority(db, owner_id=account.id, properties=properties)
    provenance_value = changes.get("provenance", entity.provenance or {})
    if properties["source"] != current["source"] and "provenance" not in changes:
        provenance_value = dict(entity.provenance or {})
        provenance_value.pop("source_id", None)
    entity_changes: dict[str, Any] = {
        "properties": properties,
        "provenance": _provenance(provenance_value, source=properties["source"]),
    }
    if "title" in changes:
        entity_changes["title"] = _text(changes["title"], field="title", limit=240, required=True)
    if "note" in changes:
        entity_changes["summary"] = _text(
            changes["note"], field="note", limit=20_000, preserve_lines=True
        )
    if "effective_at" in changes:
        entity_changes["occurred_at"] = _datetime(
            changes["effective_at"], field="effective_at", required=True
        )
    if "due_at" in changes:
        entity_changes["due_at"] = _datetime(changes["due_at"], field="due_at")
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _identifier(changes["sensitivity"], field="sensitivity")
        if sensitivity not in _SENSITIVITIES:
            raise LifeGraphError("Finance record sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    if "status" in changes:
        status = _identifier(changes["status"], field="status")
        if status not in _STATUSES:
            raise LifeGraphError("Finance record status must be active or archived")
        entity_changes["status"] = status
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Finance record updated",
    )


def delete_finance_record(
    db, *, owner_id: str, entity_id: object, expected_version: int,
    reason: object = "Finance record deleted",
) -> LifeEntity:
    entity = _owned_finance_record(db, owner_id, entity_id)
    return delete_life_entity(
        db, owner_id=owner_id, entity_id=entity.id,
        expected_version=expected_version,
        reason=_text(reason, field="reason", limit=500, required=True),
    )


def serialize_finance_record(entity: LifeEntity) -> dict[str, Any]:
    if entity.entity_type != FINANCE_ENTITY_TYPE:
        raise LifeGraphError("Entity is not a Finance record")
    properties = validate_finance_properties(entity.properties or {})
    result = serialize_life_entity(entity)
    result.update({
        "note": entity.summary or "",
        "record_type": properties["record_type"],
        "scope": properties["scope"],
        "effective_at": properties["effective_at"],
        "due_at": properties["due_at"],
        "period_start": properties["period_start"],
        "period_end": properties["period_end"],
        "amount": properties["amount"],
        "currency": properties["currency"],
        "unit": properties["unit"],
        "details": properties["details"],
        "source": properties["source"],
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
        "execution_policy": {
            "risk_level": FINANCE_ACTION_RISK_LEVEL,
            "record_only": True,
            "can_execute_financial_action": False,
        },
    })
    return result


def get_finance_record(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_finance_record(_owned_finance_record(db, owner_id, entity_id))


def _record_candidates(
    db,
    *,
    owner_id: str,
    record_type: object | None = None,
    scope: object | None = None,
    currency: object | None = None,
    from_at: object | None = None,
    to_at: object | None = None,
    include_archived: bool = False,
) -> tuple[list[LifeEntity], bool]:
    normalized_type = None
    if record_type:
        normalized_type = _identifier(record_type, field="record_type")
        if normalized_type not in FINANCE_RECORD_TYPES:
            raise LifeGraphError("Unsupported finance record_type")
    normalized_scope = None
    if scope:
        normalized_scope = _bounded_choice(scope, field="scope", choices=_SCOPES)
    normalized_currency = _currency(currency, required=False) if currency else None
    start = _datetime(from_at, field="from_at")
    end = _datetime(to_at, field="to_at")
    if start and end and end < start:
        raise LifeGraphError("to_at must not be before from_at")
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == FINANCE_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if not include_archived:
        query = query.filter(LifeEntity.status == "active")
    if start:
        query = query.filter(LifeEntity.occurred_at >= start)
    if end:
        query = query.filter(LifeEntity.occurred_at <= end)
    rows = query.order_by(
        LifeEntity.occurred_at.desc(), LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(FINANCE_SCAN_LIMIT + 1).all()
    truncated = len(rows) > FINANCE_SCAN_LIMIT
    result: list[LifeEntity] = []
    for row in rows[:FINANCE_SCAN_LIMIT]:
        properties = validate_finance_properties(row.properties or {})
        if normalized_type and properties["record_type"] != normalized_type:
            continue
        if normalized_scope and properties["scope"] != normalized_scope:
            continue
        if normalized_currency and properties["currency"] != normalized_currency:
            continue
        result.append(row)
    return result, truncated


def list_finance_records(
    db, *, owner_id: str, record_type: object | None = None,
    scope: object | None = None, currency: object | None = None,
    from_at: object | None = None, to_at: object | None = None,
    include_archived: bool = False, limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _record_candidates(
        db, owner_id=owner_id, record_type=record_type, scope=scope,
        currency=currency, from_at=from_at, to_at=to_at,
        include_archived=include_archived,
    )
    return [serialize_finance_record(row) for row in rows[:bounded]], (
        scan_truncated or len(rows) > bounded
    )


def search_finance_records(
    db, *, owner_id: str, query_text: object, record_type: object | None = None,
    scope: object | None = None, currency: object | None = None,
    from_at: object | None = None, to_at: object | None = None, limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _record_candidates(
        db, owner_id=owner_id, record_type=record_type, scope=scope,
        currency=currency, from_at=from_at, to_at=to_at,
    )
    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for entity in rows:
        record = serialize_finance_record(entity)
        title = str(record["title"]).casefold()
        note = str(record["note"]).casefold()
        body = json.dumps({
            "record_type": record["record_type"], "scope": record["scope"],
            "amount": record["amount"], "currency": record["currency"],
            "effective_at": record["effective_at"], "due_at": record["due_at"],
            "details": record["details"], "source": record["source"],
        }, ensure_ascii=False, sort_keys=True).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in note:
            rank, field = 3, "note"
        elif needle in body:
            rank, field = 4, "properties"
        else:
            continue
        matches.append(((rank, title, entity.id), {"record": record, "match": field, "rank": rank}))
    matches.sort(key=lambda row: row[0])
    items = [row for _, row in matches[:bounded]]
    return {
        "items": items, "count": len(items), "scanned": len(rows),
        "truncated": scan_truncated or len(matches) > bounded,
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def _money_totals(rows: Sequence[LifeEntity], *, record_types: set[str] | None = None) -> dict[str, str]:
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        props = validate_finance_properties(row.properties or {})
        if record_types and props["record_type"] not in record_types:
            continue
        if props["amount"] is not None and props["currency"]:
            totals[props["currency"]] += Decimal(props["amount"])
    return {currency: format(value, "f") for currency, value in sorted(totals.items())}


def _cash_flow_rows(rows: Sequence[LifeEntity], record_type: str) -> list[LifeEntity]:
    selected: list[LifeEntity] = []
    for row in rows:
        properties = validate_finance_properties(row.properties or {})
        if properties["record_type"] != record_type:
            continue
        if properties["details"].get("transaction_status") in {"refunded", "voided"}:
            continue
        selected.append(row)
    return selected


def _outstanding_rows(rows: Sequence[LifeEntity]) -> tuple[list[LifeEntity], list[LifeEntity]]:
    payable: list[LifeEntity] = []
    receivable: list[LifeEntity] = []
    terminal = {
        "loan": ("loan_status", {"paid", "closed"}),
        "tax_item": ("tax_status", {"paid", "closed"}),
        "bill": ("bill_status", {"paid", "voided"}),
        "receivable": ("receivable_status", {"received", "voided"}),
    }
    for row in rows:
        properties = validate_finance_properties(row.properties or {})
        rule = terminal.get(properties["record_type"])
        if rule is None:
            continue
        status_key, closed_states = rule
        if properties["details"].get(status_key) in closed_states:
            continue
        if properties["record_type"] == "receivable":
            receivable.append(row)
        else:
            payable.append(row)
    return payable, receivable


def _latest_balance_totals(rows: Sequence[LifeEntity]) -> tuple[dict[str, str], int]:
    latest: dict[tuple[str, str, str], LifeEntity] = {}
    for row in rows:
        properties = validate_finance_properties(row.properties or {})
        if properties["record_type"] != "balance_observation":
            continue
        details = properties["details"]
        reference = str(
            details.get("account_entity_id")
            or details.get("reference_last4")
            or row.title
        )
        key = (properties["currency"], details["balance_kind"], reference)
        existing = latest.get(key)
        if existing is None or (
            row.occurred_at or datetime.min, row.id
        ) > (
            existing.occurred_at or datetime.min, existing.id
        ):
            latest[key] = row
    return _money_totals(list(latest.values())), len(latest)


def finance_summary(
    db, *, owner_id: str, scope: object | None = None,
    from_at: object | None = None, to_at: object | None = None,
) -> dict[str, Any]:
    rows, truncated = _record_candidates(
        db, owner_id=owner_id, scope=scope, from_at=from_at, to_at=to_at,
    )
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[validate_finance_properties(row.properties or {})["record_type"]] += 1
    income = _money_totals(_cash_flow_rows(rows, "income"))
    expense = _money_totals(_cash_flow_rows(rows, "expense"))
    currencies = sorted(set(income) | set(expense))
    net = {
        currency: format(Decimal(income.get(currency, "0")) - Decimal(expense.get(currency, "0")), "f")
        for currency in currencies
    }
    payable, receivable = _outstanding_rows(rows)
    balances, balance_accounts = _latest_balance_totals(rows)
    return {
        "scope": str(scope).lower() if scope else None,
        "from_at": _iso(from_at, field="from_at"),
        "to_at": _iso(to_at, field="to_at"),
        "record_counts": dict(sorted(counts.items())),
        "cash_flow": {"income": income, "expense": expense, "net": net},
        "latest_balance_observations": {
            "totals": balances,
            "account_or_balance_keys": balance_accounts,
            "method": "latest observation per currency, balance kind, and masked account reference",
        },
        "outstanding_payables": _money_totals(payable),
        "outstanding_receivables": _money_totals(receivable),
        "count": len(rows), "truncated": truncated,
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def _bucket_start(value: datetime, grouping: str) -> date:
    if grouping == "day":
        return value.date()
    if grouping == "week":
        return (value - timedelta(days=value.weekday())).date()
    return value.date().replace(day=1)


def finance_cash_flow(
    db, *, owner_id: str, scope: object | None = None,
    currency: object | None = None, group_by: object = "month",
    from_at: object | None = None, to_at: object | None = None,
) -> dict[str, Any]:
    grouping = _identifier(group_by, field="group_by")
    if grouping not in _GROUPINGS:
        raise LifeGraphError("group_by must be day, week, or month")
    normalized_currency = _currency(currency, required=False) if currency else None
    rows, truncated = _record_candidates(
        db, owner_id=owner_id, scope=scope, currency=normalized_currency,
        from_at=from_at, to_at=to_at,
    )
    buckets: dict[tuple[date, str], dict[str, Decimal | int]] = {}
    for row in rows:
        props = validate_finance_properties(row.properties or {})
        if props["record_type"] not in {"income", "expense"} or row.occurred_at is None:
            continue
        details = props["details"]
        if details.get("transaction_status") in {"voided", "refunded"}:
            continue
        key = (_bucket_start(row.occurred_at, grouping), props["currency"])
        bucket = buckets.setdefault(key, {"income": Decimal(0), "expense": Decimal(0), "count": 0})
        bucket[props["record_type"]] += Decimal(props["amount"])
        bucket["count"] += 1
    items = []
    for (period, item_currency), values in sorted(buckets.items()):
        income = values["income"]
        expense = values["expense"]
        items.append({
            "period_start": period.isoformat(), "currency": item_currency,
            "income": format(income, "f"), "expense": format(expense, "f"),
            "net": format(income - expense, "f"), "count": values["count"],
        })
    return {
        "group_by": grouping, "scope": str(scope).lower() if scope else None,
        "currency": normalized_currency, "buckets": items, "count": len(items),
        "scanned": len(rows), "truncated": truncated,
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def list_subscriptions(
    db, *, owner_id: str, scope: object | None = None,
    status: object | None = None, currency: object | None = None, limit: int = 100,
) -> dict[str, Any]:
    rows, truncated = _record_candidates(
        db, owner_id=owner_id, record_type="subscription", scope=scope, currency=currency,
    )
    normalized_status = _identifier(status, field="status") if status else None
    if normalized_status and normalized_status not in {"active", "paused", "cancelled", "expired"}:
        raise LifeGraphError("Unsupported subscription status")
    selected = [
        row for row in rows
        if not normalized_status
        or validate_finance_properties(row.properties or {})["details"]["subscription_status"] == normalized_status
    ]
    records = [serialize_finance_record(row) for row in selected]
    bounded = max(1, min(100, int(limit)))
    items = records[:bounded]
    return {
        "items": items, "count": len(items),
        "totals": _money_totals(selected[:bounded], record_types={"subscription"}),
        "truncated": truncated or len(records) > bounded,
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def list_due_finance_records(
    db, *, owner_id: str, scope: object | None = None,
    due_before: object | None = None, include_overdue: bool = True, limit: int = 100,
) -> dict[str, Any]:
    cutoff = _datetime(due_before, field="due_before") or (
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows, truncated = _record_candidates(db, owner_id=owner_id, scope=scope)
    terminal = {
        "subscription": {"cancelled", "expired"}, "loan": {"paid", "closed"},
        "tax_item": {"paid", "closed"}, "bill": {"paid", "voided"},
        "receivable": {"received", "voided"},
    }
    status_key = {
        "subscription": "subscription_status", "loan": "loan_status",
        "tax_item": "tax_status", "bill": "bill_status",
        "receivable": "receivable_status",
    }
    due: list[dict[str, Any]] = []
    for row in rows:
        props = validate_finance_properties(row.properties or {})
        kind = props["record_type"]
        if kind not in _DUE_TYPES or row.due_at is None or row.due_at > cutoff:
            continue
        if not include_overdue and row.due_at < now:
            continue
        if props["details"].get(status_key[kind]) in terminal[kind]:
            continue
        serialized = serialize_finance_record(row)
        serialized["overdue"] = row.due_at < now
        due.append(serialized)
    due.sort(key=lambda row: (row["due_at"] or "", row["id"]))
    bounded = max(1, min(100, int(limit)))
    items = due[:bounded]
    return {
        "items": items, "count": len(items), "due_before": _iso(cutoff, field="due_before"),
        "truncated": truncated or len(due) > bounded,
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def finance_anomaly_input(
    db, *, owner_id: str, scope: object | None = None,
    currency: object | None = None, from_at: object | None = None,
    to_at: object | None = None, limit: int = 100,
) -> dict[str, Any]:
    """Return explainable candidate signals, not fraud findings or advice."""
    rows, truncated = _record_candidates(
        db, owner_id=owner_id, scope=scope, currency=currency,
        from_at=from_at, to_at=to_at,
    )
    records = [
        serialize_finance_record(row)
        for row in rows
        if validate_finance_properties(row.properties or {})["record_type"]
        in {"income", "expense"}
    ]
    signals: list[dict[str, Any]] = []
    duplicate_groups: dict[tuple[str, str, str, str, str], list[str]] = defaultdict(list)
    expenses_by_currency: dict[str, list[tuple[Decimal, dict[str, Any]]]] = defaultdict(list)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for record in records:
        details = record["details"]
        party = details.get("merchant") or details.get("counterparty") or ""
        key = (
            record["record_type"], record["scope"], record["currency"],
            record["amount"], f"{record['effective_at']}|{str(party).casefold()}",
        )
        duplicate_groups[key].append(record["id"])
        if record["record_type"] == "expense":
            expenses_by_currency[record["currency"]].append((Decimal(record["amount"]), record))
            if not str(details.get("category") or "").strip():
                signals.append({
                    "kind": "missing_category", "record_ids": [record["id"]],
                    "reason": "Expense has no category, which limits aggregation quality.",
                    "severity": "info",
                })
        if details.get("transaction_status") == "pending":
            when = _datetime(record["effective_at"], field="effective_at")
            if when and when < now - timedelta(days=7):
                signals.append({
                    "kind": "stale_pending", "record_ids": [record["id"]],
                    "reason": "Transaction is still marked pending more than 7 days after its effective date.",
                    "severity": "review",
                })
    for ids in duplicate_groups.values():
        if len(ids) > 1:
            signals.append({
                "kind": "possible_duplicate", "record_ids": sorted(ids),
                "reason": "Records share type, scope, currency, amount, date/time, and counterparty label.",
                "severity": "review",
            })
    for item_currency, values in expenses_by_currency.items():
        if len(values) < 5:
            continue
        median = statistics.median([amount for amount, _ in values])
        if median <= 0:
            continue
        threshold = median * Decimal(3)
        for amount, record in values:
            if amount >= threshold:
                signals.append({
                    "kind": "large_vs_median", "record_ids": [record["id"]],
                    "reason": (
                        f"Expense {record['amount']} {item_currency} is at least 3x "
                        f"the {format(median, 'f')} {item_currency} median across {len(values)} records."
                    ),
                    "severity": "review", "comparison_count": len(values),
                })
    signals.sort(key=lambda row: (row["kind"], row["record_ids"]))
    bounded = max(1, min(100, int(limit)))
    return {
        "signals": signals[:bounded], "count": len(signals[:bounded]),
        "scanned": len(records), "truncated": truncated or len(signals) > bounded,
        "interpretation": (
            "Signals are deterministic record-quality or comparison inputs. They are not "
            "fraud determinations and do not recommend a financial action."
        ),
        "rules": {
            "possible_duplicate": "same type, scope, currency, amount, effective time, and party label",
            "large_vs_median": "at least 5 same-currency expenses and amount at least 3x median",
            "stale_pending": "pending more than 7 days",
        },
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def _latest_balance_records(rows: Sequence[LifeEntity]) -> list[LifeEntity]:
    latest: dict[tuple[str, str, str], LifeEntity] = {}
    for row in rows:
        properties = validate_finance_properties(row.properties or {})
        if properties["record_type"] != "balance_observation":
            continue
        details = properties["details"]
        reference = str(
            details.get("account_entity_id")
            or details.get("reference_last4")
            or row.title
        )
        key = (properties["currency"], details["balance_kind"], reference)
        existing = latest.get(key)
        if existing is None or (
            row.occurred_at or datetime.min, row.id
        ) > (
            existing.occurred_at or datetime.min, existing.id
        ):
            latest[key] = row
    return list(latest.values())


def finance_net_worth(
    db, *, owner_id: str, as_of: object,
    scope: object | None = None,
) -> dict[str, Any]:
    """Calculate record-backed net worth without exchange-rate guessing."""

    clock, as_of_offset = _aware_as_of(as_of)
    rows, truncated = _record_candidates(
        db, owner_id=owner_id, scope=scope, to_at=clock,
    )
    latest_balances = _latest_balance_records(rows)
    assets: dict[str, Decimal] = defaultdict(Decimal)
    liabilities: dict[str, Decimal] = defaultdict(Decimal)
    evidence: list[dict[str, Any]] = []
    asset_kinds = {"cash", "account", "card_available", "investment"}
    liability_kinds = {"card_owed", "loan"}
    represented: set[tuple[str, str]] = set()
    for row in latest_balances:
        properties = validate_finance_properties(row.properties or {})
        kind = properties["details"]["balance_kind"]
        currency = properties["currency"]
        amount = Decimal(properties["amount"])
        if kind in asset_kinds:
            assets[currency] += amount
        elif kind in liability_kinds:
            liabilities[currency] += amount
        represented.add((currency, kind))
        evidence.append({
            "record_id": row.id,
            "record_type": "balance_observation",
            "balance_kind": kind,
            "currency": currency,
            "amount": properties["amount"],
            "effective_at": properties["effective_at"],
        })

    # Typed observations remain useful when no canonical balance observation
    # for that currency/kind exists. Never double-count both authorities.
    latest_typed: dict[tuple[str, str], LifeEntity] = {}
    for row in rows:
        properties = validate_finance_properties(row.properties or {})
        record_type = properties["record_type"]
        if record_type not in {"investment_observation", "loan"}:
            continue
        details = properties["details"]
        reference = str(
            details.get("symbol") or details.get("asset_label")
            or details.get("lender") or row.id
        )
        key = (record_type, reference.casefold())
        existing = latest_typed.get(key)
        if existing is None or (
            row.occurred_at or datetime.min, row.id
        ) > (
            existing.occurred_at or datetime.min, existing.id
        ):
            latest_typed[key] = row
    for row in latest_typed.values():
        properties = validate_finance_properties(row.properties or {})
        record_type = properties["record_type"]
        currency = properties["currency"]
        balance_kind = "investment" if record_type == "investment_observation" else "loan"
        if (currency, balance_kind) in represented:
            continue
        if record_type == "loan" and properties["details"].get("loan_status") in {"paid", "closed"}:
            continue
        amount = Decimal(properties["amount"])
        (assets if record_type == "investment_observation" else liabilities)[currency] += amount
        evidence.append({
            "record_id": row.id,
            "record_type": record_type,
            "balance_kind": balance_kind,
            "currency": currency,
            "amount": properties["amount"],
            "effective_at": properties["effective_at"],
        })

    currencies = sorted(set(assets) | set(liabilities))
    totals = {
        currency: {
            "assets": format(assets[currency], "f"),
            "liabilities": format(liabilities[currency], "f"),
            "net_worth": format(assets[currency] - liabilities[currency], "f"),
        }
        for currency in currencies
    }
    evidence.sort(key=lambda item: (item["currency"], item["balance_kind"], item["record_id"]))
    return {
        "as_of": clock.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of_offset": as_of_offset,
        "scope": str(scope).lower() if scope else None,
        "totals": totals,
        "evidence": evidence,
        "evidence_count": len(evidence),
        "truncated": truncated,
        "exchange_rates_used": False,
        "method": (
            "Latest balance per currency, balance kind, and masked account reference; "
            "typed investment/loan observations only fill an absent balance kind."
        ),
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def finance_forecast(
    db, *, owner_id: str, as_of: object,
    scope: object | None = None, currency: object | None = None,
    horizon_days: int = 30, lookback_days: int = 90,
) -> dict[str, Any]:
    """Project bounded cash flow from stored history and dated obligations."""

    clock, as_of_offset = _aware_as_of(as_of)
    try:
        horizon = int(horizon_days)
        lookback = int(lookback_days)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError("horizon_days and lookback_days must be integers") from exc
    if not 1 <= horizon <= 365 or not 7 <= lookback <= 730:
        raise LifeGraphError(
            "horizon_days must be 1–365 and lookback_days must be 7–730"
        )
    normalized_currency = _currency(currency, required=False) if currency else None
    history_start = clock - timedelta(days=lookback)
    horizon_end = clock + timedelta(days=horizon)
    rows, truncated = _record_candidates(
        db, owner_id=owner_id, scope=scope, currency=normalized_currency,
        to_at=horizon_end,
    )
    historic_income: dict[str, Decimal] = defaultdict(Decimal)
    historic_expense: dict[str, Decimal] = defaultdict(Decimal)
    due_payables: dict[str, Decimal] = defaultdict(Decimal)
    due_receivables: dict[str, Decimal] = defaultdict(Decimal)
    historic_ids: list[str] = []
    due_ids: list[str] = []
    expense_total = 0
    expense_classified = 0
    terminal = {
        "subscription": {"cancelled", "expired"}, "loan": {"paid", "closed"},
        "tax_item": {"paid", "closed"}, "bill": {"paid", "voided"},
        "receivable": {"received", "voided"},
    }
    status_key = {
        "subscription": "subscription_status", "loan": "loan_status",
        "tax_item": "tax_status", "bill": "bill_status",
        "receivable": "receivable_status",
    }
    for row in rows:
        properties = validate_finance_properties(row.properties or {})
        record_type = properties["record_type"]
        item_currency = properties["currency"]
        if record_type in {"income", "expense"} and row.occurred_at is not None:
            if history_start <= row.occurred_at <= clock and properties["details"].get("transaction_status") not in {"voided", "refunded"}:
                target = historic_income if record_type == "income" else historic_expense
                target[item_currency] += Decimal(properties["amount"])
                historic_ids.append(row.id)
                if record_type == "expense":
                    expense_total += 1
                    if properties["details"].get("category") != "uncategorized":
                        expense_classified += 1
        if record_type not in _DUE_TYPES or row.due_at is None:
            continue
        if not (clock < row.due_at <= horizon_end):
            continue
        if properties["details"].get(status_key[record_type]) in terminal[record_type]:
            continue
        target = due_receivables if record_type == "receivable" else due_payables
        target[item_currency] += Decimal(properties["amount"])
        due_ids.append(row.id)

    currencies = sorted(
        set(historic_income) | set(historic_expense)
        | set(due_payables) | set(due_receivables)
    )
    projections: dict[str, dict[str, str]] = {}
    for item_currency in currencies:
        projected_income = historic_income[item_currency] / Decimal(lookback) * Decimal(horizon)
        projected_expense = historic_expense[item_currency] / Decimal(lookback) * Decimal(horizon)
        projected_net = (
            projected_income - projected_expense
            - due_payables[item_currency] + due_receivables[item_currency]
        )
        projections[item_currency] = {
            "historic_income": format(historic_income[item_currency], ".2f"),
            "historic_expense": format(historic_expense[item_currency], ".2f"),
            "projected_income": format(projected_income, ".2f"),
            "projected_expense": format(projected_expense, ".2f"),
            "dated_payables": format(due_payables[item_currency], ".2f"),
            "dated_receivables": format(due_receivables[item_currency], ".2f"),
            "projected_net_change": format(projected_net, ".2f"),
        }
    return {
        "as_of": clock.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of_offset": as_of_offset,
        "horizon_days": horizon,
        "lookback_days": lookback,
        "scope": str(scope).lower() if scope else None,
        "currency": normalized_currency,
        "projections": projections,
        "classification_coverage": {
            "classified_expenses": expense_classified,
            "total_expenses": expense_total,
            "percent": round(expense_classified / expense_total * 100) if expense_total else None,
        },
        "evidence": {
            "historic_record_ids": sorted(historic_ids),
            "dated_obligation_ids": sorted(due_ids),
        },
        "assumptions": [
            "Historic daily income and expense averages continue for the selected horizon.",
            "Only explicitly dated open obligations are added; no exchange-rate conversion is attempted.",
            "The result is a deterministic scenario, not a prediction or recommendation.",
        ],
        "truncated": truncated,
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def finance_affordability(
    db, *, owner_id: str, as_of: object, amount: object,
    currency: object, scope: object | None = None,
    horizon_days: int = 30, lookback_days: int = 90,
) -> dict[str, Any]:
    """Compare one hypothetical cost with recorded liquid funds and forecast."""

    requested = _decimal(amount, field="amount", required=True)
    assert requested is not None
    if Decimal(requested) <= 0:
        raise LifeGraphError("amount must be greater than zero")
    item_currency = _currency(currency, required=True)
    assert item_currency is not None
    clock, as_of_offset = _aware_as_of(as_of)
    rows, truncated = _record_candidates(
        db, owner_id=owner_id, scope=scope, currency=item_currency, to_at=clock,
    )
    liquid = Decimal(0)
    liquid_ids: list[str] = []
    for row in _latest_balance_records(rows):
        properties = validate_finance_properties(row.properties or {})
        if properties["currency"] != item_currency:
            continue
        if properties["details"]["balance_kind"] in {"cash", "account"}:
            liquid += Decimal(properties["amount"])
            liquid_ids.append(row.id)
    forecast = finance_forecast(
        db, owner_id=owner_id, as_of=as_of, scope=scope,
        currency=item_currency, horizon_days=horizon_days,
        lookback_days=lookback_days,
    )
    projection = forecast["projections"].get(item_currency)
    projected_change = (
        Decimal(projection["projected_net_change"]) if projection else Decimal(0)
    )
    available = liquid + projected_change
    if not liquid_ids:
        status = "insufficient_evidence"
        reason = "No latest cash/account balance observation was stored."
    elif available >= Decimal(requested):
        status = "supported_by_recorded_inputs"
        reason = "Recorded liquid funds plus the bounded scenario cover the hypothetical cost."
    else:
        status = "not_supported_by_recorded_inputs"
        reason = "Recorded liquid funds plus the bounded scenario do not cover the hypothetical cost."
    return {
        "as_of": clock.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "as_of_offset": as_of_offset,
        "scope": str(scope).lower() if scope else None,
        "currency": item_currency,
        "amount": requested,
        "recorded_liquid_funds": format(liquid, ".2f"),
        "projected_net_change": format(projected_change, ".2f"),
        "record_backed_available": format(available, ".2f"),
        "status": status,
        "reason": reason,
        "evidence": {
            "liquid_balance_record_ids": sorted(liquid_ids),
            **forecast["evidence"],
        },
        "assumptions": forecast["assumptions"],
        "can_execute_purchase_or_transfer": False,
        "truncated": truncated or forecast["truncated"],
        "analysis_notice": FINANCE_ANALYSIS_NOTICE,
    }


def finance_record_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_finance_record(db, owner_id, entity_id)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    chronological = list(reversed(rows))
    result: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for row in chronological:
        snapshot = dict(row.snapshot or {})
        properties = validate_finance_properties(snapshot.get("properties") or {})
        changed: list[str] = []
        if previous is not None:
            previous_props = validate_finance_properties(previous.get("properties") or {})
            for field in (
                "title", "summary", "status", "occurred_at", "due_at",
                "confidence", "sensitivity", "provenance",
            ):
                if previous.get(field) != snapshot.get(field):
                    changed.append(field)
            for field in (
                "scope", "effective_at", "amount", "currency", "period_start",
                "period_end", "details", "source",
            ):
                if previous_props.get(field) != properties.get(field):
                    changed.append(field)
        result.append({
            "id": row.id, "version": int(row.version),
            "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "reason": row.reason or "",
            "kind": "created" if previous is None else "finance_record_changed",
            "changed_fields": changed,
            "record": {
                "title": snapshot.get("title") or "", "status": snapshot.get("status") or "active",
                "record_type": properties["record_type"], "scope": properties["scope"],
                "effective_at": properties["effective_at"], "due_at": properties["due_at"],
                "amount": properties["amount"], "currency": properties["currency"],
            },
        })
        previous = snapshot
    return list(reversed(result)), truncated
