from __future__ import annotations


def test_mcp_email_reads_accounts_through_sqlalchemy_for_postgresql(monkeypatch):
    from core import database as core_database
    from mcp_servers import email_server

    row = core_database.EmailAccount(
        id="acct-shared",
        owner="alice",
        name="Shared Mail",
        is_default=True,
        enabled=True,
        imap_host="imap.example.com",
        imap_port=993,
        imap_user="alice@example.com",
        imap_password="encrypted-imap",
        imap_starttls=False,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_security="ssl",
        smtp_user="alice@example.com",
        smtp_password="encrypted-smtp",
        from_address="alice@example.com",
    )

    class Query:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return [row]

    class Session:
        closed = False

        def query(self, model):
            assert model is core_database.EmailAccount
            return Query()

        def close(self):
            self.closed = True

    session = Session()
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://restia@example/db")
    monkeypatch.setattr(core_database, "SessionLocal", lambda: session)
    monkeypatch.setattr(
        email_server,
        "_db_path",
        lambda: (_ for _ in ()).throw(AssertionError("SQLite path must not be used")),
    )

    accounts = email_server._read_accounts_from_db()

    assert accounts == [
        {
            "id": "acct-shared",
            "owner": "alice",
            "name": "Shared Mail",
            "is_default": True,
            "enabled": True,
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "imap_user": "alice@example.com",
            "imap_password": "encrypted-imap",
            "imap_starttls": False,
            "smtp_host": "smtp.example.com",
            "smtp_port": 465,
            "smtp_security": "ssl",
            "smtp_user": "alice@example.com",
            "smtp_password": "encrypted-smtp",
            "from_address": "alice@example.com",
        }
    ]
    assert session.closed is True


def test_mcp_email_default_document_owner_uses_unified_auth(monkeypatch):
    from mcp_servers import email_server
    from src import auth_runtime

    class Auth:
        def list_users(self):
            return [
                {"username": "member", "is_admin": False},
                {"username": "admin", "is_admin": True},
            ]

    monkeypatch.delenv("ODYSSEUS_DOCUMENT_OWNER", raising=False)
    monkeypatch.setattr(auth_runtime, "get_auth_manager", lambda: Auth())

    assert email_server._default_document_owner() == "admin"
