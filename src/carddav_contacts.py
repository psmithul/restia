"""Pure CardDAV/vCard protocol helpers for the contacts domain.

No helper in this module loads global settings, chooses an owner, opens a
database session, or caches results.  Callers must pass an explicit validated
source configuration so one account can never inherit another account's
credentials or stale snapshot.
"""

from __future__ import annotations

import logging
import os
import ipaddress
import re
import socket
import ssl
import uuid
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlunparse

import httpx
import httpcore

from src.url_safety import check_outbound_url


logger = logging.getLogger(__name__)

# CardDAV responses contain private address-book data and are fully buffered by
# the pinned transport.  Bound them before materializing a response so a broken
# or hostile server cannot exhaust the Restia process.
MAX_CARDDAV_RESPONSE_BYTES = 16 * 1024 * 1024

ADDRESSBOOK_QUERY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<C:addressbook-query xmlns:D="DAV:" '
    'xmlns:C="urn:ietf:params:xml:ns:carddav">'
    '<D:prop><D:getetag/><C:address-data/></D:prop>'
    '<C:filter/>'
    '</C:addressbook-query>'
)


class CardDAVError(RuntimeError):
    """Sanitized CardDAV transport/protocol failure."""


class CardDAVConflict(CardDAVError):
    """A remote resource changed after Restia's last observed version."""


class _PinnedSyncBackend(httpcore.NetworkBackend):
    """Connect to one already-vetted IP while retaining hostname TLS/SNI."""

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
                if size > MAX_CARDDAV_RESPONSE_BYTES:
                    raise CardDAVError("CardDAV response exceeded the size limit")
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


def _resolved_ips(host: str) -> list[str]:
    values: list[str] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        value = str(sockaddr[0]).split("%", 1)[0]
        if value not in values:
            values.append(value)
    return values


def _public_http_is_forbidden(url: str, raw_ips: list[str]) -> bool:
    if urlparse(url).scheme.lower() != "http":
        return False
    if os.getenv("CARDDAV_ALLOW_PUBLIC_HTTP", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }:
        return False
    for raw in raw_ips:
        try:
            ip = ipaddress.ip_address(str(raw).split("%", 1)[0])
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not (ip.is_private or ip.is_loopback):
            return True
    return False


