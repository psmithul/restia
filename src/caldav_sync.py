"""CalDAV pull into Restia's canonical calendar authority.

The Settings UI lets users save CalDAV credentials, but the original
sync path was removed when calendar storage was migrated to SQLite.
This module re-wires that gap as a one-way pull (remote → local),
called on calendar open and from a periodic scheduler loop.

Design notes:
- We use the `caldav` lib so PROPFIND discovery + REPORT XML work
  across Radicale / Nextcloud / Apple / Fastmail without us
  reinventing the protocol. It's pure Python.
- The lib is synchronous; we run it in a threadpool via
  `asyncio.to_thread` so the FastAPI event loop stays free.
- Each remote calendar maps to one immutable ``Account.id``-owned
  ``CalendarCal`` row. Legacy username-derived IDs are adopted in place.
- VEVENTs enter only through ``calendar_service``. Pull uses exact event CAS,
  updates the Life Graph and audit in the same transaction, never emits an
  echo delivery, and soft-cancels events that disappear upstream.
- Open durable deliveries and legacy pending generations always win over
  pull. Legacy markers are translated into the encrypted outbox before the
  delivery worker drains them; this module performs no direct write-back.
- Datetimes are converted to UTC and the row is flagged `is_utc=True`
  so the serializer adds the Z suffix and the frontend renders in the
  user's local TZ correctly.
"""

import asyncio
import hashlib
import ipaddress
import logging
import os
import socket
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Pull window: 90 days back, 1 year forward. Keeps the REPORT cheap and
# matches what the calendar UI typically renders. Far-future recurring
# events still come through via RRULE expansion on the frontend.
_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 365
_MAX_DISAPPEARANCE_CHECKS = 500
_BLOCKED_HOSTS = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "metadata.google.internal",
}


def _private_caldav_allowed() -> bool:
    return os.environ.get("ODYSSEUS_ALLOW_PRIVATE_CALDAV", "0").lower() in {"1", "true", "yes"}


def _validate_caldav_address(addr: ipaddress._BaseAddress) -> None:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    ):
        raise ValueError("CalDAV URL host is not allowed")
    if addr.is_private and not _private_caldav_allowed():
        raise ValueError("Private CalDAV IPs require ODYSSEUS_ALLOW_PRIVATE_CALDAV=1")


def _validate_caldav_ip(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    _validate_caldav_address(ip)


def _resolve_caldav_host_ips(host: str) -> list[ipaddress._BaseAddress]:
    addrs: list[ipaddress._BaseAddress] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0].split("%", 1)[0]))
        except ValueError:
            continue
    return addrs


def _validate_caldav_hostname(host: str) -> None:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return
    except ValueError:
        pass
    try:
        addrs = _resolve_caldav_host_ips(host)
    except OSError:
        raise ValueError("CalDAV URL host does not resolve")
    if not addrs:
        raise ValueError("CalDAV URL host does not resolve")
    for addr in addrs:
        _validate_caldav_address(addr)


