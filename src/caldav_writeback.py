"""Hardened CalDAV transport primitives for durable CalendarDelivery work.

The V3 write authority is ``calendar_service`` plus the encrypted outbox in
``calendar_delivery``.  The old immediate ``writeback_event`` API is retained
only as a fail-closed compatibility boundary; it never performs network I/O.
"""

import ipaddress
import logging
import socket
import ssl
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpcore
import httpx

logger = logging.getLogger(__name__)

MAX_CALDAV_RESPONSE_BYTES = 16 * 1024 * 1024


class CalDAVError(RuntimeError):
    """Sanitized CalDAV transport or protocol failure."""


class CalDAVTransportError(CalDAVError):
    """A request did not produce an authoritative HTTP response."""


class CalDAVAuthError(CalDAVError):
    """The configured CalDAV credential was rejected."""


class CalDAVNotFound(CalDAVError):
    """The exact requested CalDAV resource does not exist."""


class CalDAVDiscoveryError(CalDAVError):
    """The configured calendar collection cannot accept the operation."""


class CalDAVConflict(CalDAVError):
    """The remote resource no longer matches Restia's stored ETag."""


class _PinnedSyncBackend(httpcore.NetworkBackend):
    """Connect to one vetted IP while preserving hostname TLS and SNI."""

    def __init__(self, ip: ipaddress._BaseAddress):
        self._ip = str(ip)
        self._real = httpcore.SyncBackend()

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None,
    ):
        return self._real.connect_tcp(
            self._ip, port, timeout, local_address, socket_options,
        )

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return self._real.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        self._real.sleep(seconds)


