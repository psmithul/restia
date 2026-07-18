import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

from tests.helpers.cli_loader import load_script


def _load_cli(monkeypatch):
    routes = types.ModuleType("routes.contacts_routes")
    routes._get_carddav_config = MagicMock()
    routes._fetch_contacts = MagicMock()
    routes._create_contact = MagicMock()
    monkeypatch.setitem(sys.modules, "routes.contacts_routes", routes)
    return load_script("odysseus-contacts")


def test_contact_rows_skips_invalid_rows(monkeypatch):
    cli = _load_cli(monkeypatch)

    assert cli._contact_rows([
        {"name": "Ada", "email": "ada@example.test"},
        "bad-row",
        None,
    ]) == [{"name": "Ada", "email": "ada@example.test"}]


def test_search_matches_every_email_not_only_the_first(monkeypatch):
    cli = _load_cli(monkeypatch)
    rows = [
        {
            "name": "Ada Lovelace",
            "emails": ["primary@example.test", "secondary@example.test"],
        },
        {
            "name": "Grace Hopper",
            "emails": ["grace@example.test"],
        },
    ]
    emitted = []
    monkeypatch.setattr(
        cli,
        "_with_owner",
        lambda _args, _operation, **_kwargs: rows,
    )
    monkeypatch.setattr(cli, "emit", lambda value, _args: emitted.append(value))

    cli.cmd_search(SimpleNamespace(query="secondary", pretty=False))

    assert emitted == [rows[:1]]
