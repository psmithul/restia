"""Create the deterministic Restia schema baseline.

This revision is intentionally explicit: it contains frozen Alembic
operations and never imports live ORM metadata or calls ``create_all``.
Encrypted application fields use their physical database representation
(``TEXT``); encryption and decryption remain application type behavior.

Revision ID: 20260717_0002
Revises: 20260716_0001
"""

import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20260717_0002'
down_revision: Union[str, Sequence[str], None] = '20260716_0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen adoption manifest for pre-Alembic SQLite installations.  The stamp
# command imports these constants from this revision rather than consulting
# live ORM metadata, so a later model change cannot silently redefine what the
# 20260717 schema head means.
BASELINE_REQUIRED_TABLES = frozenset({
    'account_capabilities', 'account_roles', 'accounts', 'action_audit',
    'api_tokens', 'auth_identities', 'auth_import_runs', 'auth_policy',
    'auth_sessions', 'caldav_deleted_events', 'calendar_events', 'calendars',
    'chat_messages', 'comparisons', 'crew_members',
    'direct_message_attachments', 'direct_messages', 'document_versions',
    'documents', 'editor_drafts', 'email_accounts', 'entity_links',
    'gallery_albums', 'gallery_images', 'home_link', 'inbox_items',
    'integrations', 'link_guests', 'link_invites', 'local_credentials',
    'mcp_servers', 'memories', 'mfa_factors', 'mfa_recovery_codes',
    'model_endpoints', 'notes', 'outbound_chat_links', 'planning_items',
    'progression_events', 'project_activity', 'project_attachments',
    'project_checklist_items', 'project_comments', 'project_members',
    'project_quota_locks', 'project_remote_grants', 'project_stages',
    'project_work_items', 'projects', 'provider_auth_sessions',
    'remote_blocks', 'remote_contact_prefs', 'retired_auth_subjects',
    'scheduled_tasks', 'sessions', 'signatures', 'status_posts',
    'status_views', 'study_states', 'task_runs', 'user_keys', 'user_profiles',
    'user_tool_data', 'user_tools', 'webhooks',
})

# Columns that carry authentication, ownership, audit, or V3 life-core
# invariants receive a second explicit check before an existing database may be
# stamped.  Legacy bootstrap is responsible for adding them before adoption.
BASELINE_REQUIRED_COLUMNS = {
    'accounts': frozenset({
        'id', 'username', 'status', 'auth_epoch', 'last_login_at',
        'created_at', 'updated_at',
    }),
    'account_roles': frozenset({'id', 'account_id', 'role'}),
    'account_capabilities': frozenset({'account_id', 'capabilities'}),
    'auth_identities': frozenset({
        'id', 'account_id', 'provider', 'issuer', 'subject', 'state',
        'linked_at', 'last_verified_at',
    }),
    'auth_policy': frozenset({
        'id', 'signup_enabled', 'bootstrap_completed', 'version',
    }),
    'local_credentials': frozenset({
        'id', 'account_id', 'password_hash', 'algorithm', 'version',
        'password_changed_at',
    }),
    'mfa_factors': frozenset({
        'id', 'account_id', 'kind', 'state', 'secret', 'pending_secret',
        'last_used_step',
    }),
    'mfa_recovery_codes': frozenset({
        'id', 'factor_id', 'code_hash', 'digest_scheme', 'used_at',
    }),
    'auth_sessions': frozenset({
        'id', 'account_id', 'token_digest', 'digest_scheme', 'auth_epoch',
        'expires_at', 'revoked_at', 'interface', 'auth_method',
        'source_identity_id',
    }),
    'api_tokens': frozenset({
        'id', 'owner', 'account_id', 'token_hash', 'digest_scheme', 'scopes',
        'is_active', 'revoked_at', 'expires_at',
    }),
    'auth_import_runs': frozenset({
        'id', 'source_kind', 'state', 'auth_sha256', 'sessions_sha256',
        'backup_auth_path', 'backup_sessions_path',
    }),
    'action_audit': frozenset({
        'id', 'owner_id', 'action', 'entity_type', 'entity_id',
        'before_state', 'after_state', 'details', 'created_at',
    }),
    'inbox_items': frozenset({
        'id', 'owner_id', 'kind', 'status', 'source_type', 'source_ref',
        'metadata', 'idempotency_key', 'version',
    }),
    'planning_items': frozenset({
        'id', 'owner', 'status', 'due_date', 'calendar_event_uid', 'version',
    }),
    'projects': frozenset({
        'id', 'owner', 'key', 'archived', 'completed_at', 'version',
    }),
    'project_work_items': frozenset({
        'id', 'project_id', 'stage_id', 'item_number', 'archived', 'version',
    }),
    'scheduled_tasks': frozenset({
        'id', 'owner', 'task_type', 'action', 'trigger_type', 'next_run',
        'status', 'notifications_enabled',
    }),
    'email_accounts': frozenset({
        'id', 'owner', 'imap_password', 'smtp_password', 'oauth_access_token',
        'oauth_refresh_token',
    }),
}


def _install_action_audit_guards() -> None:
    """Enforce the audit log's append-only invariant below the ORM."""

    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        op.execute("""
            CREATE TRIGGER action_audit_no_update
            BEFORE UPDATE ON action_audit
            BEGIN
                SELECT RAISE(ABORT, 'ActionAudit rows are append-only');
            END
        """)
        op.execute("""
            CREATE TRIGGER action_audit_no_delete
            BEFORE DELETE ON action_audit
            BEGIN
                SELECT RAISE(ABORT, 'ActionAudit rows are append-only');
            END
        """)
    elif dialect == 'postgresql':
        op.execute("""
            CREATE FUNCTION restia_reject_action_audit_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'ActionAudit rows are append-only';
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER action_audit_no_update_or_delete
            BEFORE UPDATE OR DELETE ON action_audit
            FOR EACH ROW EXECUTE FUNCTION restia_reject_action_audit_mutation()
        """)


def _remove_action_audit_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == 'sqlite':
        op.execute('DROP TRIGGER IF EXISTS action_audit_no_update')
        op.execute('DROP TRIGGER IF EXISTS action_audit_no_delete')
    elif dialect == 'postgresql':
        op.execute(
            'DROP TRIGGER IF EXISTS action_audit_no_update_or_delete '
            'ON action_audit'
        )
        op.execute(
            'DROP FUNCTION IF EXISTS restia_reject_action_audit_mutation()'
        )