class _PinnedSyncTransport(httpx.BaseTransport):
    """A no-proxy transport that bounds response bytes before buffering."""

    def __init__(self, ip: ipaddress._BaseAddress):
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            http1=True,
            http2=False,
            network_backend=_PinnedSyncBackend(ip),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        core_response = self._pool.handle_request(core_request)
        try:
            chunks: list[bytes] = []
            size = 0
            for chunk in core_response.iter_stream():
                size += len(chunk)
                if size > MAX_CALDAV_RESPONSE_BYTES:
                    raise CalDAVTransportError(
                        "CalDAV response exceeded the size limit"
                    )
                chunks.append(chunk)
            content = b"".join(chunks)
        finally:
            core_response.close()
        return httpx.Response(
            status_code=core_response.status,
            headers=core_response.headers,
            content=content,
            extensions=core_response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def _resolved_ips(host: str) -> list[ipaddress._BaseAddress]:
    values: list[ipaddress._BaseAddress] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            value = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
        except ValueError:
            continue
        if value not in values:
            values.append(value)
    return values


def _secure_request(method: str, url: str, **kwargs) -> httpx.Response:
    """Resolve, revalidate, pin, and bound one non-redirecting DAV request."""

    from src.caldav_sync import _validate_caldav_address, validate_caldav_url

    try:
        cleaned = validate_caldav_url(url)
        parsed = urlparse(cleaned)
        # Resolve again immediately before constructing the pinned socket. This
        # closes the validation/connect DNS-rebinding gap.
        ips = _resolved_ips(str(parsed.hostname or ""))
        if not ips:
            raise ValueError("CalDAV URL host does not resolve")
        for ip in ips:
            _validate_caldav_address(ip)
    except (OSError, ValueError) as exc:
        raise CalDAVDiscoveryError("CalDAV destination is not allowed") from exc

    try:
        with httpx.Client(
            transport=_PinnedSyncTransport(ips[0]),
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
        ) as client:
            return client.request(method, cleaned, **kwargs)
    except CalDAVError:
        raise
    except (
        httpx.HTTPError,
        httpcore.NetworkError,
        httpcore.TimeoutException,
        httpcore.ProtocolError,
        httpcore.ProxyError,
        OSError,
    ) as exc:
        raise CalDAVTransportError("CalDAV request failed") from exc


def _auth_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    token = str(config.get("access_token") or "")
    if token:
        return {"headers": {"Authorization": f"Bearer {token}"}}
    username = str(config.get("username") or "")
    password = str(config.get("password") or "")
    return {"auth": (username, password)} if username else {}


def _same_origin_resource(collection_url: str, href: str) -> str:
    """Resolve a server href without allowing credentials to cross origins."""

    target = urljoin(collection_url.rstrip("/") + "/", str(href or ""))
    base = urlparse(collection_url)
    parsed = urlparse(target)
    base_port = base.port or (443 if base.scheme == "https" else 80)
    target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if (
        parsed.scheme.lower() != base.scheme.lower()
        or (parsed.hostname or "").lower() != (base.hostname or "").lower()
        or target_port != base_port
    ):
        raise CalDAVConflict("CalDAV resource changed origin")
    return target


def deterministic_event_href(collection_url: str, uid: str) -> str:
    """Return the stable create target used for every replay of one UID."""

    cleaned_uid = str(uid or "").strip()
    if not cleaned_uid:
        raise CalDAVConflict("Calendar event UID is missing")
    return urljoin(
        collection_url.rstrip("/") + "/",
        quote(cleaned_uid, safe="") + ".ics",
    )


def _semantic_events(raw_ical: str) -> tuple[tuple[Any, ...], ...]:
    """Canonicalize meaningful VEVENT properties, ignoring server metadata."""

    from icalendar import Calendar

    ignored = {
        "BEGIN", "END", "DTSTAMP", "CREATED", "LAST-MODIFIED", "SEQUENCE",
    }
    try:
        calendar = Calendar.from_ical(str(raw_ical or ""))
    except Exception as exc:
        raise CalDAVConflict("Calendar resource is not valid iCalendar") from exc
    events: list[tuple[Any, ...]] = []
    for component in calendar.walk("VEVENT"):
        values: list[tuple[str, tuple[tuple[str, str], ...], str]] = []
        for name, value in component.property_items(recursive=False):
            upper = str(name).upper()
            if upper in ignored:
                continue
            params = tuple(sorted(
                (str(key).upper(), str(param))
                for key, param in dict(getattr(value, "params", {}) or {}).items()
            ))
            encoded = value.to_ical() if hasattr(value, "to_ical") else str(value)
            if isinstance(encoded, bytes):
                encoded = encoded.decode("utf-8", "replace")
            values.append((upper, params, str(encoded)))
        events.append(tuple(sorted(values)))
    if not events:
        raise CalDAVConflict("Calendar resource has no VEVENT")
    return tuple(sorted(events))


def semantically_equal_ical(left: str, right: str) -> bool:
    """Compare event meaning while tolerating server-added envelope metadata."""

    return _semantic_events(left) == _semantic_events(right)


def _classify_exact_get(response: httpx.Response) -> tuple[str, str]:
    if response.status_code in {401, 403}:
        raise CalDAVAuthError("CalDAV authentication failed")
    if response.status_code == 404:
        raise CalDAVNotFound("CalDAV resource was not found")
    if 300 <= response.status_code < 400:
        raise CalDAVTransportError("CalDAV redirect was refused")
    if response.status_code != 200:
        raise CalDAVTransportError("CalDAV resource read failed")
    return response.text, str(response.headers.get("etag") or "")


def get_calendar_resource(
    config: dict[str, Any], *, collection_url: str, href: str,
) -> tuple[str, str]:
    target = _same_origin_resource(collection_url, href)
    kwargs = _auth_kwargs(config)
    response = _secure_request("GET", target, **kwargs)
    return _classify_exact_get(response)


def _replay_result_if_equal(
    config: dict[str, Any], *, collection_url: str, href: str, raw_ical: str,
) -> tuple[str, str] | None:
    try:
        current, etag = get_calendar_resource(
            config, collection_url=collection_url, href=href,
        )
    except CalDAVNotFound:
        return None
    if semantically_equal_ical(current, raw_ical):
        if not etag:
            raise CalDAVTransportError(
                "CalDAV resource has no verifiable ETag"
            )
        return href, etag
    return None


def put_calendar_event(
    config: dict[str, Any],
    *,
    collection_url: str,
    uid: str,
    raw_ical: str,
    operation: str,
    href: str | None = None,
    etag: str | None = None,
) -> tuple[str, str]:
    """Conditionally create/update one exact event and reconcile crash replay."""

    normalized = str(operation or "").lower()
    if normalized == "create":
        target = deterministic_event_href(collection_url, uid)
        precondition = {"If-None-Match": "*"}
    elif normalized == "update":
        if not href or not etag:
            raise CalDAVConflict("Calendar update is missing its stored ETag")
        target = _same_origin_resource(collection_url, href)
        precondition = {"If-Match": str(etag)}
    else:
        raise CalDAVConflict("Calendar delivery operation is invalid")

    kwargs = _auth_kwargs(config)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(precondition)
    headers["Content-Type"] = "text/calendar; charset=utf-8"
    response = _secure_request(
        "PUT", target, headers=headers, content=str(raw_ical).encode("utf-8"),
        **kwargs,
    )
    if response.status_code in {200, 201, 204}:
        delivered_etag = str(response.headers.get("etag") or "")
        if delivered_etag:
            return target, delivered_etag
        replay = _replay_result_if_equal(
            config, collection_url=collection_url, href=target,
            raw_ical=raw_ical,
        )
        if replay is not None:
            return replay
        raise CalDAVTransportError("CalDAV write returned no verifiable ETag")
    if response.status_code in {401, 403}:
        raise CalDAVAuthError("CalDAV authentication failed")
    if response.status_code == 404:
        if normalized == "update":
            raise CalDAVNotFound("CalDAV event was not found")
        raise CalDAVDiscoveryError("CalDAV calendar collection was not found")
    if response.status_code in {409, 412}:
        replay = _replay_result_if_equal(
            config, collection_url=collection_url, href=target,
            raw_ical=raw_ical,
        )
        if replay is not None:
            return replay
        raise CalDAVConflict("CalDAV event changed remotely")
    if 300 <= response.status_code < 400:
        raise CalDAVTransportError("CalDAV redirect was refused")
    raise CalDAVTransportError("CalDAV write failed")


def delete_calendar_event(
    config: dict[str, Any],
    *,
    collection_url: str,
    href: str | None,
    etag: str | None,
) -> None:
    """Conditionally delete one exact resource; a genuine 404 is success."""

    if not href or not etag:
        raise CalDAVConflict("Calendar delete is missing its stored ETag")
    target = _same_origin_resource(collection_url, href)
    kwargs = _auth_kwargs(config)
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["If-Match"] = str(etag)
    response = _secure_request("DELETE", target, headers=headers, **kwargs)
    if response.status_code in {200, 202, 204, 404}:
        return
    if response.status_code in {401, 403}:
        raise CalDAVAuthError("CalDAV authentication failed")
    if response.status_code in {409, 412}:
        try:
            get_calendar_resource(
                config, collection_url=collection_url, href=target,
            )
        except CalDAVNotFound:
            return
        raise CalDAVConflict("CalDAV event changed remotely")
    if 300 <= response.status_code < 400:
        raise CalDAVTransportError("CalDAV redirect was refused")
    raise CalDAVTransportError("CalDAV delete failed")


def _stable_cal_id(remote_url: str, owner: str = "", account_id: str = "") -> str:
    # Reuse the sync module's hashing so owner+account_id scoping stays consistent.
    from src.caldav_sync import _stable_cal_id as _sync_id
    return _sync_id(remote_url, owner=owner, account_id=account_id)


def build_event_ical(ev: dict) -> str:
    """Serialize a local event dict to a VCALENDAR/VEVENT iCalendar string.

    ``ev`` keys: uid, summary, description, location, dtstart (datetime),
    dtend (datetime), all_day (bool), is_utc (bool), rrule (str),
    recurrence_exdates (list[str]).
    Mirrors how the pull path interprets is_utc/all_day so a round-trip is stable.
    """
    from icalendar import Calendar, Event as iEvent
    from icalendar.prop import vRecur

    cal = Calendar()
    cal.add("prodid", "-//Restia//CalDAV write-back//EN")
    cal.add("version", "2.0")

    ve = iEvent()
    ve.add("uid", ev["uid"])
    ve.add("summary", ev.get("summary") or "")
    if ev.get("description"):
        ve.add("description", ev["description"])
    if ev.get("location"):
        ve.add("location", ev["location"])

    dtstart = ev["dtstart"]
    dtend = ev["dtend"]
    if ev.get("all_day"):
        ve.add("dtstart", dtstart.date())
        ve.add("dtend", dtend.date())
    elif ev.get("is_utc"):
        # Stored as naive-UTC instants — re-attach UTC so the server gets a Z time.
        ve.add("dtstart", dtstart.replace(tzinfo=timezone.utc))
        ve.add("dtend", dtend.replace(tzinfo=timezone.utc))
    else:
        # Legacy naive-local ("floating") time — emit without a TZ.
        ve.add("dtstart", dtstart)
        ve.add("dtend", dtend)

    if ev.get("rrule"):
        try:
            ve.add("rrule", vRecur.from_ical(ev["rrule"]))
        except Exception:
            logger.debug("CalDAV write-back: skipping unparseable rrule %r", ev.get("rrule"))
    for exdate in ev.get("recurrence_exdates") or []:
        try:
            if ev.get("all_day"):
                ve.add("exdate", datetime.strptime(exdate[:10], "%Y-%m-%d").date())
            else:
                dt = datetime.strptime(exdate[:16], "%Y-%m-%dT%H:%M")
                ve.add("exdate", dt.replace(tzinfo=timezone.utc) if ev.get("is_utc") else dt)
        except Exception:
            logger.debug("CalDAV write-back: skipping unparseable exdate %r", exdate)

    cal.add_component(ve)
    return cal.to_ical().decode("utf-8")


def find_remote_calendar(calendars, local_cal_id: str, owner: str = "", account_id: str = ""):
    """Find the remote calendar whose URL hashes to ``local_cal_id``, or None.

    ``owner`` and ``account_id`` must match what was used when the local calendar
    id was originally computed in ``_sync_blocking`` so the hash round-trips."""
    for cal in calendars:
        try:
            if _stable_cal_id(str(cal.url), owner=owner, account_id=account_id) == local_cal_id:
                return cal
        except Exception:
            continue
    return None


def _resource_href(obj) -> str:
    try:
        return str(getattr(obj, "url", "") or "")
    except Exception:
        return ""


def _resource_etag(obj) -> str:
    try:
        etag = getattr(obj, "etag", None)
        if callable(etag):
            etag = etag()
        return str(etag or "")
    except Exception:
        return ""


def push_event(calendars, local_cal_id: str, ev: dict, *, delete: bool = False,
               owner: str = "", account_id: str = "") -> dict:
    """Create/update (or delete) ``ev`` on the matching remote calendar.

    Returns ``{"ok": bool, ...}``. ``calendars`` is the discovered caldav
    calendar list (injected so this is unit-testable with fakes).
    ``owner`` and ``account_id`` are forwarded to ``find_remote_calendar``
    so the URL hash round-trips correctly (#2765).
    """
    uid = (ev or {}).get("uid") if isinstance(ev, dict) else None
    if not uid:
        return {"ok": False, "error": "event uid is required"}

    remote = find_remote_calendar(calendars, local_cal_id, owner=owner, account_id=account_id)
    if remote is None:
        return {"ok": False, "error": "remote calendar not found"}
    remote_url = str(getattr(remote, "url", "") or "")

    try:
        existing = remote.event_by_uid(uid)
    except Exception:
        existing = None

    if delete:
        if existing is None:
            return {"ok": True, "note": "already absent on remote", "calendar_url": remote_url}
        existing.delete()
        return {
            "ok": True,
            "calendar_url": remote_url,
            "remote_href": _resource_href(existing),
            "remote_etag": _resource_etag(existing),
        }

    ical = build_event_ical(ev)
    if existing is not None:
        existing.data = ical
        existing.save()
        return {
            "ok": True,
            "updated": True,
            "calendar_url": remote_url,
            "remote_href": _resource_href(existing),
            "remote_etag": _resource_etag(existing),
        }
    created = remote.save_event(ical)
    return {
        "ok": True,
        "created": True,
        "calendar_url": remote_url,
        "remote_href": _resource_href(created),
        "remote_etag": _resource_etag(created),
    }


def _discover_calendars(client):
    """Discover the principal's calendars, falling back to the URL itself —
    same strategy as the pull path."""
    from caldav.lib.error import AuthorizationError, NotFoundError
    try:
        return client.principal().calendars()
    except (AuthorizationError, NotFoundError):
        raise
    except Exception:
        try:
            return [client.calendar(url=str(client.url))]
        except Exception:
            return []


def _writeback_blocking(local_cal_id, ev, delete, url, username, password,
                        owner="", account_id="", oauth_access_token=None) -> dict:
    from src.caldav_sync import _build_dav_client
    # Redirects disabled here too: the write-back path opens its own DAVClient,
    # so it needs the same SSRF-via-redirect protection as the pull path.
    client = _build_dav_client(url, username, password, oauth_access_token)
    calendars = _discover_calendars(client)
    if not calendars:
        return {"ok": False, "error": "no remote calendars discovered"}
    return push_event(calendars, local_cal_id, ev, delete=delete,
                      owner=owner, account_id=account_id)


async def writeback_event(owner: str, calendar_source: str, calendar_id: str,
                          ev: dict, *, delete: bool = False) -> dict:
    """Fail closed: immediate writes were replaced by CalendarDelivery."""

    if calendar_source != "caldav":
        return {"skipped": "not a caldav calendar"}
    return {
        "ok": False,
        "error": (
            "Direct CalDAV write-back is retired; enqueue CalendarDelivery"
        ),
    }