def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Resolve, validate, and pin every CardDAV socket destination.

    The short-lived client ignores proxy environment variables and refuses
    redirects, preventing stored Basic credentials from crossing either a
    proxy or an attacker-controlled redirect/rebinding boundary.
    """

    from src.url_safety import check_outbound_url

    parsed = urlparse(validate_carddav_url(url))
    try:
        raw_ips = _resolved_ips(str(parsed.hostname or ""))
    except OSError as exc:
        raise CardDAVError("CardDAV host resolution failed") from exc
    ok, _reason = check_outbound_url(
        url,
        block_private=os.getenv("CARDDAV_BLOCK_PRIVATE_IPS", "false").lower()
        == "true",
        resolver=lambda _host: list(raw_ips),
    )
    if not ok or not raw_ips:
        raise CardDAVError("CardDAV destination is not allowed")
    if _public_http_is_forbidden(url, raw_ips):
        raise CardDAVError("Public CardDAV destinations require HTTPS")
    try:
        ip = ipaddress.ip_address(raw_ips[0])
    except ValueError as exc:
        raise CardDAVError("CardDAV host resolution failed") from exc
    transport = _PinnedSyncTransport(ip)
    with httpx.Client(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        return client.request(method, url, **kwargs)


def validate_carddav_url(url: object) -> str:
    cleaned = (url if isinstance(url, str) else "").strip().rstrip("/")
    if len(cleaned) > 2048:
        raise ValueError("Rejected CardDAV URL: URL is too long")
    parsed = urlparse(cleaned)
    if parsed.username or parsed.password:
        raise ValueError(
            "Rejected CardDAV URL: put credentials in the dedicated fields"
        )
    if parsed.fragment:
        raise ValueError("Rejected CardDAV URL: fragments are not allowed")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Rejected CardDAV URL: invalid port") from exc
    try:
        raw_ips = _resolved_ips(str(parsed.hostname or ""))
    except OSError as exc:
        raise ValueError("Rejected CardDAV URL: host resolution failed") from exc
    ok, reason = check_outbound_url(
        cleaned,
        block_private=os.getenv(
            "CARDDAV_BLOCK_PRIVATE_IPS", "false"
        ).lower() == "true",
        resolver=lambda _host: list(raw_ips),
    )
    if not ok:
        raise ValueError(f"Rejected CardDAV URL: {reason}")
    if _public_http_is_forbidden(cleaned, raw_ips):
        raise ValueError("Rejected CardDAV URL: public endpoints require HTTPS")
    return cleaned


def normalize_config(config: dict[str, Any]) -> dict[str, str]:
    return {
        "url": validate_carddav_url(config.get("url") or ""),
        "username": str(config.get("username") or ""),
        "password": str(config.get("password") or ""),
    }


def _auth(config: dict[str, str]) -> tuple[str, str] | None:
    return (
        (config["username"], config["password"])
        if config.get("username")
        else None
    )


def normalize_contact(contact: dict[str, Any]) -> dict[str, Any]:
    emails: list[str] = []
    raw_emails = contact.get("emails") or (
        [] if not contact.get("email") else [contact.get("email")]
    )
    for raw in raw_emails:
        value = str(raw or "").strip()
        if value and value not in emails:
            emails.append(value)

    phones: list[str] = []
    raw_phones = contact.get("phones") or (
        [] if not contact.get("phone") else [contact.get("phone")]
    )
    for raw in raw_phones:
        value = str(raw or "").strip()
        if value and value not in phones:
            phones.append(value)

    name = str(contact.get("name") or "").strip()
    if not name and emails:
        name = emails[0].split("@", 1)[0]
    return {
        "uid": str(contact.get("uid") or uuid.uuid4()),
        "name": name,
        "emails": emails,
        "phones": phones,
        "address": str(contact.get("address") or "").strip(),
    }


def _vunesc(value: str) -> str:
    if not value:
        return value
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            following = value[index + 1]
            out.append("\n" if following in ("n", "N") else following)
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


def split_vcard_blocks(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[str] = []
    for chunk in re.split(r"(?i)BEGIN:VCARD", normalized):
        if not chunk.strip():
            continue
        end = re.search(r"(?im)^END:VCARD\s*$", chunk)
        if end is None:
            continue
        body = chunk[: end.end()].strip("\n")
        blocks.append("BEGIN:VCARD\r\n" + body.replace("\n", "\r\n") + "\r\n")
    return blocks


def parse_vcards(text: str) -> list[dict[str, Any]]:
    unfolded = re.sub(r"\r\n[ \t]", "", str(text or ""))
    unfolded = re.sub(r"\n[ \t]", "", unfolded)
    contacts: list[dict[str, Any]] = []
    for block in re.split(r"(?i)BEGIN:VCARD", unfolded):
        if not block.strip():
            continue
        contact: dict[str, Any] = {
            "name": "", "emails": [], "phones": [], "uid": "", "address": "",
        }
        for raw_line in block.splitlines():
            line = raw_line.strip()
            property_line = re.sub(r"^[A-Za-z0-9-]+\.", "", line, count=1)
            upper = property_line.upper()
            if upper.startswith("FN:") or upper.startswith("FN;"):
                contact["name"] = (
                    _vunesc(property_line.split(":", 1)[1])
                    if ":" in property_line else ""
                )
            elif upper.startswith("EMAIL") and ":" in property_line:
                email = _vunesc(property_line.split(":", 1)[1])
                if email and email not in contact["emails"]:
                    contact["emails"].append(email)
            elif upper.startswith("TEL") and ":" in property_line:
                phone = _vunesc(property_line.split(":", 1)[1])
                if phone and phone not in contact["phones"]:
                    contact["phones"].append(phone)
            elif upper.startswith("ADR") and ":" in property_line:
                parts = [
                    _vunesc(part).strip()
                    for part in property_line.split(":", 1)[1].split(";")
                ]
                contact["address"] = ", ".join(part for part in parts if part)
            elif upper.startswith("UID:"):
                contact["uid"] = _vunesc(property_line.split(":", 1)[1])
        if any((contact["name"], contact["emails"], contact["phones"], contact["address"])):
            contacts.append(normalize_contact(contact))
    return contacts


def _vesc(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def build_vcard(
    name: str,
    email: str = "",
    uid: str | None = None,
    *,
    emails: list[str] | None = None,
    phones: list[str] | None = None,
    address: str = "",
) -> str:
    contact_uid = uid or str(uuid.uuid4())
    email_values = [
        value.strip()
        for value in (emails if emails is not None else ([email] if email else []))
        if value and value.strip()
    ]
    phone_values = [value.strip() for value in (phones or []) if value and value.strip()]
    name_parts = str(name or "").strip().split()
    first = name_parts[0] if name_parts else ""
    last = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    lines = [
        "BEGIN:VCARD",
        "VERSION:4.0",
        f"UID:{_vesc(contact_uid)}",
        f"FN:{_vesc(name)}",
        f"N:{_vesc(last)};{_vesc(first)};;;",
    ]
    for index, value in enumerate(email_values):
        lines.append(
            f"EMAIL;PREF=1:{_vesc(value)}" if index == 0 else f"EMAIL:{_vesc(value)}"
        )
    lines.extend(f"TEL:{_vesc(value)}" for value in phone_values)
    if str(address or "").strip():
        lines.append(f"ADR:;;{_vesc(address.strip())};;;;")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def absolute_url(base_url: str, href: str) -> str:
    base = validate_carddav_url(base_url)
    base_parts = urlparse(base)
    joined = urljoin(base.rstrip("/") + "/", str(href or ""))
    joined_parts = urlparse(joined)
    if (joined_parts.scheme, joined_parts.netloc) != (
        base_parts.scheme, base_parts.netloc,
    ):
        joined = urlunparse((
            base_parts.scheme,
            base_parts.netloc,
            joined_parts.path or "/",
            "",
            joined_parts.query,
            "",
        ))
    return validate_carddav_url(joined)


def vcard_url(base_url: str, uid: str) -> str:
    return validate_carddav_url(base_url) + "/" + quote(str(uid), safe="") + ".vcf"


def _parsed_card(raw_vcard: str) -> dict[str, Any] | None:
    parsed = parse_vcards(raw_vcard)
    if not parsed:
        return None
    contact = parsed[0]
    contact["raw_vcard"] = raw_vcard
    return contact


def _fetch_via_report(config: dict[str, str]) -> list[dict[str, Any]] | None:
    from defusedxml import ElementTree as ET

    response = _request(
        "REPORT",
        config["url"],
        content=ADDRESSBOOK_QUERY.encode("utf-8"),
        headers={"Content-Type": "application/xml; charset=utf-8", "Depth": "1"},
        auth=_auth(config),
        timeout=10,
    )
    if response.status_code not in (200, 207):
        return None
    try:
        root = ET.fromstring(response.text)
    except Exception as exc:
        raise CardDAVError("CardDAV returned invalid XML") from exc
    namespaces = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:carddav"}
    result: list[dict[str, Any]] = []
    for item in root.findall("D:response", namespaces):
        href = item.find("D:href", namespaces)
        data = item.find(".//C:address-data", namespaces)
        etag = item.find(".//D:getetag", namespaces)
        raw_vcard = str(data.text or "") if data is not None else ""
        contact = _parsed_card(raw_vcard) if raw_vcard.strip() else None
        if contact is None or href is None or not str(href.text or "").strip():
            continue
        contact["href"] = str(href.text).strip()
        contact["etag"] = str(etag.text or "").strip() if etag is not None else ""
        result.append(contact)
    # Some servers accept an empty filter but return no matches. Let GET prove
    # whether the address book is actually empty.
    return result or None


def fetch_contacts(config: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = normalize_config(config)
    try:
        reported = _fetch_via_report(normalized)
        if reported is not None:
            return reported
        response = _request(
            "GET", normalized["url"], auth=_auth(normalized), timeout=10,
        )
        if response.status_code != 200:
            raise CardDAVError(f"CardDAV fetch failed with status {response.status_code}")
        result: list[dict[str, Any]] = []
        for block in split_vcard_blocks(response.text):
            contact = _parsed_card(block)
            if contact is not None:
                result.append(contact)
        return result
    except CardDAVError:
        raise
    except Exception as exc:
        logger.warning("CardDAV contact fetch failed: %s", type(exc).__name__)
        raise CardDAVError("CardDAV contact fetch failed") from exc


def put_contact(
    config: dict[str, Any],
    *,
    uid: str,
    raw_vcard: str,
    href: str | None = None,
    etag: str | None = None,
) -> tuple[str, str | None]:
    normalized = normalize_config(config)
    target = (
        absolute_url(normalized["url"], href)
        if href else vcard_url(normalized["url"], uid)
    )
    headers = {"Content-Type": "text/vcard; charset=utf-8"}
    current_etag = str(etag or "").strip()
    if href:
        if not current_etag:
            _body, current_etag, exists = get_contact_resource(
                normalized, target=target,
            )
            if not exists:
                raise CardDAVConflict("CardDAV contact no longer exists")
            if not current_etag:
                raise CardDAVConflict(
                    "CardDAV server did not provide an update precondition"
                )
        headers["If-Match"] = current_etag
    else:
        headers["If-None-Match"] = "*"
    try:
        response = _request(
            "PUT", target,
            data=raw_vcard.encode("utf-8"),
            headers=headers,
            auth=_auth(normalized),
            timeout=15,
        )
    except Exception as exc:
        raise CardDAVError("CardDAV contact write failed") from exc
    if response.status_code in (409, 412):
        current_body, observed_etag, exists = get_contact_resource(
            normalized, target=target,
        )
        if exists and _same_vcard(current_body, raw_vcard):
            return target, observed_etag
        raise CardDAVConflict("CardDAV contact changed remotely")
    if response.status_code not in (200, 201, 204):
        raise CardDAVError(f"CardDAV contact write failed with status {response.status_code}")
    observed_etag = response.headers.get("etag")
    if not observed_etag:
        _body, observed_etag, _exists = get_contact_resource(
            normalized, target=target,
        )
    return target, observed_etag


def delete_contact(
    config: dict[str, Any], *, uid: str, href: str | None = None,
    etag: str | None = None,
) -> None:
    normalized = normalize_config(config)
    target = (
        absolute_url(normalized["url"], href)
        if href else vcard_url(normalized["url"], uid)
    )
    current_etag = str(etag or "").strip()
    if not current_etag:
        _body, current_etag, exists = get_contact_resource(
            normalized, target=target,
        )
        if not exists:
            return
        if not current_etag:
            raise CardDAVConflict(
                "CardDAV server did not provide a delete precondition"
            )
    try:
        response = _request(
            "DELETE", target,
            headers={"If-Match": current_etag},
            auth=_auth(normalized),
            timeout=10,
        )
    except Exception as exc:
        raise CardDAVError("CardDAV contact delete failed") from exc
    if response.status_code in (409, 412):
        _body, _etag, exists = get_contact_resource(normalized, target=target)
        if not exists:
            return
        raise CardDAVConflict("CardDAV contact changed remotely")
    if response.status_code not in (200, 204, 404):
        raise CardDAVError(f"CardDAV contact delete failed with status {response.status_code}")


def _same_vcard(left: object, right: object) -> bool:
    def normalized(value: object) -> str:
        return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    if normalized(left) == normalized(right):
        return True
    parsed_left = parse_vcards(str(left or ""))
    parsed_right = parse_vcards(str(right or ""))
    if len(parsed_left) != 1 or len(parsed_right) != 1:
        return False
    left_card, right_card = parsed_left[0], parsed_right[0]
    return (
        all(
            left_card.get(field) == right_card.get(field)
            for field in ("uid", "name", "address")
        )
        and set(left_card.get("emails") or []) == set(right_card.get("emails") or [])
        and set(left_card.get("phones") or []) == set(right_card.get("phones") or [])
    )


def get_contact_resource(
    config: dict[str, Any], *, target: str,
) -> tuple[str, str | None, bool]:
    """Read one exact resource for ETag preconditions and crash recovery."""

    normalized = normalize_config(config)
    safe_target = absolute_url(normalized["url"], target)
    try:
        response = _request(
            "GET", safe_target, auth=_auth(normalized), timeout=10,
        )
    except Exception as exc:
        raise CardDAVError("CardDAV contact precondition read failed") from exc
    if response.status_code == 404:
        return "", None, False
    if response.status_code != 200:
        raise CardDAVError(
            f"CardDAV contact precondition read failed with status {response.status_code}"
        )
    return response.text, response.headers.get("etag"), True


def prepare_import_cards(text: str) -> list[tuple[dict[str, Any], str]]:
    result: list[tuple[dict[str, Any], str]] = []
    for raw_block in split_vcard_blocks(text):
        normalized = raw_block.replace("\r\n", "\n").rstrip("\n")
        uid_match = re.search(r"(?im)^UID:(.+)$", normalized)
        uid = str(uid_match.group(1)).strip() if uid_match else str(uuid.uuid4())
        if not re.search(r"(?im)^VERSION:", normalized):
            normalized = normalized.replace(
                "BEGIN:VCARD", "BEGIN:VCARD\nVERSION:4.0", 1
            )
        if uid_match is None:
            version_match = re.search(r"(?im)^VERSION:.*$", normalized)
            if version_match is not None:
                insert_at = version_match.end()
                normalized = normalized[:insert_at] + f"\nUID:{uid}" + normalized[insert_at:]
            else:
                normalized = normalized.replace(
                    "BEGIN:VCARD", f"BEGIN:VCARD\nVERSION:4.0\nUID:{uid}", 1
                )
        raw_vcard = normalized.replace("\n", "\r\n") + "\r\n"
        contact = _parsed_card(raw_vcard)
        if contact is not None:
            result.append((contact, raw_vcard))
    return result


__all__ = [
    "CardDAVConflict",
    "CardDAVError",
    "absolute_url",
    "build_vcard",
    "delete_contact",
    "fetch_contacts",
    "get_contact_resource",
    "normalize_config",
    "normalize_contact",
    "parse_vcards",
    "prepare_import_cards",
    "put_contact",
    "split_vcard_blocks",
    "validate_carddav_url",
    "vcard_url",
]