def _install_chat_message_fts() -> None:
    """Install the optional SQLite transcript index for fresh databases.

    FTS5 is part of the shipped SQLite runtime, but not every downstream
    Python build enables it.  Keep the core schema usable in those builds and
    let the existing search fallback handle the missing auxiliary index.
    """

    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        return
    try:
        bind.exec_driver_sql(
            'CREATE VIRTUAL TABLE temp._restia_fts5_probe USING fts5(content)'
        )
        bind.exec_driver_sql('DROP TABLE temp._restia_fts5_probe')
    except Exception as exc:
        logging.getLogger(__name__).warning(
            'chat_messages FTS baseline skipped; FTS5 unavailable: %s', exc
        )
        return

    op.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
            content,
            message_id UNINDEXED,
            session_id UNINDEXED,
            role UNINDEXED
        )
    """)
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS chat_messages_fts_ai
        AFTER INSERT ON chat_messages BEGIN
            INSERT INTO chat_messages_fts(content, message_id, session_id, role)
            VALUES (COALESCE(new.content, ''), new.id, new.session_id, new.role);
        END
    """)
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS chat_messages_fts_ad
        AFTER DELETE ON chat_messages BEGIN
            DELETE FROM chat_messages_fts WHERE message_id = old.id;
        END
    """)
    op.execute("""
        CREATE TRIGGER IF NOT EXISTS chat_messages_fts_au
        AFTER UPDATE ON chat_messages BEGIN
            DELETE FROM chat_messages_fts WHERE message_id = old.id;
            INSERT INTO chat_messages_fts(content, message_id, session_id, role)
            VALUES (COALESCE(new.content, ''), new.id, new.session_id, new.role);
        END
    """)


def _remove_chat_message_fts() -> None:
    if op.get_bind().dialect.name != 'sqlite':
        return
    op.execute('DROP TRIGGER IF EXISTS chat_messages_fts_au')
    op.execute('DROP TRIGGER IF EXISTS chat_messages_fts_ad')
    op.execute('DROP TRIGGER IF EXISTS chat_messages_fts_ai')
    op.execute('DROP TABLE IF EXISTS chat_messages_fts')


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('username', sa.String(length=160), nullable=False),
    sa.Column('display_name', sa.String(length=160), nullable=True),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('auth_epoch', sa.Integer(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("status IN ('active', 'renaming', 'disabled', 'deletion_pending', 'deleted')", name='ck_accounts_status'),
    sa.CheckConstraint('auth_epoch >= 1', name='ck_accounts_auth_epoch'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_accounts_status', 'accounts', ['status'], unique=False)
    op.create_index(op.f('ix_accounts_username'), 'accounts', ['username'], unique=True)
    op.create_table('auth_import_runs',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('source_kind', sa.String(length=64), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('auth_sha256', sa.String(length=64), nullable=True),
    sa.Column('sessions_sha256', sa.String(length=64), nullable=True),
    sa.Column('backup_auth_path', sa.Text(), nullable=True),
    sa.Column('backup_sessions_path', sa.Text(), nullable=True),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("state IN ('pending', 'completed', 'failed')", name='ck_auth_import_runs_state'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('source_kind')
    )
    op.create_table('auth_policy',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('signup_enabled', sa.Boolean(), nullable=False),
    sa.Column('bootstrap_completed', sa.Boolean(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('version >= 1', name='ck_auth_policy_version'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('caldav_deleted_events',
    sa.Column('uid', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('calendar_id', sa.String(), nullable=True),
    sa.Column('remote_href', sa.String(), nullable=True),
    sa.Column('remote_etag', sa.String(), nullable=True),
    sa.Column('caldav_base_url', sa.String(), nullable=True),
    sa.Column('summary', sa.String(), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('uid')
    )
    op.create_index(op.f('ix_caldav_deleted_events_calendar_id'), 'caldav_deleted_events', ['calendar_id'], unique=False)
    op.create_index(op.f('ix_caldav_deleted_events_owner'), 'caldav_deleted_events', ['owner'], unique=False)
    op.create_index(op.f('ix_caldav_deleted_events_uid'), 'caldav_deleted_events', ['uid'], unique=False)
    op.create_table('calendars',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('color', sa.String(), nullable=True),
    sa.Column('source', sa.String(), nullable=True),
    sa.Column('account_id', sa.String(), nullable=True),
    sa.Column('caldav_base_url', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_calendars_account_id'), 'calendars', ['account_id'], unique=False)
    op.create_index(op.f('ix_calendars_id'), 'calendars', ['id'], unique=False)
    op.create_index(op.f('ix_calendars_owner'), 'calendars', ['owner'], unique=False)
    op.create_table('comparisons',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('model_a', sa.String(), nullable=False),
    sa.Column('model_b', sa.String(), nullable=False),
    sa.Column('endpoint_a', sa.String(), nullable=False),
    sa.Column('endpoint_b', sa.String(), nullable=False),
    sa.Column('response_a', sa.Text(), nullable=True),
    sa.Column('response_b', sa.Text(), nullable=True),
    sa.Column('metrics_a', sa.Text(), nullable=True),
    sa.Column('metrics_b', sa.Text(), nullable=True),
    sa.Column('winner', sa.String(), nullable=True),
    sa.Column('is_blind', sa.Boolean(), nullable=True),
    sa.Column('blind_mapping', sa.Text(), nullable=True),
    sa.Column('voted_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_comparisons_id'), 'comparisons', ['id'], unique=False)
    op.create_index(op.f('ix_comparisons_owner'), 'comparisons', ['owner'], unique=False)
    op.create_index('ix_comparisons_voted_at', 'comparisons', ['voted_at'], unique=False)
    op.create_table('direct_messages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('sender', sa.String(), nullable=False),
    sa.Column('recipient', sa.String(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('read_at', sa.DateTime(), nullable=True),
    sa.Column('edited_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.Column('reply_to_id', sa.Integer(), nullable=True),
    sa.Column('reactions', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_direct_messages_created_at'), 'direct_messages', ['created_at'], unique=False)
    op.create_index(op.f('ix_direct_messages_recipient'), 'direct_messages', ['recipient'], unique=False)
    op.create_index(op.f('ix_direct_messages_sender'), 'direct_messages', ['sender'], unique=False)
    op.create_index('ix_dm_pair', 'direct_messages', ['sender', 'recipient', 'created_at'], unique=False)
    op.create_index('ix_dm_unread', 'direct_messages', ['recipient', 'read_at'], unique=False)
    op.create_table('editor_drafts',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('source_image_id', sa.String(), nullable=True),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('thumbnail', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_editor_drafts_id'), 'editor_drafts', ['id'], unique=False)
    op.create_index(op.f('ix_editor_drafts_owner'), 'editor_drafts', ['owner'], unique=False)
    op.create_index('ix_editor_drafts_owner_updated', 'editor_drafts', ['owner', 'is_active', 'updated_at'], unique=False)
    op.create_index(op.f('ix_editor_drafts_source_image_id'), 'editor_drafts', ['source_image_id'], unique=False)
    op.create_table('email_accounts',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('is_default', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('imap_host', sa.String(), nullable=True),
    sa.Column('imap_port', sa.Integer(), nullable=True),
    sa.Column('imap_user', sa.String(), nullable=True),
    sa.Column('imap_password', sa.String(), nullable=True),
    sa.Column('imap_starttls', sa.Boolean(), nullable=True),
    sa.Column('smtp_host', sa.String(), nullable=True),
    sa.Column('smtp_port', sa.Integer(), nullable=True),
    sa.Column('smtp_security', sa.String(), nullable=True),
    sa.Column('smtp_user', sa.String(), nullable=True),
    sa.Column('smtp_password', sa.String(), nullable=True),
    sa.Column('from_address', sa.String(), nullable=True),
    sa.Column('display_name', sa.String(), nullable=True),
    sa.Column('oauth_provider', sa.String(), nullable=True),
    sa.Column('oauth_access_token', sa.String(), nullable=True),
    sa.Column('oauth_refresh_token', sa.String(), nullable=True),
    sa.Column('oauth_token_expiry', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_email_accounts_id'), 'email_accounts', ['id'], unique=False)
    op.create_index(op.f('ix_email_accounts_owner'), 'email_accounts', ['owner'], unique=False)
    op.create_index('ix_email_accounts_owner_default', 'email_accounts', ['owner', 'is_default'], unique=False)
    op.create_table('gallery_albums',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('cover_id', sa.String(), nullable=True),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_gallery_albums_id'), 'gallery_albums', ['id'], unique=False)
    op.create_index(op.f('ix_gallery_albums_owner'), 'gallery_albums', ['owner'], unique=False)
    op.create_table('home_link',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('local_user', sa.String(), nullable=False),
    sa.Column('home_url', sa.String(), nullable=False),
    sa.Column('handle', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_home_link_local_user'), 'home_link', ['local_user'], unique=True)
    op.create_table('integrations',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_integrations_id'), 'integrations', ['id'], unique=False)
    op.create_index(op.f('ix_integrations_owner'), 'integrations', ['owner'], unique=False)
    op.create_table('link_guests',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('handle', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_seen', sa.DateTime(), nullable=True),
    sa.Column('invite_id', sa.Integer(), nullable=True),
    sa.Column('pubkey', sa.Text(), nullable=True),
    sa.Column('scope', sa.String(length=16), server_default='full', nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_link_guests_handle'), 'link_guests', ['handle'], unique=True)
    op.create_index(op.f('ix_link_guests_invite_id'), 'link_guests', ['invite_id'], unique=False)
    op.create_index(op.f('ix_link_guests_status'), 'link_guests', ['status'], unique=False)
    op.create_index(op.f('ix_link_guests_token_hash'), 'link_guests', ['token_hash'], unique=True)
    op.create_table('link_invites',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('code_hash', sa.String(), nullable=False),
    sa.Column('created_by', sa.String(), nullable=False),
    sa.Column('label', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('max_uses', sa.Integer(), nullable=False),
    sa.Column('uses', sa.Integer(), nullable=False),
    sa.Column('revoked', sa.Boolean(), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('project_role', sa.String(length=16), nullable=True),
    sa.Column('hub_url', sa.String(length=2048), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_link_invites_code_hash'), 'link_invites', ['code_hash'], unique=True)
    op.create_index(op.f('ix_link_invites_project_id'), 'link_invites', ['project_id'], unique=False)
    op.create_table('mcp_servers',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('transport', sa.String(), nullable=False),
    sa.Column('command', sa.String(), nullable=True),
    sa.Column('args', sa.Text(), nullable=True),
    sa.Column('env', sa.Text(), nullable=True),
    sa.Column('url', sa.String(), nullable=True),
    sa.Column('is_enabled', sa.Boolean(), nullable=True),
    sa.Column('oauth_config', sa.Text(), nullable=True),
    sa.Column('disabled_tools', sa.Text(), nullable=True),
    sa.Column('oauth_tokens', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mcp_servers_id'), 'mcp_servers', ['id'], unique=False)
    op.create_table('model_endpoints',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('base_url', sa.String(), nullable=False),
    sa.Column('api_key', sa.Text(), nullable=True),
    sa.Column('is_enabled', sa.Boolean(), nullable=True),
    sa.Column('hidden_models', sa.Text(), nullable=True),
    sa.Column('cached_models', sa.Text(), nullable=True),
    sa.Column('pinned_models', sa.Text(), nullable=True),
    sa.Column('model_type', sa.String(), nullable=True),
    sa.Column('endpoint_kind', sa.String(), nullable=True),
    sa.Column('model_refresh_mode', sa.String(), nullable=True),
    sa.Column('model_refresh_interval', sa.Integer(), nullable=True),
    sa.Column('model_refresh_timeout', sa.Integer(), nullable=True),
    sa.Column('supports_tools', sa.Boolean(), nullable=True),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('provider_auth_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_model_endpoints_id'), 'model_endpoints', ['id'], unique=False)
    op.create_index(op.f('ix_model_endpoints_owner'), 'model_endpoints', ['owner'], unique=False)
    op.create_index(op.f('ix_model_endpoints_provider_auth_id'), 'model_endpoints', ['provider_auth_id'], unique=False)
    op.create_table('notes',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('title', sa.String(), nullable=True),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('items', sa.Text(), nullable=True),
    sa.Column('note_type', sa.String(), nullable=True),
    sa.Column('color', sa.String(), nullable=True),
    sa.Column('label', sa.String(), nullable=True),
    sa.Column('pinned', sa.Boolean(), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=True),
    sa.Column('due_date', sa.String(), nullable=True),
    sa.Column('source', sa.String(), nullable=True),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=True),
    sa.Column('image_url', sa.String(), nullable=True),
    sa.Column('repeat', sa.String(), nullable=True),
    sa.Column('ai_classification', sa.Text(), nullable=True),
    sa.Column('ai_content_hash', sa.String(), nullable=True),
    sa.Column('agent_session_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_notes_id'), 'notes', ['id'], unique=False)
    op.create_index(op.f('ix_notes_owner'), 'notes', ['owner'], unique=False)
    op.create_table('outbound_chat_links',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('home_url', sa.String(length=2048), nullable=False),
    sa.Column('handle', sa.String(length=32), nullable=False),
    sa.Column('owner', sa.String(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_outbound_chat_links_home_url'), 'outbound_chat_links', ['home_url'], unique=True)
    op.create_index(op.f('ix_outbound_chat_links_owner'), 'outbound_chat_links', ['owner'], unique=False)
    op.create_table('progression_events',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner', sa.String(), nullable=False),
    sa.Column('event_key', sa.String(length=180), nullable=False),
    sa.Column('source_type', sa.String(length=48), nullable=False),
    sa.Column('source_id', sa.String(length=180), nullable=False),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('xp', sa.Integer(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner', 'event_key', name='uq_progression_owner_event')
    )
    op.create_index(op.f('ix_progression_events_occurred_at'), 'progression_events', ['occurred_at'], unique=False)
    op.create_index(op.f('ix_progression_events_owner'), 'progression_events', ['owner'], unique=False)
    op.create_index(op.f('ix_progression_events_source_type'), 'progression_events', ['source_type'], unique=False)
    op.create_index('ix_progression_owner_occurred', 'progression_events', ['owner', 'occurred_at'], unique=False)
    op.create_index('ix_progression_owner_source', 'progression_events', ['owner', 'source_type'], unique=False)
    op.create_table('projects',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner', sa.String(), nullable=False),
    sa.Column('key', sa.String(length=12), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('template', sa.String(length=32), nullable=False),
    sa.Column('color', sa.String(length=16), nullable=False),
    sa.Column('icon', sa.String(length=32), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('next_item_number', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner', 'key', name='uq_projects_owner_key')
    )
    op.create_index(op.f('ix_projects_archived'), 'projects', ['archived'], unique=False)
    op.create_index(op.f('ix_projects_completed_at'), 'projects', ['completed_at'], unique=False)
    op.create_index(op.f('ix_projects_owner'), 'projects', ['owner'], unique=False)
    op.create_index('ix_projects_owner_archived_updated', 'projects', ['owner', 'archived', 'updated_at'], unique=False)
    op.create_table('provider_auth_sessions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('provider', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('label', sa.String(), nullable=True),
    sa.Column('base_url', sa.String(), nullable=False),
    sa.Column('access_token', sa.Text(), nullable=True),
    sa.Column('refresh_token', sa.Text(), nullable=True),
    sa.Column('last_refresh', sa.DateTime(), nullable=True),
    sa.Column('auth_mode', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_provider_auth_sessions_id'), 'provider_auth_sessions', ['id'], unique=False)
    op.create_index(op.f('ix_provider_auth_sessions_owner'), 'provider_auth_sessions', ['owner'], unique=False)
    op.create_index(op.f('ix_provider_auth_sessions_provider'), 'provider_auth_sessions', ['provider'], unique=False)
    op.create_table('remote_blocks',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('local_user', sa.String(), nullable=False),
    sa.Column('handle', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_remote_block_pair', 'remote_blocks', ['local_user', 'handle'], unique=True)
    op.create_index(op.f('ix_remote_blocks_handle'), 'remote_blocks', ['handle'], unique=False)
    op.create_index(op.f('ix_remote_blocks_local_user'), 'remote_blocks', ['local_user'], unique=False)
    op.create_table('remote_contact_prefs',
    sa.Column('local_user', sa.String(), nullable=False),
    sa.Column('discoverable', sa.Boolean(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('local_user')
    )
    op.create_table('sessions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('endpoint_url', sa.String(), nullable=False),
    sa.Column('model', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('rag', sa.Boolean(), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=True),
    sa.Column('folder', sa.String(), nullable=True),
    sa.Column('headers', sa.JSON(), nullable=True),
    sa.Column('last_accessed', sa.DateTime(), nullable=True),
    sa.Column('last_message_at', sa.DateTime(), nullable=True),
    sa.Column('is_important', sa.Boolean(), nullable=True),
    sa.Column('message_count', sa.Integer(), nullable=True),
    sa.Column('total_input_tokens', sa.Integer(), nullable=True),
    sa.Column('total_output_tokens', sa.Integer(), nullable=True),
    sa.Column('mode', sa.String(), nullable=True),
    sa.Column('crew_member_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_sessions_active', 'sessions', ['archived', 'last_accessed'], unique=False)
    op.create_index(op.f('ix_sessions_id'), 'sessions', ['id'], unique=False)
    op.create_index(op.f('ix_sessions_owner'), 'sessions', ['owner'], unique=False)
    op.create_index('ix_sessions_search', 'sessions', ['name', 'archived'], unique=False)
    op.create_table('signatures',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('data_png', sa.Text(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('svg', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_signatures_id'), 'signatures', ['id'], unique=False)
    op.create_index(op.f('ix_signatures_owner'), 'signatures', ['owner'], unique=False)
    op.create_table('status_posts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('author', sa.String(), nullable=False),
    sa.Column('image', sa.Text(), nullable=False),
    sa.Column('caption', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_status_posts_author'), 'status_posts', ['author'], unique=False)
    op.create_index(op.f('ix_status_posts_created_at'), 'status_posts', ['created_at'], unique=False)
    op.create_index(op.f('ix_status_posts_expires_at'), 'status_posts', ['expires_at'], unique=False)
    op.create_table('status_views',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('status_id', sa.Integer(), nullable=False),
    sa.Column('viewer', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_status_view_pair', 'status_views', ['status_id', 'viewer'], unique=True)
    op.create_index(op.f('ix_status_views_status_id'), 'status_views', ['status_id'], unique=False)
    op.create_index(op.f('ix_status_views_viewer'), 'status_views', ['viewer'], unique=False)
    op.create_table('study_states',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('goal_text', sa.Text(), nullable=False),
    sa.Column('target_minutes', sa.Integer(), nullable=False),
    sa.Column('target_date', sa.String(), nullable=True),
    sa.Column('total_seconds', sa.Integer(), nullable=False),
    sa.Column('current_session_seconds', sa.Integer(), nullable=False),
    sa.Column('timer_started_at', sa.DateTime(), nullable=True),
    sa.Column('timer_running', sa.Boolean(), nullable=False),
    sa.Column('last_prompt_at', sa.DateTime(), nullable=True),
    sa.Column('setup_initialized', sa.Boolean(), nullable=False),
    sa.Column('review_level', sa.Integer(), nullable=False),
    sa.Column('review_count', sa.Integer(), nullable=False),
    sa.Column('last_review_result', sa.String(), nullable=True),
    sa.Column('last_reviewed_at', sa.DateTime(), nullable=True),
    sa.Column('next_review_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_study_states_owner'), 'study_states', ['owner'], unique=False)
    op.create_table('user_keys',
    sa.Column('username', sa.String(), nullable=False),
    sa.Column('public_jwk', sa.Text(), nullable=False),
    sa.Column('wrapped_private', sa.Text(), nullable=False),
    sa.Column('kdf_salt', sa.String(), nullable=False),
    sa.Column('kdf_iterations', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('username')
    )
    op.create_table('user_profiles',
    sa.Column('username', sa.String(), nullable=False),
    sa.Column('display_name', sa.String(), nullable=True),
    sa.Column('avatar_color', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('username')
    )
    op.create_table('webhooks',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('url', sa.String(), nullable=False),
    sa.Column('secret', sa.String(), nullable=True),
    sa.Column('events', sa.String(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('last_triggered_at', sa.DateTime(), nullable=True),
    sa.Column('last_status_code', sa.Integer(), nullable=True),
    sa.Column('last_error', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_webhooks_id'), 'webhooks', ['id'], unique=False)
    op.create_table('account_capabilities',
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('account_id')
    )
    op.create_table('account_roles',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('role', sa.String(length=64), nullable=False),
    sa.Column('granted_by_account_id', sa.String(length=36), nullable=True),
    sa.Column('granted_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('length(role) > 0', name='ck_account_roles_role'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['granted_by_account_id'], ['accounts.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'role', name='uq_account_role')
    )
    op.create_index(op.f('ix_account_roles_account_id'), 'account_roles', ['account_id'], unique=False)
    op.create_index('ix_account_roles_role', 'account_roles', ['role'], unique=False)
    op.create_table('action_audit',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('action', sa.String(length=80), nullable=False),
    sa.Column('entity_type', sa.String(length=48), nullable=False),
    sa.Column('entity_id', sa.String(length=255), nullable=False),
    sa.Column('before_state', sa.JSON(), nullable=False),
    sa.Column('after_state', sa.JSON(), nullable=False),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['accounts.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_action_audit_action'), 'action_audit', ['action'], unique=False)
    op.create_index(op.f('ix_action_audit_created_at'), 'action_audit', ['created_at'], unique=False)
    op.create_index('ix_action_audit_entity', 'action_audit', ['owner_id', 'entity_type', 'entity_id'], unique=False)
    op.create_index(op.f('ix_action_audit_entity_id'), 'action_audit', ['entity_id'], unique=False)
    op.create_index('ix_action_audit_owner_created', 'action_audit', ['owner_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_action_audit_owner_id'), 'action_audit', ['owner_id'], unique=False)
    op.create_table('api_tokens',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('account_id', sa.String(length=36), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('token_prefix', sa.String(), nullable=False),
    sa.Column('digest_scheme', sa.String(length=32), nullable=False),
    sa.Column('scopes', sa.String(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_tokens_account_id'), 'api_tokens', ['account_id'], unique=False)
    op.create_index(op.f('ix_api_tokens_id'), 'api_tokens', ['id'], unique=False)
    op.create_index(op.f('ix_api_tokens_owner'), 'api_tokens', ['owner'], unique=False)
    op.create_table('auth_identities',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('issuer', sa.String(length=500), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('linked_at', sa.DateTime(), nullable=False),
    sa.Column('last_verified_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("state IN ('active', 'disabled', 'unlinked')", name='ck_auth_identities_state'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'issuer', 'subject', name='uq_auth_identity_provider_issuer_subject')
    )
    op.create_index(op.f('ix_auth_identities_account_id'), 'auth_identities', ['account_id'], unique=False)
    op.create_index('ix_auth_identity_account_provider', 'auth_identities', ['account_id', 'provider'], unique=False)
    op.create_index('ix_auth_identity_issuer_subject', 'auth_identities', ['issuer', 'subject'], unique=False)
    op.create_table('calendar_events',
    sa.Column('uid', sa.String(), nullable=False),
    sa.Column('calendar_id', sa.String(), nullable=False),
    sa.Column('summary', sa.String(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('location', sa.String(), nullable=True),
    sa.Column('dtstart', sa.DateTime(), nullable=False),
    sa.Column('dtend', sa.DateTime(), nullable=False),
    sa.Column('all_day', sa.Boolean(), nullable=True),
    sa.Column('is_utc', sa.Boolean(), nullable=False),
    sa.Column('rrule', sa.String(), nullable=True),
    sa.Column('recurrence_exdates', sa.Text(), nullable=True),
    sa.Column('color', sa.String(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('importance', sa.String(), nullable=True),
    sa.Column('event_type', sa.String(), nullable=True),
    sa.Column('last_pinged', sa.DateTime(), nullable=True),
    sa.Column('origin', sa.String(), nullable=True),
    sa.Column('remote_href', sa.String(), nullable=True),
    sa.Column('remote_etag', sa.String(), nullable=True),
    sa.Column('caldav_sync_pending', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['calendar_id'], ['calendars.id'], ),
    sa.PrimaryKeyConstraint('uid')
    )
    op.create_index(op.f('ix_calendar_events_calendar_id'), 'calendar_events', ['calendar_id'], unique=False)
    op.create_index(op.f('ix_calendar_events_dtstart'), 'calendar_events', ['dtstart'], unique=False)
    op.create_index(op.f('ix_calendar_events_origin'), 'calendar_events', ['origin'], unique=False)
    op.create_index(op.f('ix_calendar_events_uid'), 'calendar_events', ['uid'], unique=False)
    op.create_table('chat_messages',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('session_id', sa.String(), nullable=False),
    sa.Column('role', sa.String(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('metadata', sa.Text(), nullable=True),
    sa.Column('timestamp', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chat_messages_id'), 'chat_messages', ['id'], unique=False)
    op.create_index(op.f('ix_chat_messages_session_id'), 'chat_messages', ['session_id'], unique=False)
    op.create_index('ix_messages_session_time', 'chat_messages', ['session_id', 'timestamp'], unique=False)
    op.create_table('crew_members',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('avatar', sa.String(), nullable=True),
    sa.Column('user_name', sa.String(), nullable=True),
    sa.Column('personality', sa.Text(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.Column('endpoint_url', sa.String(), nullable=True),
    sa.Column('greeting', sa.Text(), nullable=True),
    sa.Column('enabled_tools', sa.Text(), nullable=True),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('sort_order', sa.Integer(), nullable=True),
    sa.Column('is_default_assistant', sa.Boolean(), nullable=True),
    sa.Column('timezone', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_crew_members_id'), 'crew_members', ['id'], unique=False)
    op.create_index(op.f('ix_crew_members_owner'), 'crew_members', ['owner'], unique=False)
    op.create_table('direct_message_attachments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('message_id', sa.Integer(), nullable=False),
    sa.Column('filename', sa.Text(), nullable=False),
    sa.Column('mime', sa.String(length=32), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('data_b64', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['message_id'], ['direct_messages.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_direct_message_attachments_message_id'), 'direct_message_attachments', ['message_id'], unique=False)
    op.create_index('ix_dm_attachment_message_created', 'direct_message_attachments', ['message_id', 'created_at'], unique=False)
    op.create_table('documents',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('title', sa.String(), nullable=False),
    sa.Column('language', sa.String(), nullable=True),
    sa.Column('current_content', sa.Text(), nullable=False),
    sa.Column('version_count', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=True),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('tidy_verdict', sa.String(), nullable=True),
    sa.Column('source_email_uid', sa.String(), nullable=True),
    sa.Column('source_email_folder', sa.String(), nullable=True),
    sa.Column('source_email_account_id', sa.String(), nullable=True),
    sa.Column('source_email_message_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_documents_id'), 'documents', ['id'], unique=False)
    op.create_index(op.f('ix_documents_owner'), 'documents', ['owner'], unique=False)
    op.create_index(op.f('ix_documents_session_id'), 'documents', ['session_id'], unique=False)
    op.create_index(op.f('ix_documents_source_email_message_id'), 'documents', ['source_email_message_id'], unique=False)
    op.create_table('entity_links',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('source_type', sa.String(length=48), nullable=False),
    sa.Column('source_id', sa.String(length=255), nullable=False),
    sa.Column('relation', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=48), nullable=False),
    sa.Column('target_id', sa.String(length=255), nullable=False),
    sa.Column('metadata', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'source_type', 'source_id', 'relation', 'target_type', 'target_id', name='uq_entity_link_edge')
    )
    op.create_index(op.f('ix_entity_links_owner_id'), 'entity_links', ['owner_id'], unique=False)
    op.create_index('ix_entity_links_source', 'entity_links', ['owner_id', 'source_type', 'source_id'], unique=False)
    op.create_index('ix_entity_links_target', 'entity_links', ['owner_id', 'target_type', 'target_id'], unique=False)
    op.create_table('gallery_images',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('filename', sa.String(), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('caption', sa.Text(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.Column('size', sa.String(), nullable=True),
    sa.Column('quality', sa.String(), nullable=True),
    sa.Column('tags', sa.String(), nullable=True),
    sa.Column('ai_tags', sa.Text(), nullable=True),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('album_id', sa.String(), nullable=True),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('favorite', sa.Boolean(), nullable=True),
    sa.Column('file_hash', sa.String(length=64), nullable=True),
    sa.Column('taken_at', sa.DateTime(), nullable=True),
    sa.Column('camera_make', sa.String(), nullable=True),
    sa.Column('camera_model', sa.String(), nullable=True),
    sa.Column('gps_lat', sa.String(), nullable=True),
    sa.Column('gps_lng', sa.String(), nullable=True),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('file_size', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['album_id'], ['gallery_albums.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('filename')
    )
    op.create_index('ix_gallery_images_active', 'gallery_images', ['is_active', 'created_at'], unique=False)
    op.create_index(op.f('ix_gallery_images_album_id'), 'gallery_images', ['album_id'], unique=False)
    op.create_index(op.f('ix_gallery_images_file_hash'), 'gallery_images', ['file_hash'], unique=False)
    op.create_index(op.f('ix_gallery_images_id'), 'gallery_images', ['id'], unique=False)
    op.create_index('ix_gallery_images_model', 'gallery_images', ['model'], unique=False)
    op.create_index(op.f('ix_gallery_images_owner'), 'gallery_images', ['owner'], unique=False)
    op.create_index(op.f('ix_gallery_images_session_id'), 'gallery_images', ['session_id'], unique=False)
    op.create_index('ix_gallery_images_tags', 'gallery_images', ['tags'], unique=False)
    op.create_index(op.f('ix_gallery_images_taken_at'), 'gallery_images', ['taken_at'], unique=False)
    op.create_table('inbox_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner_id', sa.String(length=36), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('status', sa.String(length=24), nullable=False),
    sa.Column('source_type', sa.String(length=48), nullable=False),
    sa.Column('source_ref', sa.Text(), nullable=True),
    sa.Column('metadata', sa.JSON(), nullable=False),
    sa.Column('classification_confidence', sa.Integer(), nullable=False),
    sa.Column('classification_reason', sa.String(length=500), nullable=False),
    sa.Column('processed_target_type', sa.String(length=48), nullable=True),
    sa.Column('processed_target_id', sa.String(length=255), nullable=True),
    sa.Column('processed_at', sa.DateTime(), nullable=True),
    sa.Column('archived_at', sa.DateTime(), nullable=True),
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('owner_id', 'idempotency_key', name='uq_inbox_owner_idempotency')
    )
    op.create_index(op.f('ix_inbox_items_kind'), 'inbox_items', ['kind'], unique=False)
    op.create_index(op.f('ix_inbox_items_owner_id'), 'inbox_items', ['owner_id'], unique=False)
    op.create_index(op.f('ix_inbox_items_status'), 'inbox_items', ['status'], unique=False)
    op.create_index('ix_inbox_owner_kind_status', 'inbox_items', ['owner_id', 'kind', 'status'], unique=False)
    op.create_index('ix_inbox_owner_status_updated', 'inbox_items', ['owner_id', 'status', 'updated_at'], unique=False)
    op.create_table('local_credentials',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('algorithm', sa.String(length=32), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('password_changed_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('version >= 1', name='ck_local_credentials_version'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_local_credentials_account_id'), 'local_credentials', ['account_id'], unique=True)
    op.create_table('memories',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('category', sa.String(), nullable=True),
    sa.Column('source', sa.String(), nullable=True),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('timestamp', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_memories_id'), 'memories', ['id'], unique=False)
    op.create_index('ix_memories_lookup', 'memories', ['category', 'timestamp'], unique=False)
    op.create_index(op.f('ix_memories_owner'), 'memories', ['owner'], unique=False)
    op.create_index('ix_memories_session', 'memories', ['session_id', 'timestamp'], unique=False)
    op.create_index(op.f('ix_memories_session_id'), 'memories', ['session_id'], unique=False)
    op.create_table('mfa_factors',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('state', sa.String(length=24), nullable=False),
    sa.Column('secret', sa.Text(), nullable=True),
    sa.Column('pending_secret', sa.Text(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('last_used_step', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("state IN ('pending', 'active', 'disabled')", name='ck_mfa_factors_state'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('account_id', 'kind', name='uq_mfa_factor_account_kind')
    )
    op.create_index(op.f('ix_mfa_factors_account_id'), 'mfa_factors', ['account_id'], unique=False)
    op.create_table('project_members',
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('username', sa.String(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('added_by', sa.String(), nullable=True),
    sa.Column('joined_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('project_id', 'username')
    )
    op.create_index('ix_project_members_username', 'project_members', ['username', 'project_id'], unique=False)
    op.create_table('project_quota_locks',
    sa.Column('key', sa.String(length=80), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_index(op.f('ix_project_quota_locks_project_id'), 'project_quota_locks', ['project_id'], unique=False)
    op.create_table('project_remote_grants',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('guest_id', sa.Integer(), nullable=True),
    sa.Column('handle_snapshot', sa.String(length=32), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('invited_by', sa.String(), nullable=False),
    sa.Column('invited_at', sa.DateTime(), nullable=False),
    sa.Column('responded_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['guest_id'], ['link_guests.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'guest_id', name='uq_project_remote_grants_project_guest')
    )
    op.create_index('ix_project_remote_grants_guest_status', 'project_remote_grants', ['guest_id', 'status', 'project_id'], unique=False)
    op.create_index('ix_project_remote_grants_project_status', 'project_remote_grants', ['project_id', 'status', 'invited_at'], unique=False)
    op.create_table('project_stages',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('category', sa.String(length=24), nullable=False),
    sa.Column('color', sa.String(length=16), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('wip_limit', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_project_stages_order', 'project_stages', ['project_id', 'position'], unique=False)
    op.create_index(op.f('ix_project_stages_project_id'), 'project_stages', ['project_id'], unique=False)
    op.create_table('retired_auth_subjects',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('issuer', sa.String(length=500), nullable=False),
    sa.Column('subject', sa.String(length=255), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=True),
    sa.Column('reason', sa.String(length=64), nullable=False),
    sa.Column('retired_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'issuer', 'subject', name='uq_retired_auth_subject')
    )
    op.create_index('ix_retired_auth_subject_account', 'retired_auth_subjects', ['account_id'], unique=False)
    op.create_table('scheduled_tasks',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=True),
    sa.Column('task_type', sa.String(), nullable=True),
    sa.Column('action', sa.String(), nullable=True),
    sa.Column('schedule', sa.String(), nullable=True),
    sa.Column('scheduled_time', sa.String(), nullable=True),
    sa.Column('scheduled_day', sa.Integer(), nullable=True),
    sa.Column('scheduled_date', sa.DateTime(), nullable=True),
    sa.Column('trigger_type', sa.String(), nullable=True),
    sa.Column('trigger_event', sa.String(), nullable=True),
    sa.Column('trigger_count', sa.Integer(), nullable=True),
    sa.Column('trigger_counter', sa.Integer(), nullable=True),
    sa.Column('next_run', sa.DateTime(), nullable=True),
    sa.Column('last_run', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('output_target', sa.String(), nullable=True),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.Column('endpoint_url', sa.String(), nullable=True),
    sa.Column('run_count', sa.Integer(), nullable=True),
    sa.Column('cron_expression', sa.String(), nullable=True),
    sa.Column('then_task_id', sa.String(), nullable=True),
    sa.Column('webhook_token', sa.String(), nullable=True),
    sa.Column('crew_member_id', sa.String(), nullable=True),
    sa.Column('character_id', sa.String(), nullable=True),
    sa.Column('max_steps', sa.Integer(), nullable=True),
    sa.Column('email_results', sa.Boolean(), nullable=True),
    sa.Column('notifications_enabled', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['then_task_id'], ['scheduled_tasks.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('webhook_token')
    )
    op.create_index('ix_scheduled_tasks_due', 'scheduled_tasks', ['status', 'next_run'], unique=False)
    op.create_index('ix_scheduled_tasks_event', 'scheduled_tasks', ['trigger_type', 'trigger_event', 'status'], unique=False)
    op.create_index(op.f('ix_scheduled_tasks_id'), 'scheduled_tasks', ['id'], unique=False)
    op.create_index(op.f('ix_scheduled_tasks_next_run'), 'scheduled_tasks', ['next_run'], unique=False)
    op.create_index(op.f('ix_scheduled_tasks_owner'), 'scheduled_tasks', ['owner'], unique=False)
    op.create_table('user_tools',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('icon', sa.String(), nullable=True),
    sa.Column('html_content', sa.Text(), nullable=False),
    sa.Column('scope', sa.String(), nullable=False),
    sa.Column('session_id', sa.String(), nullable=True),
    sa.Column('owner', sa.String(), nullable=True),
    sa.Column('is_pinned', sa.Boolean(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('author', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_user_tools_active', 'user_tools', ['is_active'], unique=False)
    op.create_index(op.f('ix_user_tools_id'), 'user_tools', ['id'], unique=False)
    op.create_index(op.f('ix_user_tools_owner'), 'user_tools', ['owner'], unique=False)
    op.create_index('ix_user_tools_scope', 'user_tools', ['scope'], unique=False)
    op.create_table('auth_sessions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=False),
    sa.Column('token_digest', sa.String(length=128), nullable=False),
    sa.Column('digest_scheme', sa.String(length=32), nullable=False),
    sa.Column('auth_epoch', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(), nullable=True),
    sa.Column('interface', sa.String(length=32), nullable=False),
    sa.Column('auth_method', sa.String(length=32), nullable=False),
    sa.Column('source_identity_id', sa.String(length=36), nullable=True),
    sa.Column('external_session_id', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('auth_epoch >= 1', name='ck_auth_sessions_auth_epoch'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_identity_id'], ['auth_identities.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_auth_sessions_account_id'), 'auth_sessions', ['account_id'], unique=False)
    op.create_index('ix_auth_sessions_account_revoked_expires', 'auth_sessions', ['account_id', 'revoked_at', 'expires_at'], unique=False)
    op.create_index(op.f('ix_auth_sessions_expires_at'), 'auth_sessions', ['expires_at'], unique=False)
    op.create_index(op.f('ix_auth_sessions_revoked_at'), 'auth_sessions', ['revoked_at'], unique=False)
    op.create_index(op.f('ix_auth_sessions_token_digest'), 'auth_sessions', ['token_digest'], unique=True)
    op.create_table('document_versions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('document_id', sa.String(), nullable=False),
    sa.Column('version_number', sa.Integer(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('summary', sa.String(), nullable=True),
    sa.Column('source', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_document_versions_document_id'), 'document_versions', ['document_id'], unique=False)
    op.create_index(op.f('ix_document_versions_id'), 'document_versions', ['id'], unique=False)
    op.create_table('mfa_recovery_codes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('factor_id', sa.String(length=36), nullable=False),
    sa.Column('code_hash', sa.String(length=255), nullable=False),
    sa.Column('digest_scheme', sa.String(length=32), nullable=False),
    sa.Column('used_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['factor_id'], ['mfa_factors.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('factor_id', 'code_hash', name='uq_mfa_recovery_code')
    )
    op.create_index(op.f('ix_mfa_recovery_codes_factor_id'), 'mfa_recovery_codes', ['factor_id'], unique=False)
    op.create_index('ix_mfa_recovery_factor_used', 'mfa_recovery_codes', ['factor_id', 'used_at'], unique=False)
    op.create_table('planning_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('owner', sa.String(), nullable=False),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('priority', sa.String(length=16), nullable=False),
    sa.Column('due_date', sa.String(length=10), nullable=True),
    sa.Column('scheduled_start', sa.DateTime(), nullable=True),
    sa.Column('scheduled_end', sa.DateTime(), nullable=True),
    sa.Column('calendar_id', sa.String(), nullable=True),
    sa.Column('calendar_event_uid', sa.String(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('source', sa.String(length=24), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['calendar_event_uid'], ['calendar_events.uid'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['calendar_id'], ['calendars.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('calendar_event_uid')
    )
    op.create_index(op.f('ix_planning_items_completed_at'), 'planning_items', ['completed_at'], unique=False)
    op.create_index(op.f('ix_planning_items_due_date'), 'planning_items', ['due_date'], unique=False)
    op.create_index(op.f('ix_planning_items_owner'), 'planning_items', ['owner'], unique=False)
    op.create_index(op.f('ix_planning_items_scheduled_start'), 'planning_items', ['scheduled_start'], unique=False)
    op.create_index(op.f('ix_planning_items_status'), 'planning_items', ['status'], unique=False)
    op.create_index('ix_planning_owner_status_due', 'planning_items', ['owner', 'status', 'due_date'], unique=False)
    op.create_index('ix_planning_owner_updated', 'planning_items', ['owner', 'updated_at'], unique=False)
    op.create_table('project_work_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('stage_id', sa.String(length=36), nullable=True),
    sa.Column('item_number', sa.Integer(), nullable=False),
    sa.Column('item_type', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=240), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('priority', sa.String(length=16), nullable=False),
    sa.Column('labels', sa.JSON(), nullable=False),
    sa.Column('reporter', sa.String(), nullable=False),
    sa.Column('assignee', sa.String(), nullable=True),
    sa.Column('start_date', sa.String(length=10), nullable=True),
    sa.Column('due_date', sa.String(length=10), nullable=True),
    sa.Column('estimate_minutes', sa.Integer(), nullable=False),
    sa.Column('logged_minutes', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.String(length=36), nullable=True),
    sa.Column('blocked_by_id', sa.String(length=36), nullable=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['blocked_by_id'], ['project_work_items.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['parent_id'], ['project_work_items.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['stage_id'], ['project_stages.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('project_id', 'item_number', name='uq_project_work_item_number')
    )
    op.create_index(op.f('ix_project_work_items_archived'), 'project_work_items', ['archived'], unique=False)
    op.create_index(op.f('ix_project_work_items_assignee'), 'project_work_items', ['assignee'], unique=False)
    op.create_index(op.f('ix_project_work_items_blocked_by_id'), 'project_work_items', ['blocked_by_id'], unique=False)
    op.create_index('ix_project_work_items_board', 'project_work_items', ['project_id', 'archived', 'stage_id', 'position'], unique=False)
    op.create_index(op.f('ix_project_work_items_due_date'), 'project_work_items', ['due_date'], unique=False)
    op.create_index(op.f('ix_project_work_items_parent_id'), 'project_work_items', ['parent_id'], unique=False)
    op.create_index(op.f('ix_project_work_items_project_id'), 'project_work_items', ['project_id'], unique=False)
    op.create_index(op.f('ix_project_work_items_stage_id'), 'project_work_items', ['stage_id'], unique=False)
    op.create_table('task_runs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('task_id', sa.String(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('status', sa.String(), nullable=True),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('tokens_used', sa.Integer(), nullable=True),
    sa.Column('steps', sa.Text(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['scheduled_tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_task_runs_id'), 'task_runs', ['id'], unique=False)
    op.create_index('ix_task_runs_task', 'task_runs', ['task_id', 'started_at'], unique=False)
    op.create_table('user_tool_data',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tool_id', sa.String(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['tool_id'], ['user_tools.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_user_tool_data_tool_key', 'user_tool_data', ['tool_id', 'key'], unique=True)
    op.create_table('project_activity',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('project_id', sa.String(length=36), nullable=False),
    sa.Column('work_item_id', sa.String(length=36), nullable=True),
    sa.Column('actor', sa.String(), nullable=False),
    sa.Column('event_type', sa.String(length=40), nullable=False),
    sa.Column('summary', sa.String(length=500), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['work_item_id'], ['project_work_items.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_project_activity_created_at'), 'project_activity', ['created_at'], unique=False)
    op.create_index(op.f('ix_project_activity_event_type'), 'project_activity', ['event_type'], unique=False)
    op.create_index('ix_project_activity_item_created', 'project_activity', ['work_item_id', 'created_at'], unique=False)
    op.create_index('ix_project_activity_project_created', 'project_activity', ['project_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_project_activity_project_id'), 'project_activity', ['project_id'], unique=False)
    op.create_index(op.f('ix_project_activity_work_item_id'), 'project_activity', ['work_item_id'], unique=False)
    op.create_table('project_attachments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('work_item_id', sa.String(length=36), nullable=False),
    sa.Column('uploader', sa.String(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=False),
    sa.Column('original_name', sa.String(length=240), nullable=False),
    sa.Column('storage_key', sa.String(length=160), nullable=False),
    sa.Column('mime', sa.String(length=160), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('supersedes_id', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['supersedes_id'], ['project_attachments.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['work_item_id'], ['project_work_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('storage_key')
    )
    op.create_index('ix_project_attachments_item_created', 'project_attachments', ['work_item_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_project_attachments_sha256'), 'project_attachments', ['sha256'], unique=False)
    op.create_index(op.f('ix_project_attachments_status'), 'project_attachments', ['status'], unique=False)
    op.create_index(op.f('ix_project_attachments_work_item_id'), 'project_attachments', ['work_item_id'], unique=False)
    op.create_table('project_checklist_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('work_item_id', sa.String(length=36), nullable=False),
    sa.Column('text', sa.String(length=500), nullable=False),
    sa.Column('is_done', sa.Boolean(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('created_by', sa.String(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['work_item_id'], ['project_work_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_project_checklist_items_work_item_id'), 'project_checklist_items', ['work_item_id'], unique=False)
    op.create_index('ix_project_checklist_order', 'project_checklist_items', ['work_item_id', 'position'], unique=False)
    op.create_table('project_comments',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('work_item_id', sa.String(length=36), nullable=False),
    sa.Column('author', sa.String(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('edited_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['work_item_id'], ['project_work_items.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_project_comments_item_created', 'project_comments', ['work_item_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_project_comments_work_item_id'), 'project_comments', ['work_item_id'], unique=False)
    _install_action_audit_guards()
    _install_chat_message_fts()


def downgrade() -> None:
    _remove_chat_message_fts()
    _remove_action_audit_guards()
    op.drop_index(op.f('ix_project_comments_work_item_id'), table_name='project_comments')
    op.drop_index('ix_project_comments_item_created', table_name='project_comments')
    op.drop_table('project_comments')
    op.drop_index('ix_project_checklist_order', table_name='project_checklist_items')
    op.drop_index(op.f('ix_project_checklist_items_work_item_id'), table_name='project_checklist_items')
    op.drop_table('project_checklist_items')
    op.drop_index(op.f('ix_project_attachments_work_item_id'), table_name='project_attachments')
    op.drop_index(op.f('ix_project_attachments_status'), table_name='project_attachments')
    op.drop_index(op.f('ix_project_attachments_sha256'), table_name='project_attachments')
    op.drop_index('ix_project_attachments_item_created', table_name='project_attachments')
    op.drop_table('project_attachments')
    op.drop_index(op.f('ix_project_activity_work_item_id'), table_name='project_activity')
    op.drop_index(op.f('ix_project_activity_project_id'), table_name='project_activity')
    op.drop_index('ix_project_activity_project_created', table_name='project_activity')
    op.drop_index('ix_project_activity_item_created', table_name='project_activity')
    op.drop_index(op.f('ix_project_activity_event_type'), table_name='project_activity')
    op.drop_index(op.f('ix_project_activity_created_at'), table_name='project_activity')
    op.drop_table('project_activity')
    op.drop_index('ix_user_tool_data_tool_key', table_name='user_tool_data')
    op.drop_table('user_tool_data')
    op.drop_index('ix_task_runs_task', table_name='task_runs')
    op.drop_index(op.f('ix_task_runs_id'), table_name='task_runs')
    op.drop_table('task_runs')
    op.drop_index(op.f('ix_project_work_items_stage_id'), table_name='project_work_items')
    op.drop_index(op.f('ix_project_work_items_project_id'), table_name='project_work_items')
    op.drop_index(op.f('ix_project_work_items_parent_id'), table_name='project_work_items')
    op.drop_index(op.f('ix_project_work_items_due_date'), table_name='project_work_items')
    op.drop_index('ix_project_work_items_board', table_name='project_work_items')
    op.drop_index(op.f('ix_project_work_items_blocked_by_id'), table_name='project_work_items')
    op.drop_index(op.f('ix_project_work_items_assignee'), table_name='project_work_items')
    op.drop_index(op.f('ix_project_work_items_archived'), table_name='project_work_items')
    op.drop_table('project_work_items')
    op.drop_index('ix_planning_owner_updated', table_name='planning_items')
    op.drop_index('ix_planning_owner_status_due', table_name='planning_items')
    op.drop_index(op.f('ix_planning_items_status'), table_name='planning_items')
    op.drop_index(op.f('ix_planning_items_scheduled_start'), table_name='planning_items')
    op.drop_index(op.f('ix_planning_items_owner'), table_name='planning_items')
    op.drop_index(op.f('ix_planning_items_due_date'), table_name='planning_items')
    op.drop_index(op.f('ix_planning_items_completed_at'), table_name='planning_items')
    op.drop_table('planning_items')
    op.drop_index('ix_mfa_recovery_factor_used', table_name='mfa_recovery_codes')
    op.drop_index(op.f('ix_mfa_recovery_codes_factor_id'), table_name='mfa_recovery_codes')
    op.drop_table('mfa_recovery_codes')
    op.drop_index(op.f('ix_document_versions_id'), table_name='document_versions')
    op.drop_index(op.f('ix_document_versions_document_id'), table_name='document_versions')
    op.drop_table('document_versions')
    op.drop_index(op.f('ix_auth_sessions_token_digest'), table_name='auth_sessions')
    op.drop_index(op.f('ix_auth_sessions_revoked_at'), table_name='auth_sessions')
    op.drop_index(op.f('ix_auth_sessions_expires_at'), table_name='auth_sessions')
    op.drop_index('ix_auth_sessions_account_revoked_expires', table_name='auth_sessions')
    op.drop_index(op.f('ix_auth_sessions_account_id'), table_name='auth_sessions')
    op.drop_table('auth_sessions')
    op.drop_index('ix_user_tools_scope', table_name='user_tools')
    op.drop_index(op.f('ix_user_tools_owner'), table_name='user_tools')
    op.drop_index(op.f('ix_user_tools_id'), table_name='user_tools')
    op.drop_index('ix_user_tools_active', table_name='user_tools')
    op.drop_table('user_tools')
    op.drop_index(op.f('ix_scheduled_tasks_owner'), table_name='scheduled_tasks')
    op.drop_index(op.f('ix_scheduled_tasks_next_run'), table_name='scheduled_tasks')
    op.drop_index(op.f('ix_scheduled_tasks_id'), table_name='scheduled_tasks')
    op.drop_index('ix_scheduled_tasks_event', table_name='scheduled_tasks')
    op.drop_index('ix_scheduled_tasks_due', table_name='scheduled_tasks')
    op.drop_table('scheduled_tasks')
    op.drop_index('ix_retired_auth_subject_account', table_name='retired_auth_subjects')
    op.drop_table('retired_auth_subjects')
    op.drop_index(op.f('ix_project_stages_project_id'), table_name='project_stages')
    op.drop_index('ix_project_stages_order', table_name='project_stages')
    op.drop_table('project_stages')
    op.drop_index('ix_project_remote_grants_project_status', table_name='project_remote_grants')
    op.drop_index('ix_project_remote_grants_guest_status', table_name='project_remote_grants')
    op.drop_table('project_remote_grants')
    op.drop_index(op.f('ix_project_quota_locks_project_id'), table_name='project_quota_locks')
    op.drop_table('project_quota_locks')
    op.drop_index('ix_project_members_username', table_name='project_members')
    op.drop_table('project_members')
    op.drop_index(op.f('ix_mfa_factors_account_id'), table_name='mfa_factors')
    op.drop_table('mfa_factors')
    op.drop_index(op.f('ix_memories_session_id'), table_name='memories')
    op.drop_index('ix_memories_session', table_name='memories')
    op.drop_index(op.f('ix_memories_owner'), table_name='memories')
    op.drop_index('ix_memories_lookup', table_name='memories')
    op.drop_index(op.f('ix_memories_id'), table_name='memories')
    op.drop_table('memories')
    op.drop_index(op.f('ix_local_credentials_account_id'), table_name='local_credentials')
    op.drop_table('local_credentials')
    op.drop_index('ix_inbox_owner_status_updated', table_name='inbox_items')
    op.drop_index('ix_inbox_owner_kind_status', table_name='inbox_items')
    op.drop_index(op.f('ix_inbox_items_status'), table_name='inbox_items')
    op.drop_index(op.f('ix_inbox_items_owner_id'), table_name='inbox_items')
    op.drop_index(op.f('ix_inbox_items_kind'), table_name='inbox_items')
    op.drop_table('inbox_items')
    op.drop_index(op.f('ix_gallery_images_taken_at'), table_name='gallery_images')
    op.drop_index('ix_gallery_images_tags', table_name='gallery_images')
    op.drop_index(op.f('ix_gallery_images_session_id'), table_name='gallery_images')
    op.drop_index(op.f('ix_gallery_images_owner'), table_name='gallery_images')
    op.drop_index('ix_gallery_images_model', table_name='gallery_images')
    op.drop_index(op.f('ix_gallery_images_id'), table_name='gallery_images')
    op.drop_index(op.f('ix_gallery_images_file_hash'), table_name='gallery_images')
    op.drop_index(op.f('ix_gallery_images_album_id'), table_name='gallery_images')
    op.drop_index('ix_gallery_images_active', table_name='gallery_images')
    op.drop_table('gallery_images')
    op.drop_index('ix_entity_links_target', table_name='entity_links')
    op.drop_index('ix_entity_links_source', table_name='entity_links')
    op.drop_index(op.f('ix_entity_links_owner_id'), table_name='entity_links')
    op.drop_table('entity_links')
    op.drop_index(op.f('ix_documents_source_email_message_id'), table_name='documents')
    op.drop_index(op.f('ix_documents_session_id'), table_name='documents')
    op.drop_index(op.f('ix_documents_owner'), table_name='documents')
    op.drop_index(op.f('ix_documents_id'), table_name='documents')
    op.drop_table('documents')
    op.drop_index('ix_dm_attachment_message_created', table_name='direct_message_attachments')
    op.drop_index(op.f('ix_direct_message_attachments_message_id'), table_name='direct_message_attachments')
    op.drop_table('direct_message_attachments')
    op.drop_index(op.f('ix_crew_members_owner'), table_name='crew_members')
    op.drop_index(op.f('ix_crew_members_id'), table_name='crew_members')
    op.drop_table('crew_members')
    op.drop_index('ix_messages_session_time', table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_session_id'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_id'), table_name='chat_messages')
    op.drop_table('chat_messages')
    op.drop_index(op.f('ix_calendar_events_uid'), table_name='calendar_events')
    op.drop_index(op.f('ix_calendar_events_origin'), table_name='calendar_events')
    op.drop_index(op.f('ix_calendar_events_dtstart'), table_name='calendar_events')
    op.drop_index(op.f('ix_calendar_events_calendar_id'), table_name='calendar_events')
    op.drop_table('calendar_events')
    op.drop_index('ix_auth_identity_issuer_subject', table_name='auth_identities')
    op.drop_index('ix_auth_identity_account_provider', table_name='auth_identities')
    op.drop_index(op.f('ix_auth_identities_account_id'), table_name='auth_identities')
    op.drop_table('auth_identities')
    op.drop_index(op.f('ix_api_tokens_owner'), table_name='api_tokens')
    op.drop_index(op.f('ix_api_tokens_id'), table_name='api_tokens')
    op.drop_index(op.f('ix_api_tokens_account_id'), table_name='api_tokens')
    op.drop_table('api_tokens')
    op.drop_index(op.f('ix_action_audit_owner_id'), table_name='action_audit')
    op.drop_index('ix_action_audit_owner_created', table_name='action_audit')
    op.drop_index(op.f('ix_action_audit_entity_id'), table_name='action_audit')
    op.drop_index('ix_action_audit_entity', table_name='action_audit')
    op.drop_index(op.f('ix_action_audit_created_at'), table_name='action_audit')
    op.drop_index(op.f('ix_action_audit_action'), table_name='action_audit')
    op.drop_table('action_audit')
    op.drop_index('ix_account_roles_role', table_name='account_roles')
    op.drop_index(op.f('ix_account_roles_account_id'), table_name='account_roles')
    op.drop_table('account_roles')
    op.drop_table('account_capabilities')
    op.drop_index(op.f('ix_webhooks_id'), table_name='webhooks')
    op.drop_table('webhooks')
    op.drop_table('user_profiles')
    op.drop_table('user_keys')
    op.drop_index(op.f('ix_study_states_owner'), table_name='study_states')
    op.drop_table('study_states')
    op.drop_index(op.f('ix_status_views_viewer'), table_name='status_views')
    op.drop_index(op.f('ix_status_views_status_id'), table_name='status_views')
    op.drop_index('ix_status_view_pair', table_name='status_views')
    op.drop_table('status_views')
    op.drop_index(op.f('ix_status_posts_expires_at'), table_name='status_posts')
    op.drop_index(op.f('ix_status_posts_created_at'), table_name='status_posts')
    op.drop_index(op.f('ix_status_posts_author'), table_name='status_posts')
    op.drop_table('status_posts')
    op.drop_index(op.f('ix_signatures_owner'), table_name='signatures')
    op.drop_index(op.f('ix_signatures_id'), table_name='signatures')
    op.drop_table('signatures')
    op.drop_index('ix_sessions_search', table_name='sessions')
    op.drop_index(op.f('ix_sessions_owner'), table_name='sessions')
    op.drop_index(op.f('ix_sessions_id'), table_name='sessions')
    op.drop_index('ix_sessions_active', table_name='sessions')
    op.drop_table('sessions')
    op.drop_table('remote_contact_prefs')
    op.drop_index(op.f('ix_remote_blocks_local_user'), table_name='remote_blocks')
    op.drop_index(op.f('ix_remote_blocks_handle'), table_name='remote_blocks')
    op.drop_index('ix_remote_block_pair', table_name='remote_blocks')
    op.drop_table('remote_blocks')
    op.drop_index(op.f('ix_provider_auth_sessions_provider'), table_name='provider_auth_sessions')
    op.drop_index(op.f('ix_provider_auth_sessions_owner'), table_name='provider_auth_sessions')
    op.drop_index(op.f('ix_provider_auth_sessions_id'), table_name='provider_auth_sessions')
    op.drop_table('provider_auth_sessions')
    op.drop_index('ix_projects_owner_archived_updated', table_name='projects')
    op.drop_index(op.f('ix_projects_owner'), table_name='projects')
    op.drop_index(op.f('ix_projects_completed_at'), table_name='projects')
    op.drop_index(op.f('ix_projects_archived'), table_name='projects')
    op.drop_table('projects')
    op.drop_index('ix_progression_owner_source', table_name='progression_events')
    op.drop_index('ix_progression_owner_occurred', table_name='progression_events')
    op.drop_index(op.f('ix_progression_events_source_type'), table_name='progression_events')
    op.drop_index(op.f('ix_progression_events_owner'), table_name='progression_events')
    op.drop_index(op.f('ix_progression_events_occurred_at'), table_name='progression_events')
    op.drop_table('progression_events')
    op.drop_index(op.f('ix_outbound_chat_links_owner'), table_name='outbound_chat_links')
    op.drop_index(op.f('ix_outbound_chat_links_home_url'), table_name='outbound_chat_links')
    op.drop_table('outbound_chat_links')
    op.drop_index(op.f('ix_notes_owner'), table_name='notes')
    op.drop_index(op.f('ix_notes_id'), table_name='notes')
    op.drop_table('notes')
    op.drop_index(op.f('ix_model_endpoints_provider_auth_id'), table_name='model_endpoints')
    op.drop_index(op.f('ix_model_endpoints_owner'), table_name='model_endpoints')
    op.drop_index(op.f('ix_model_endpoints_id'), table_name='model_endpoints')
    op.drop_table('model_endpoints')
    op.drop_index(op.f('ix_mcp_servers_id'), table_name='mcp_servers')
    op.drop_table('mcp_servers')
    op.drop_index(op.f('ix_link_invites_project_id'), table_name='link_invites')
    op.drop_index(op.f('ix_link_invites_code_hash'), table_name='link_invites')
    op.drop_table('link_invites')
    op.drop_index(op.f('ix_link_guests_token_hash'), table_name='link_guests')
    op.drop_index(op.f('ix_link_guests_status'), table_name='link_guests')
    op.drop_index(op.f('ix_link_guests_invite_id'), table_name='link_guests')
    op.drop_index(op.f('ix_link_guests_handle'), table_name='link_guests')
    op.drop_table('link_guests')
    op.drop_index(op.f('ix_integrations_owner'), table_name='integrations')
    op.drop_index(op.f('ix_integrations_id'), table_name='integrations')
    op.drop_table('integrations')
    op.drop_index(op.f('ix_home_link_local_user'), table_name='home_link')
    op.drop_table('home_link')
    op.drop_index(op.f('ix_gallery_albums_owner'), table_name='gallery_albums')
    op.drop_index(op.f('ix_gallery_albums_id'), table_name='gallery_albums')
    op.drop_table('gallery_albums')
    op.drop_index('ix_email_accounts_owner_default', table_name='email_accounts')
    op.drop_index(op.f('ix_email_accounts_owner'), table_name='email_accounts')
    op.drop_index(op.f('ix_email_accounts_id'), table_name='email_accounts')
    op.drop_table('email_accounts')
    op.drop_index(op.f('ix_editor_drafts_source_image_id'), table_name='editor_drafts')
    op.drop_index('ix_editor_drafts_owner_updated', table_name='editor_drafts')
    op.drop_index(op.f('ix_editor_drafts_owner'), table_name='editor_drafts')
    op.drop_index(op.f('ix_editor_drafts_id'), table_name='editor_drafts')
    op.drop_table('editor_drafts')
    op.drop_index('ix_dm_unread', table_name='direct_messages')
    op.drop_index('ix_dm_pair', table_name='direct_messages')
    op.drop_index(op.f('ix_direct_messages_sender'), table_name='direct_messages')
    op.drop_index(op.f('ix_direct_messages_recipient'), table_name='direct_messages')
    op.drop_index(op.f('ix_direct_messages_created_at'), table_name='direct_messages')
    op.drop_table('direct_messages')
    op.drop_index('ix_comparisons_voted_at', table_name='comparisons')
    op.drop_index(op.f('ix_comparisons_owner'), table_name='comparisons')
    op.drop_index(op.f('ix_comparisons_id'), table_name='comparisons')
    op.drop_table('comparisons')
    op.drop_index(op.f('ix_calendars_owner'), table_name='calendars')
    op.drop_index(op.f('ix_calendars_id'), table_name='calendars')
    op.drop_index(op.f('ix_calendars_account_id'), table_name='calendars')
    op.drop_table('calendars')
    op.drop_index(op.f('ix_caldav_deleted_events_uid'), table_name='caldav_deleted_events')
    op.drop_index(op.f('ix_caldav_deleted_events_owner'), table_name='caldav_deleted_events')
    op.drop_index(op.f('ix_caldav_deleted_events_calendar_id'), table_name='caldav_deleted_events')
    op.drop_table('caldav_deleted_events')
    op.drop_table('auth_policy')
    op.drop_table('auth_import_runs')
    op.drop_index(op.f('ix_accounts_username'), table_name='accounts')
    op.drop_index('ix_accounts_status', table_name='accounts')
    op.drop_table('accounts')
