"""Focused contracts for V3 Personal Finance typed records."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import ActionAudit, Base, LifeEntity, LifeEntityVersion
from routes.life_routes import setup_life_routes
from src.finance_service import (
    FINANCE_ANALYSIS_NOTICE,
    FINANCE_RECORD_TYPES,
    classify_expense_category,
    create_finance_record,
    finance_affordability,
    finance_anomaly_input,
    finance_cash_flow,
    finance_forecast,
    finance_net_worth,
    finance_record_history,
    finance_summary,
    get_finance_record,
    list_due_finance_records,
    list_finance_records,
    update_finance_record,
)
from src.identity import ensure_account
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return bool(self.users)


@pytest.fixture()
def finance_env(tmp_path):
    db_path = tmp_path / "finance.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_life_routes(session_factory=factory))
    yield SimpleNamespace(app=app, Session=factory, engine=engine)
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _payload(record_type: str, **overrides):
    payload = {
        "record_type": record_type,
        "title": record_type.replace("_", " ").title(),
        "scope": "personal",
        "effective_at": datetime(2026, 7, 1, 8),
        "source": {"kind": "manual", "label": "User entry"},
        "amount": "100",
        "currency": "INR",
        "details": {},
        "note": "Private finance record",
        "provenance": {"capture": "manual"},
    }
    typed = {
        "account": {
            "amount": None,
            "details": {
                "account_kind": "bank", "display_name": "Daily account",
                "institution": "Local bank", "reference_last4": "1234",
            },
        },
        "balance_observation": {"details": {"balance_kind": "account"}},
        "income": {
            "details": {"category": "salary", "counterparty": "Employer"}
        },
        "expense": {
            "details": {"category": "travel", "merchant": "Bus operator"}
        },
        "subscription": {
            "due_at": datetime(2026, 7, 15),
            "details": {"provider": "Cloud service", "cadence": "monthly"},
        },
        "investment_observation": {
            "details": {
                "asset_label": "Index fund", "symbol": "INDEX",
                "quantity": "10.5", "quantity_unit": "units",
            }
        },
        "loan": {
            "due_at": datetime(2026, 7, 20),
            "details": {"lender": "Lender", "loan_kind": "student"},
        },
        "tax_item": {
            "due_at": datetime(2026, 7, 25),
            "details": {
                "jurisdiction": "India", "tax_year": 2026,
                "item_kind": "estimated tax",
            },
        },
        "bill": {
            "due_at": datetime(2026, 7, 8),
            "details": {"payee": "Utility", "category": "utilities"},
        },
        "receivable": {
            "due_at": datetime(2026, 7, 9),
            "details": {"payer": "Client", "category": "consulting"},
        },
        "budget_goal": {
            "period_start": datetime(2026, 7, 1),
            "period_end": datetime(2026, 7, 31, 23, 59),
            "details": {"target_kind": "spending_limit", "category": "travel"},
        },
        "document_reference": {
            "amount": None,
            "currency": None,
            "details": {
                "document_kind": "receipt", "storage_ref": "files/receipt-july.pdf"
            },
        },
    }
    payload.update(typed[record_type])
    payload.update(overrides)
    return payload


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize("record_type", sorted(FINANCE_RECORD_TYPES))
def test_all_finance_types_use_account_scoped_canonical_life_entity(
    finance_env, record_type
):
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        entity, created = create_finance_record(
            db, account=alice, **_payload(record_type)
        )
        db.commit()

        assert created is True
        assert entity.entity_type == "finance_record"
        assert entity.owner_id == alice.id
        assert entity.properties["finance_schema_version"] == 1
        assert entity.properties["record_type"] == record_type
        assert entity.properties["scope"] == "personal"
        assert db.query(LifeEntity).filter_by(id=entity.id).one().id == entity.id
    finally:
        db.close()


def test_finance_rejects_secrets_full_numbers_and_executor_payloads_and_masks_last4(
    finance_env,
):
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        with pytest.raises(LifeGraphError, match="credentials or secrets"):
            create_finance_record(
                db, account=alice,
                **_payload("account", details={
                    "account_kind": "bank", "display_name": "Unsafe",
                    "password": "never-store-this",
                }),
            )
        with pytest.raises(LifeGraphError, match="full card or financial account number"):
            create_finance_record(
                db, account=alice,
                **_payload("expense", note="Card 4111 1111 1111 1111"),
            )
        with pytest.raises(LifeGraphError, match="cannot execute"):
            create_finance_record(
                db, account=alice,
                **_payload("expense", provenance={"execute": "transfer"}),
            )
        with pytest.raises(LifeGraphError, match="too large"):
            create_finance_record(
                db, account=alice, **_payload("expense", amount="1e999999")
            )
        entity, _ = create_finance_record(db, account=alice, **_payload("account"))
        db.commit()
        assert entity.properties["details"]["reference_last4"] == "••••1234"
        assert "1234" not in entity.summary
        assert get_finance_record(
            db, owner_id=alice.id, entity_id=entity.id
        )["details"]["reference_last4"] == "••••1234"
        updated = update_finance_record(
            db, account=alice, entity_id=entity.id, expected_version=1,
            changes={"note": "Metadata reviewed"},
        )
        assert updated.properties["details"]["reference_last4"] == "••••1234"
    finally:
        db.close()


def test_finance_owner_isolation_and_personal_business_scope(finance_env):
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        personal, _ = create_finance_record(db, account=alice, **_payload("expense"))
        create_finance_record(
            db, account=alice, **_payload("expense", scope="business", title="Business expense")
        )
        create_finance_record(
            db, account=bob, **_payload("expense", title="Bob private expense", amount="9999")
        )
        db.commit()

        with pytest.raises(LifeGraphNotFound, match="not found"):
            get_finance_record(db, owner_id=bob.id, entity_id=personal.id)
        business, _ = list_finance_records(db, owner_id=alice.id, scope="business")
        assert [row["title"] for row in business] == ["Business expense"]
        alice_rows, _ = list_finance_records(db, owner_id=alice.id)
        assert all(row["title"] != "Bob private expense" for row in alice_rows)

        bob_account, _ = create_finance_record(
            db, account=bob, **_payload("account", title="Bob account")
        )
        db.flush()
        with pytest.raises(LifeGraphNotFound, match="not found"):
            create_finance_record(
                db, account=alice,
                **_payload(
                    "expense",
                    details={
                        "category": "travel",
                        "account_entity_id": bob_account.id,
                    },
                ),
            )
    finally:
        db.close()


def test_finance_optimistic_versions_audit_and_history(finance_env):
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        entity, _ = create_finance_record(db, account=alice, **_payload("expense"))
        updated = update_finance_record(
            db, account=alice, entity_id=entity.id, expected_version=1,
            changes={"amount": "125.50", "note": "Corrected from receipt"},
        )
        with pytest.raises(LifeGraphConflict, match="another client"):
            update_finance_record(
                db, account=alice, entity_id=entity.id, expected_version=1,
                changes={"amount": "200"},
            )
        db.commit()

        assert updated.version == 2
        history, truncated = finance_record_history(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [2, 1]
        assert set(history[0]["changed_fields"]) == {"summary", "amount"}
        assert db.query(LifeEntityVersion).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 2
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 2
    finally:
        db.close()


def test_summary_cash_flow_and_anomaly_inputs_are_bounded_and_explainable(finance_env):
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        create_finance_record(
            db, account=alice,
            **_payload("income", title="Salary", amount="1000", effective_at=datetime(2026, 7, 1)),
        )
        amounts = ["10", "10", "10", "10", "100"]
        for index, amount in enumerate(amounts):
            create_finance_record(
                db, account=alice,
                **_payload(
                    "expense", title=f"Expense {index}", amount=amount,
                    effective_at=datetime(2026, 7, index + 1, 12),
                    idempotency_key=f"expense-{index}",
                ),
            )
        duplicate = _payload(
            "expense", title="Duplicate A", amount="25",
            effective_at=datetime(2026, 7, 10, 12),
            details={"category": "food", "merchant": "Cafe"},
        )
        create_finance_record(db, account=alice, **duplicate)
        duplicate["title"] = "Duplicate B"
        create_finance_record(db, account=alice, **duplicate)
        create_finance_record(
            db, account=alice,
            **_payload("expense", title="Business", scope="business", amount="500"),
        )
        db.commit()

        summary = finance_summary(db, owner_id=alice.id, scope="personal")
        assert summary["cash_flow"]["income"]["INR"] == "1000"
        assert summary["cash_flow"]["expense"]["INR"] == "190"
        assert summary["cash_flow"]["net"]["INR"] == "810"
        assert summary["analysis_notice"] == FINANCE_ANALYSIS_NOTICE

        flow = finance_cash_flow(
            db, owner_id=alice.id, scope="personal", currency="INR", group_by="month"
        )
        assert flow["buckets"] == [{
            "period_start": "2026-07-01", "currency": "INR", "income": "1000",
            "expense": "190", "net": "810", "count": 8,
        }]
        anomaly = finance_anomaly_input(
            db, owner_id=alice.id, scope="personal", currency="INR"
        )
        assert any(row["kind"] == "possible_duplicate" for row in anomaly["signals"])
        assert any(row["kind"] == "large_vs_median" for row in anomaly["signals"])
        assert "not fraud determinations" in anomaly["interpretation"]
        assert anomaly["analysis_notice"] == FINANCE_ANALYSIS_NOTICE
    finally:
        db.close()


def test_due_records_exclude_terminal_states_and_remain_owner_scoped(finance_env):
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        due_at = datetime.now() + timedelta(days=5)
        create_finance_record(
            db, account=alice,
            **_payload("bill", title="Due utility", due_at=due_at),
        )
        create_finance_record(
            db, account=alice,
            **_payload(
                "bill", title="Paid utility", due_at=due_at,
                details={"payee": "Utility", "bill_status": "paid"},
            ),
        )
        create_finance_record(
            db, account=bob,
            **_payload("bill", title="Bob due", due_at=due_at),
        )
        db.commit()

        due = list_due_finance_records(
            db, owner_id=alice.id, due_before=due_at + timedelta(days=1)
        )
        assert [row["title"] for row in due["items"]] == ["Due utility"]
    finally:
        db.close()


def test_expense_classification_is_deterministic_and_preserves_user_categories(
    finance_env,
):
    assert classify_expense_category(
        title="Airport taxi", merchant="Ola"
    ) == {
        "category": "transport",
        "category_source": "deterministic_v1",
        "category_confidence": 85,
        "category_rule": "keyword:taxi",
    }
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        inferred, _ = create_finance_record(
            db,
            account=alice,
            **_payload(
                "expense", title="Dinner at local restaurant",
                details={"merchant": "Local restaurant"},
            ),
        )
        explicit, _ = create_finance_record(
            db,
            account=alice,
            **_payload(
                "expense", title="Taxi for client meeting",
                details={"category": "business_travel", "merchant": "Ola"},
            ),
        )
        db.commit()
        assert inferred.properties["details"] == {
            "category": "food",
            "merchant": "Local restaurant",
            "category_source": "deterministic_v1",
            "category_confidence": 85,
            "category_rule": "keyword:restaurant",
            "transaction_status": "posted",
        }
        assert explicit.properties["details"]["category"] == "business_travel"
        assert "category_source" not in explicit.properties["details"]
    finally:
        db.close()


def test_net_worth_forecast_and_affordability_are_owner_scoped_evidence_only(
    finance_env,
):
    as_of = "2026-07-17T12:00:00+05:30"
    clock = datetime(2026, 7, 17, 6, 30)
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        common = {
            "account": alice,
            "scope": "personal",
            "source": {"kind": "manual", "label": "User entry"},
            "currency": "INR",
            "provenance": {"interface": "test"},
        }
        create_finance_record(
            db, **common, record_type="balance_observation",
            title="Daily account", effective_at=clock - timedelta(days=2),
            amount="4000", details={"balance_kind": "account"},
        )
        latest_balance, _ = create_finance_record(
            db, **common, record_type="balance_observation",
            title="Daily account", effective_at=clock - timedelta(hours=1),
            amount="5000", details={"balance_kind": "account"},
        )
        create_finance_record(
            db, **common, record_type="balance_observation",
            title="Card owed", effective_at=clock - timedelta(hours=2),
            amount="100", details={"balance_kind": "card_owed"},
        )
        loan, _ = create_finance_record(
            db, **common, record_type="loan", title="Education loan",
            effective_at=clock - timedelta(days=40), due_at=clock + timedelta(days=20),
            amount="50", details={"lender": "Lender", "loan_kind": "student"},
        )
        income, _ = create_finance_record(
            db, **common, record_type="income", title="Consulting income",
            effective_at=clock - timedelta(days=10), amount="3000",
            details={"category": "consulting", "counterparty": "Client"},
        )
        expense, _ = create_finance_record(
            db, **common, record_type="expense", title="Grocery expense",
            effective_at=clock - timedelta(days=5), amount="600",
            details={"merchant": "Grocery store"},
        )
        old_bill, _ = create_finance_record(
            db, **common, record_type="bill", title="Annual fee",
            effective_at=clock - timedelta(days=200), due_at=clock + timedelta(days=5),
            amount="500", details={"payee": "Provider", "category": "fees"},
        )
        receivable, _ = create_finance_record(
            db, **common, record_type="receivable", title="Client receivable",
            effective_at=clock - timedelta(days=60), due_at=clock + timedelta(days=4),
            amount="100", details={"payer": "Client", "category": "consulting"},
        )
        create_finance_record(
            db, account=bob, record_type="balance_observation",
            title="Bob account", scope="personal", effective_at=clock,
            amount="999999", currency="INR",
            details={"balance_kind": "account"},
            source={"kind": "manual", "label": "Bob entry"},
        )
        create_finance_record(
            db, **common, record_type="balance_observation",
            title="Future account", effective_at=clock + timedelta(days=1),
            amount="888888", details={"balance_kind": "account"},
        )
        db.commit()

        worth = finance_net_worth(db, owner_id=alice.id, as_of=as_of)
        assert worth["totals"] == {
            "INR": {"assets": "5000", "liabilities": "150", "net_worth": "4850"}
        }
        assert latest_balance.id in {row["record_id"] for row in worth["evidence"]}
        assert worth["exchange_rates_used"] is False

        forecast = finance_forecast(
            db, owner_id=alice.id, as_of=as_of, currency="INR",
            horizon_days=30, lookback_days=30,
        )
        assert forecast["projections"]["INR"] == {
            "historic_income": "3000.00", "historic_expense": "600.00",
            "projected_income": "3000.00", "projected_expense": "600.00",
            "dated_payables": "550.00", "dated_receivables": "100.00",
            "projected_net_change": "1950.00",
        }
        assert forecast["evidence"] == {
            "historic_record_ids": sorted([income.id, expense.id]),
            "dated_obligation_ids": sorted([loan.id, old_bill.id, receivable.id]),
        }
        assert forecast["classification_coverage"]["percent"] == 100

        decision = finance_affordability(
            db, owner_id=alice.id, as_of=as_of, amount="6000",
            currency="INR", horizon_days=30, lookback_days=30,
        )
        assert decision["status"] == "supported_by_recorded_inputs"
        assert decision["record_backed_available"] == "6950.00"
        assert decision["can_execute_purchase_or_transfer"] is False
        assert "999999" not in str(worth) + str(forecast) + str(decision)
    finally:
        db.close()


def test_finance_scenarios_require_explicit_offset_and_do_not_convert_currency(
    finance_env,
):
    db = finance_env.Session()
    try:
        alice = _account(db, "alice")
        with pytest.raises(LifeGraphError, match="explicit UTC offset"):
            finance_net_worth(db, owner_id=alice.id, as_of="2026-07-17T12:00:00")
        result = finance_affordability(
            db, owner_id=alice.id, as_of="2026-07-17T12:00:00+05:30",
            amount="100", currency="USD",
        )
        assert result["status"] == "insufficient_evidence"
        assert result["recorded_liquid_funds"] == "0.00"
        assert result["can_execute_purchase_or_transfer"] is False
    finally:
        db.close()


@pytest.mark.anyio
async def test_finance_routes_crud_import_idempotency_atomicity_and_generic_bypass(
    finance_env,
):
    create = await _call(finance_env, "POST", "/api/life/finance/records", json={
        **_payload("expense", amount=42.25),
        "effective_at": "2026-07-01T08:00:00",
    })
    assert create.status_code == 201, create.text
    record = create.json()["record"]
    assert record["amount"] == "42.25"
    assert record["execution_policy"] == {
        "risk_level": 6, "record_only": True, "can_execute_financial_action": False,
    }

    bob_read = await _call(
        finance_env, "GET", f"/api/life/finance/records/{record['id']}", user="bob"
    )
    assert bob_read.status_code == 404
    update = await _call(
        finance_env, "PATCH", f"/api/life/finance/records/{record['id']}",
        json={"version": 1, "amount": "44", "note": "Receipt verified"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["record"]["version"] == 2

    generic_update = await _call(
        finance_env, "PATCH", f"/api/life/entities/{record['id']}",
        json={"version": 2, "title": "Bypass"},
    )
    assert generic_update.status_code == 400
    assert "typed Finance" in generic_update.text
    generic_delete = await _call(
        finance_env, "DELETE", f"/api/life/entities/{record['id']}",
        json={"version": 2},
    )
    assert generic_delete.status_code == 400

    import_rows = []
    for suffix, amount in (("a", "10"), ("b", "20")):
        item = _payload(
            "expense", title=f"Imported {suffix}", amount=amount,
            source={
                "kind": "statement", "label": "July statement",
                "external_id": f"txn-{suffix}",
            },
            idempotency_key=f"statement-{suffix}",
        )
        item["effective_at"] = "2026-07-02T08:00:00"
        import_rows.append(item)
    first_import = await _call(
        finance_env, "POST", "/api/life/finance/import", json={"records": import_rows}
    )
    assert first_import.status_code == 201, first_import.text
    assert first_import.json()["created_count"] == 2
    replay = await _call(
        finance_env, "POST", "/api/life/finance/import", json={"records": import_rows}
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["created_count"] == 0

    bad_batch = [dict(import_rows[0]), dict(import_rows[1])]
    bad_batch[0]["title"] = "Atomic first"
    bad_batch[0]["idempotency_key"] = "atomic-first"
    bad_batch[0]["source"] = {
        "kind": "statement", "label": "Other statement", "external_id": "atomic-a"
    }
    bad_batch[1]["title"] = "Atomic invalid"
    bad_batch[1]["idempotency_key"] = "atomic-invalid"
    bad_batch[1]["source"] = {"kind": "manual", "label": "Not importable"}
    failed = await _call(
        finance_env, "POST", "/api/life/finance/import", json={"records": bad_batch}
    )
    assert failed.status_code == 400
    search = await _call(
        finance_env, "GET", "/api/life/finance/search?q=Atomic%20first"
    )
    assert search.status_code == 200
    assert search.json()["items"] == []

    generic_create = await _call(
        finance_env, "POST", "/api/life/entities", json={
            "entity_type": "finance_record", "title": "Bypass",
            "properties": {"finance_schema_version": 1},
        },
    )
    assert generic_create.status_code == 400
    assert "typed Finance" in generic_create.text

    for path in (
        "/api/life/finance/records",
        "/api/life/finance/summary",
        "/api/life/finance/cash-flow?group_by=month",
        "/api/life/finance/subscriptions",
        "/api/life/finance/due",
        "/api/life/finance/anomaly-input",
    ):
        response = await _call(finance_env, "GET", path)
        assert response.status_code == 200, (path, response.text)

    for path, params in (
        ("/api/life/finance/net-worth", {"as_of": "2026-07-17T12:00:00+05:30"}),
        ("/api/life/finance/forecast", {"as_of": "2026-07-17T12:00:00+05:30"}),
        ("/api/life/finance/affordability", {
            "as_of": "2026-07-17T12:00:00+05:30",
            "amount": "1000", "currency": "INR",
        }),
    ):
        response = await _call(finance_env, "GET", path, params=params)
        assert response.status_code == 200, (path, response.text)
        assert response.json().get("can_execute_purchase_or_transfer") is not True

    delete = await _call(
        finance_env, "DELETE", f"/api/life/finance/records/{record['id']}",
        json={"version": 2, "reason": "Duplicate local record"},
    )
    assert delete.status_code == 200, delete.text
    assert delete.json()["record"]["status"] == "deleted"