def validate_caldav_url(raw_url: str) -> str:
    """Validate and normalize a user-provided CalDAV URL before server-side use."""
    url = (raw_url if isinstance(raw_url, str) else "").strip()
    if not url:
        raise ValueError("CalDAV URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("CalDAV URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("CalDAV URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("Put CalDAV credentials in the username/password fields, not the URL")
    if parsed.fragment:
        raise ValueError("CalDAV URL fragments are not allowed")
    try:
        parsed.port
    except ValueError:
        raise ValueError("CalDAV URL has an invalid port")
    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("CalDAV URL host is not allowed")
    _validate_caldav_ip(host)
    _validate_caldav_hostname(host)
    return urlunparse(parsed._replace(fragment="")).rstrip("/")


def _event_etag(obj) -> str:
    """Best-effort ETag extraction from python-caldav resources."""
    try:
        etag = getattr(obj, "etag", None)
        if callable(etag):
            etag = etag()
        return str(etag or "")
    except Exception:
        return ""


def _stable_cal_id(remote_url: str, owner: str = "", account_id: str = "") -> str:
    """Deterministic local id for a remote CalDAV calendar, scoped to owner
    and account so two users — or one user with two accounts — pointing at
    the same server URL get distinct local rows (avoids PK collision, #2765).
    The owner and account_id default to "" for the legacy/URL-only path so
    existing callers without those arguments keep working."""
    key = f"{owner}\n{account_id}\n{remote_url}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"caldav-{h}"


def _to_utc_naive(dt):
    """CalDAV datetimes can be tz-aware (with a TZID) or naive. The DB
    column is naive but we set is_utc=True so the serializer adds Z.
    All-day events stay as date and get widened to datetime here."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None), False
        return dt, False  # naive → treat as local
    # date-only (all-day)
    return datetime(dt.year, dt.month, dt.day), True


def _find_existing_event(db, pending, uid_val, calendar_id, owner_id):
    """Find exactly one owner's event generation for one collection."""
    from core.database import CalendarEvent
    return pending.get((owner_id, uid_val)) or db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid_val,
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.calendar_id == calendar_id,
    ).first()


def _resolve_account(db, owner: str):
    """Resolve a legacy login alias once, then use immutable Account.id."""

    from core.database import Account
    from src.identity import ensure_account, normalize_identity

    raw = str(owner or "").strip()
    normalized = normalize_identity(raw)
    if not normalized:
        raise ValueError("A concrete CalDAV owner is required")
    account = db.query(Account).filter(
        (Account.id == raw) | (Account.username == normalized),
        Account.status == "active",
    ).first()
    return account if account is not None else ensure_account(db, normalized)


def _positive_duration(start: datetime, end: datetime, all_day: bool) -> datetime:
    if end > start:
        return end
    return start + (timedelta(days=1) if all_day else timedelta(hours=1))


def _component_exdates(component, *, all_day: bool) -> list[str]:
    """Normalize iCalendar EXDATE values to Restia occurrence keys."""

    raw = component.get("exdate")
    if raw is None:
        return []
    properties = raw if isinstance(raw, list) else [raw]
    values: list[str] = []
    for prop in properties:
        for item in list(getattr(prop, "dts", ()) or ()):
            value = getattr(item, "dt", None)
            if isinstance(value, datetime):
                if value.tzinfo is not None:
                    value = value.astimezone(timezone.utc).replace(tzinfo=None)
                values.append(
                    value.date().isoformat()
                    if all_day else value.strftime("%Y-%m-%dT%H:%M")
                )
            elif isinstance(value, date):
                values.append(value.isoformat())
    return sorted(set(values))


def _safe_remote_href(collection_url: str, href: object) -> str | None:
    """Resolve one discovered resource href without accepting URL credentials."""

    from src.caldav_writeback import _same_origin_resource

    raw = str(href or "").strip()
    if not raw:
        return None
    target = _same_origin_resource(collection_url, raw)
    parsed = urlparse(target)
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("CalDAV resource URL is not allowed")
    return target


def _remote_uid_presence(remote_calendar, uid: str) -> tuple[str, str | None]:
    """Confirm whether a window-missing UID still exists on the collection.

    A calendar-query window is not a deletion feed: an event rescheduled beyond
    the window also disappears from ``date_search``.  Only an authoritative
    UID lookup returning NotFound permits local soft-cancellation. Transport,
    auth, or unsupported-query failures remain unknown and fail closed.
    """

    from caldav.lib.error import NotFoundError

    try:
        remote_calendar.event_by_uid(uid)
    except NotFoundError:
        return "missing", None
    except Exception as exc:
        return "unknown", type(exc).__name__
    return "present", None


def _google_caldav_events_url(url: str) -> str | None:
    """Map a Google CalDAV *principal* URL to its event-collection URL.

    Google serves the principal at ``…/user`` but events live under ``…/events``
    — the ``/user`` resource holds no VEVENTs. The `caldav` library's
    principal→home-set discovery does not reliably enumerate calendars from
    Google's ``/user`` endpoint, so the sync falls into the "treat the URL as a
    single calendar" fallback below. Pointed at ``/user`` that fallback issues
    every calendar-query REPORT against the principal, which returns a clean but
    empty 200 for all date ranges — the calendar shows no events even though
    auth succeeded (issue #2507).

    Both Google CalDAV endpoint forms are handled, since some accounts only
    authenticate against one of them:
      - newer:  ``https://apidata.googleusercontent.com/caldav/v2/<id>/user``
      - legacy: ``https://www.google.com/calendar/dav/<id>/user``

    Returns the events URL for a recognised Google principal URL, else None so
    the caller keeps the original URL unchanged.
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if not path.endswith("/user"):
        return None
    is_google = (
        host.endswith("googleusercontent.com")                       # newer /caldav/v2 form
        or (host in ("www.google.com", "google.com") and "/calendar/dav/" in path)  # legacy form
    )
    if not is_google:
        return None
    new_path = path[: -len("/user")] + "/events"
    return urlunparse(parts._replace(path=new_path))


def _open_url_as_calendar(client, url: str):
    """Open ``url`` as a single calendar collection.

    Used when principal discovery yields no calendars. Google's principal URL
    is not an event collection, so map it to the events URL first
    (see ``_google_caldav_events_url``); other servers' URLs are used as-is.
    """
    target = _google_caldav_events_url(url) or url
    return client.calendar(url=target)


def _save_caldav_accounts(owner: str, accounts: list) -> None:
    from routes.prefs_routes import _load_for_user, _save_for_user

    prefs = _load_for_user(owner) or {}
    prefs["caldav_accounts"] = accounts
    prefs.pop("caldav", None)
    _save_for_user(owner, prefs)


def _ensure_google_calendar_token(acc: dict, owner: str) -> str | None:
    if acc.get("oauth_provider") != "google":
        return None

    import time
    from src.secret_storage import decrypt as _dec, encrypt as _enc

    access_token = _dec(acc.get("oauth_access_token") or "")
    try:
        expiry = int(acc.get("oauth_token_expiry") or 0)
    except (TypeError, ValueError):
        expiry = 0

    if expiry > time.time() + 300 and access_token:
        return access_token

    refresh_token = _dec(acc.get("oauth_refresh_token") or "")
    if not refresh_token:
        return access_token

    import os, httpx
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.warning("Google Calendar token refresh skipped: OAuth client id/secret not configured")
        return access_token

    try:
        resp = httpx.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        new_access = data["access_token"]
        acc["oauth_access_token"] = _enc(new_access)
        acc["oauth_token_expiry"] = str(int(time.time()) + data.get("expires_in", 3600))

        accounts = _load_caldav_accounts(owner)
        for a in accounts:
            if a.get("id") == acc.get("id"):
                a["oauth_access_token"] = acc["oauth_access_token"]
                a["oauth_token_expiry"] = acc["oauth_token_expiry"]
        _save_caldav_accounts(owner, accounts)
        return new_access
    except Exception as e:
        logger.warning(f"Google Calendar token refresh failed: {e}")
        return access_token


def _build_dav_client(url: str, username: str, password: str, oauth_access_token: str = None):
    """Construct a CalDAV client with automatic redirects disabled.

    ``validate_caldav_url`` resolves and vets the *initial* host, but caldav's
    underlying HTTP session follows 3xx redirects by default. So a URL that
    passes validation can still be redirected — at request time — to
    loopback / link-local / private space, re-opening the SSRF the host check
    closes. Pin the session to zero redirects: any 3xx then raises instead of
    silently following an attacker-chosen ``Location``. This mirrors the
    test-connection path in ``routes/calendar_routes.py``, which already sets
    ``follow_redirects=False``.

    DAVClient exposes no per-request redirect flag, so we set it on the session
    after construction (the session is created in ``__init__``).
    """
    import caldav

    client = caldav.DAVClient(
        url=url,
        username="" if oauth_access_token else username,
        password="" if oauth_access_token else password,
    )
    if oauth_access_token:
        if not hasattr(client.session, "headers") or client.session.headers is None:
            client.session.headers = {}
        client.session.headers["Authorization"] = f"Bearer {oauth_access_token}"

    # Unconditional: a redirect-disable that only sometimes applies is not a
    # control. The session exists right after __init__ on every real client;
    # test_build_dav_client_disables_redirects asserts it against installed
    # caldav in CI.
    client.session.max_redirects = 0
    return client


def _should_prune_window(seen_uids: set, parse_failed: bool) -> bool:
    """Whether the post-sync prune of vanished CalDAV events is safe to run.

    The prune soft-cancels local ``origin=="caldav"`` rows in the window whose
    UID the server did not just return. Any parse failure (total or partial) makes
    ``seen_uids`` an incomplete view of the server, so pruning against it can
    cancel events that still exist upstream but could not be read: a total
    failure cancels the whole window, a partial failure cancels just the
    unreadable ones. Only prune on a clean read. An empty ``seen_uids`` after a
    clean read is a genuinely empty window, which is safe to prune.
    """
    return not parse_failed


def _sync_blocking(owner: str, url: str, username: str, password: str, account_id: str = "", oauth_access_token: str = None) -> dict:
    """The actual sync — synchronous, intended to run in a threadpool.
    Returns counts: {calendars, events, deleted, skipped, errors}."""
    # Lazy imports so a missing `caldav` dep doesn't break app startup —
    # the integrations form still works, sync just no-ops with an error.
    from caldav.lib.error import AuthorizationError, NotFoundError
    from core.database import Account, CalendarCal, CalendarEvent, SessionLocal
    from src.audit_context import bind_service_audit_context
    from src.caldav_writeback import _same_origin_resource
    from src.calendar_service import (
        CalendarConflict,
        CalendarRemoteWritePending,
        CalendarServiceError,
        cancel_missing_remote_calendar_event,
        ingest_remote_calendar_event,
        upsert_caldav_calendar,
    )

    result = {
        "calendars": 0, "events": 0, "deleted": 0, "skipped": 0,
        "errors": [],
    }

    # Keep the internal entry point safe even when a test/CLI bypasses the
    # public async wrapper that normally performs this validation first.
    url = validate_caldav_url(url)
    client = _build_dav_client(url, username, password, oauth_access_token)

    # Discovery: try principal → calendars first; if the server doesn't
    # support discovery (or the URL points directly at a calendar), fall
    # back to treating the URL as a single calendar.
    calendars = []
    try:
        principal = client.principal()
        calendars = principal.calendars()
    except (AuthorizationError, NotFoundError) as e:
        result["errors"].append(f"Discovery failed: {e}")
        return result
    except Exception as e:
        logger.info(f"CalDAV principal discovery failed, trying URL as calendar: {e}")
        try:
            calendars = [_open_url_as_calendar(client, url)]
        except Exception as e2:
            result["errors"].append(f"Could not open URL as calendar: {e2}")
            return result

    if not calendars:
        try:
            calendars = [_open_url_as_calendar(client, url)]
        except Exception as e:
            result["errors"].append(f"No calendars and URL fallback failed: {e}")
            return result

    start = datetime.utcnow() - timedelta(days=_LOOKBACK_DAYS)
    end = datetime.utcnow() + timedelta(days=_LOOKAHEAD_DAYS)

    db = SessionLocal()
    try:
        account = _resolve_account(db, owner)
        bind_service_audit_context(
            db,
            account_id=account.id,
            interface="automation",
            actor_type="connector",
            credential_type="caldav",
        )
        resolved_owner_id = str(account.id)
        db.commit()
        for remote_cal in calendars:
            try:
                remote_url = validate_caldav_url(str(remote_cal.url))
                _same_origin_resource(url, remote_url)
                display_name = (remote_cal.name or "").strip() or "CalDAV"
                connector_id = str(account_id or "").strip() or None
                account = db.query(Account).filter(
                    Account.id == resolved_owner_id,
                    Account.status == "active",
                ).one()
                # Preserve an already-migrated/legacy calendar id before using
                # the new immutable-owner deterministic id for first discovery.
                local_cal = db.query(CalendarCal).filter(
                    CalendarCal.owner_id == resolved_owner_id,
                    CalendarCal.source == "caldav",
                    CalendarCal.account_id == connector_id,
                    CalendarCal.caldav_base_url == remote_url,
                ).order_by(CalendarCal.created_at.asc()).first()
                if local_cal is None:
                    unbound = db.query(CalendarCal).filter(
                        CalendarCal.owner_id == resolved_owner_id,
                        CalendarCal.source == "caldav",
                        CalendarCal.account_id.is_(None),
                        CalendarCal.caldav_base_url == remote_url,
                    ).order_by(CalendarCal.created_at.asc()).limit(2).all()
                    if len(unbound) == 1:
                        local_cal = unbound[0]
                if local_cal is None:
                    legacy_id = _stable_cal_id(
                        remote_url, owner=owner, account_id=account_id,
                    )
                    local_cal = db.query(CalendarCal).filter(
                        CalendarCal.id == legacy_id,
                        CalendarCal.owner_id == resolved_owner_id,
                    ).first()
                cal_id = (
                    local_cal.id if local_cal is not None else
                    _stable_cal_id(
                        remote_url, owner=resolved_owner_id, account_id=account_id,
                    )
                )
                local_cal, _created = upsert_caldav_calendar(
                    db,
                    account=account,
                    calendar_id=cal_id,
                    name=display_name,
                    connector_account_id=connector_id,
                    remote_url=remote_url,
                )
                owner_id = resolved_owner_id
                local_calendar_id = str(local_cal.id)
                db.commit()
                result["calendars"] += 1

                # Fetch events in window. `date_search` returns CalendarObject
                # resources; each may contain one VEVENT (most servers) or
                # several (rare).
                from icalendar import Calendar as iCal

                seen_uids = set()
                # Track events added to the session but not yet committed so
                # duplicate UIDs within the same batch are updated, not re-inserted
                # (which would violate the UNIQUE constraint on commit).
                pending: dict = {}
                parse_failed = False
                try:
                    objs = remote_cal.date_search(start=start, end=end, expand=False)
                except Exception as e:
                    result["errors"].append(f"{display_name}: date_search failed ({e})")
                    continue

                for obj in objs:
                    # A resource property may lazily issue WebDAV requests.
                    # Commit prior mutations and detach its transport metadata
                    # before opening the next authority transaction.
                    db.commit()
                    pending.clear()
                    try:
                        ical = iCal.from_ical(obj.data)
                        remote_href = _safe_remote_href(
                            remote_url, getattr(obj, "url", ""),
                        )
                        remote_etag = _event_etag(obj) or None
                    except Exception as e:
                        result["errors"].append(f"{display_name}: parse failed ({e})")
                        parse_failed = True
                        continue

                    for comp in ical.walk():
                        if comp.name != "VEVENT":
                            continue
                        uid_val = str(comp.get("uid", "")).strip()
                        if not uid_val:
                            result["errors"].append(
                                f"{display_name}: VEVENT is missing UID"
                            )
                            parse_failed = True
                            continue
                        seen_uids.add(uid_val)

                        dtstart_p = comp.get("dtstart")
                        if not dtstart_p:
                            result["errors"].append(
                                f"{display_name}: {uid_val}: VEVENT is missing DTSTART"
                            )
                            parse_failed = True
                            continue
                        start_dt, all_day = _to_utc_naive(dtstart_p.dt)

                        dtend_p = comp.get("dtend")
                        if dtend_p:
                            end_dt, _ = _to_utc_naive(dtend_p.dt)
                        elif all_day:
                            end_dt = start_dt + timedelta(days=1)
                        else:
                            end_dt = start_dt + timedelta(hours=1)
                        # A synced event with DTEND <= DTSTART (e.g. a single-day
                        # all-day event whose source wrote DTEND equal to DTSTART)
                        # would be stored zero-duration and silently dropped by the
                        # list_events overlap filter. Clamp to a positive span.
                        end_dt = _positive_duration(start_dt, end_dt, all_day)

                        # is_utc reflects whether the source carried a TZ
                        # we converted from. All-day = no TZ semantics.
                        row_is_utc = (
                            not all_day
                            and isinstance(dtstart_p.dt, datetime)
                            and dtstart_p.dt.tzinfo is not None
                        )

                        summary = str(comp.get("summary", ""))
                        description = str(comp.get("description", ""))
                        location = str(comp.get("location", ""))
                        rrule = (
                            comp.get("rrule").to_ical().decode()
                            if comp.get("rrule")
                            else ""
                        )
                        existing = _find_existing_event(
                            db, pending, uid_val, local_calendar_id, owner_id,
                        )
                        expected_version = (
                            int(existing.version or 1) if existing is not None else None
                        )
                        start_input = (
                            start_dt.date().isoformat()
                            if all_day else
                            start_dt.replace(tzinfo=timezone.utc).isoformat().replace(
                                "+00:00", "Z"
                            ) if row_is_utc else start_dt.isoformat()
                        )
                        end_input = (
                            end_dt.date().isoformat()
                            if all_day else
                            end_dt.replace(tzinfo=timezone.utc).isoformat().replace(
                                "+00:00", "Z"
                            ) if row_is_utc else end_dt.isoformat()
                        )
                        try:
                            with db.begin_nested():
                                mutation = ingest_remote_calendar_event(
                                    db,
                                    account=account,
                                    calendar_id=local_calendar_id,
                                    uid=uid_val,
                                    expected_version=expected_version,
                                    summary=summary,
                                    description=description,
                                    location=location,
                                    dtstart=start_input,
                                    dtend=end_input,
                                    all_day=all_day,
                                    is_utc=row_is_utc,
                                    rrule=rrule,
                                    recurrence_exdates=_component_exdates(
                                        comp, all_day=all_day,
                                    ),
                                    status=(
                                        "cancelled"
                                        if str(comp.get("status", "")).upper()
                                        == "CANCELLED" else "confirmed"
                                    ),
                                    remote_href=remote_href,
                                    remote_etag=remote_etag,
                                )
                            pending[(owner_id, uid_val)] = mutation.event
                            result["events"] += 1
                        except CalendarRemoteWritePending:
                            # The local committed generation is authoritative;
                            # seeing the UID is enough to keep prune safe.
                            result["skipped"] += 1
                        except (CalendarConflict, CalendarServiceError) as exc:
                            result["errors"].append(
                                f"{display_name}: {uid_val}: {str(exc)[:160]}"
                            )
                            parse_failed = True
                db.commit()

                # Soft-cancel locally-cached CalDAV events that vanished
                # upstream (only within our sync window — events outside
                # the window aren't in `objs`, so we'd false-delete them).
                # Only rows previously pulled from the server are eligible;
                # locally-created rows never disappear just because the server
                # has not observed their durable delivery yet.
                # Skip the prune on any parse failure: seen_uids is then an
                # incomplete view of the server, so pruning against it would
                # delete events that still exist upstream but could not be read
                # (the empty-seen_uids case wipes the whole window; a partial
                # failure deletes just the unreadable rows).
                if _should_prune_window(seen_uids, parse_failed):
                    stale = db.query(CalendarEvent).filter(
                        CalendarEvent.calendar_id == local_calendar_id,
                        CalendarEvent.owner_id == owner_id,
                        CalendarEvent.origin == "caldav",
                        CalendarEvent.dtstart >= start,
                        CalendarEvent.dtstart <= end,
                        CalendarEvent.remote_href.isnot(None),
                        ~CalendarEvent.uid.in_(seen_uids) if seen_uids else CalendarEvent.uid.isnot(None),
                    ).order_by(
                        CalendarEvent.updated_at.asc(), CalendarEvent.uid.asc(),
                    ).limit(_MAX_DISAPPEARANCE_CHECKS).all()
                    stale_refs = [
                        (str(ev.uid), int(ev.version or 1)) for ev in stale
                    ]
                    # UID verification can perform network I/O. End the read
                    # transaction and detach primitives before asking the
                    # server whether each window-missing resource truly died.
                    db.rollback()
                    confirmed_missing: list[tuple[str, int]] = []
                    for event_uid, event_version in stale_refs:
                        presence, error_code = _remote_uid_presence(
                            remote_cal, event_uid,
                        )
                        if presence == "missing":
                            confirmed_missing.append((event_uid, event_version))
                        elif presence == "unknown":
                            result["skipped"] += 1
                            result["errors"].append(
                                f"{display_name}: {event_uid}: deletion "
                                f"verification failed ({error_code})"
                            )
                    if confirmed_missing:
                        account = db.query(Account).filter(
                            Account.id == owner_id,
                            Account.status == "active",
                        ).one()
                    for event_uid, event_version in confirmed_missing:
                        try:
                            with db.begin_nested():
                                cancel_missing_remote_calendar_event(
                                    db,
                                    account=account,
                                    calendar_id=local_calendar_id,
                                    uid=event_uid,
                                    expected_version=event_version,
                                )
                            result["deleted"] += 1
                        except CalendarRemoteWritePending:
                            result["skipped"] += 1
                        except (CalendarConflict, CalendarServiceError) as exc:
                            result["errors"].append(
                                f"{display_name}: {event_uid}: {str(exc)[:160]}"
                            )
                    db.commit()
            except Exception as e:
                logger.exception("CalDAV sync failed for one calendar")
                result["errors"].append(str(e)[:200])
                db.rollback()
    finally:
        db.close()

    return result


def _adopt_legacy_pending(owner: str, *, limit: int = 1_000) -> dict:
    """Translate committed legacy event markers into durable deliveries."""

    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from src.audit_context import bind_service_audit_context
    from src.calendar_service import (
        CalendarServiceError,
        adopt_legacy_caldav_delivery,
    )

    result = {"owner_id": None, "adopted": 0, "skipped": 0, "errors": []}
    db = SessionLocal()
    try:
        account = _resolve_account(db, owner)
        result["owner_id"] = account.id
        bind_service_audit_context(
            db,
            account_id=account.id,
            interface="automation",
            actor_type="service",
            credential_type="caldav",
        )
        rows = db.query(CalendarEvent).join(
            CalendarCal,
            (CalendarCal.id == CalendarEvent.calendar_id)
            & (CalendarCal.owner_id == CalendarEvent.owner_id),
        ).filter(
            CalendarEvent.owner_id == account.id,
            CalendarCal.source == "caldav",
            CalendarEvent.caldav_sync_pending.isnot(None),
        ).order_by(
            CalendarEvent.updated_at.asc(), CalendarEvent.uid.asc(),
        ).limit(max(1, min(int(limit), 10_000))).all()
        for event in rows:
            try:
                with db.begin_nested():
                    delivery = adopt_legacy_caldav_delivery(
                        db,
                        account=account,
                        uid=event.uid,
                        expected_version=int(event.version or 1),
                    )
                result["adopted" if delivery is not None else "skipped"] += 1
            except CalendarServiceError as exc:
                result["errors"].append(f"{event.uid}: {str(exc)[:160]}")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return result


def _load_caldav_accounts(owner: str) -> list:
    """Return the list of CalDAV accounts for *owner*, auto-migrating the legacy
    single-account ``caldav`` key to the new ``caldav_accounts`` list on first call.

    The save step is best-effort: if ``_save_for_user`` is unavailable (e.g. in a
    test with a minimal prefs mock) the migrated accounts are still returned; the
    next real call will just re-run the cheap migration again.
    """
    import uuid as _uuid
    from routes.prefs_routes import _load_for_user

    prefs = _load_for_user(owner) or {}
    if "caldav_accounts" in prefs:
        return list(prefs["caldav_accounts"] or [])
    # Migrate legacy single-account config to the list format.
    legacy = prefs.get("caldav", {}) or {}
    if legacy.get("url"):
        accounts = [{
            "id": str(_uuid.uuid4()),
            "label": "CalDAV",
            "url": legacy["url"],
            "username": legacy.get("username", ""),
            "password": legacy.get("password", ""),
        }]
        prefs["caldav_accounts"] = accounts
        prefs.pop("caldav", None)
        try:
            from routes.prefs_routes import _save_for_user
            _save_for_user(owner, prefs)
        except (ImportError, AttributeError):
            pass  # best-effort; next call re-migrates from the still-present legacy key
        return accounts
    return []


async def sync_caldav(owner: str) -> dict:
    """Pull CalDAV state into local DB for `owner` across all configured accounts.
    Returns aggregated counts + per-account errors."""
    from src.secret_storage import decrypt

    accounts = _load_caldav_accounts(owner)
    if not accounts:
        return {
            "calendars": 0, "events": 0, "deleted": 0, "skipped": 0,
            "errors": ["CalDAV is not configured"],
        }

    adopted = await asyncio.to_thread(_adopt_legacy_pending, owner)
    totals: dict = {
        "calendars": 0, "events": 0, "deleted": 0, "skipped": 0,
        "legacy_adopted": int(adopted.get("adopted", 0)),
        "errors": list(adopted.get("errors", [])),
    }
    for acc in accounts:
        url = (acc.get("url") or "").strip()
        user = (acc.get("username") or "").strip()
        pw = acc.get("password") or ""
        account_id = acc.get("id") or ""
        label = acc.get("label") or url or account_id
        try:
            pw = decrypt(pw)
        except Exception:
            pass

        access_token = None
        if acc.get("oauth_provider") == "google":
            access_token = _ensure_google_calendar_token(acc, owner)

        if not (url and user and (pw or access_token)):
            totals["errors"].append(f"{label}: missing URL, username, or password/token")
            continue
        try:
            url = validate_caldav_url(url)
            result = await asyncio.to_thread(_sync_blocking, owner, url, user, pw, account_id, access_token)
        except ValueError as e:
            result = {"calendars": 0, "events": 0, "deleted": 0, "skipped": 0, "errors": [str(e)]}
        except Exception as e:
            logger.exception("CalDAV sync raised for account %s", label)
            result = {"calendars": 0, "events": 0, "deleted": 0, "skipped": 0, "errors": [str(e)[:200]]}
        totals["calendars"] += result.get("calendars", 0)
        totals["events"] += result.get("events", 0)
        totals["deleted"] += result.get("deleted", 0)
        totals["skipped"] += result.get("skipped", 0)
        for err in result.get("errors", []):
            totals["errors"].append(f"{label}: {err}")
    return totals


async def push_event_create(owner: str, uid: str) -> dict:
    """Compatibility shim: drain the durable outbox, never direct write-back."""

    result = await push_pending_events(owner)
    return {
        "ok": not result.get("errors") and not result.get("conflicts"),
        "uid": str(uid),
        **result,
    }


async def push_event_update(owner: str, uid: str) -> dict:
    return await push_event_create(owner, uid)


async def push_event_delete(owner: str, uid: str) -> dict:
    return await push_event_create(owner, uid)


async def push_pending_events(owner: str) -> dict:
    """Adopt legacy markers, then drain committed CalendarDelivery rows."""

    from core.database import SessionLocal
    from src.calendar_delivery import drain_calendar_deliveries

    adopted = await asyncio.to_thread(_adopt_legacy_pending, owner)
    owner_id = str(adopted.get("owner_id") or "")
    if not owner_id:
        return {
            "events": 0, "completed": 0, "retried": 0, "conflicts": 0,
            "legacy_adopted": 0,
            "errors": ["CalDAV owner account is unavailable"],
        }
    drained = await asyncio.to_thread(
        drain_calendar_deliveries,
        SessionLocal,
        owner_id=owner_id,
        limit=500,
    )
    return {
        "events": int(drained.get("completed", 0)),
        "completed": int(drained.get("completed", 0)),
        "retried": int(drained.get("retried", 0)),
        "conflicts": int(drained.get("conflicts", 0)),
        "legacy_adopted": int(adopted.get("adopted", 0)),
        "errors": list(adopted.get("errors", [])),
    }


async def sync_caldav_direction(owner: str, direction: str = "pull") -> dict:
    direction = (direction or "pull").strip().lower()
    if direction == "pull":
        return await sync_caldav(owner)
    if direction == "push":
        return await push_pending_events(owner)
    if direction == "both":
        pushed = await push_pending_events(owner)
        pulled = await sync_caldav(owner)
        return {"push": pushed, "pull": pulled}
    return {
        "calendars": 0,
        "events": 0,
        "deleted": 0,
        "skipped": 0,
        "errors": [f"Unsupported CalDAV sync direction: {direction}"],
    }
