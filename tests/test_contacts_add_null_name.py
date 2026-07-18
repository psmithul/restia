"""Contact add payload normalization remains null-safe."""

from routes.contacts_routes import _normalize_add_payload


def test_null_name_does_not_crash():
    payload, error = _normalize_add_payload({"name": None, "email": "x@y.com"})
    assert error is None
    assert payload == {
        "name": "x", "email": "x@y.com", "phones": [], "address": "",
    }


def test_null_email_does_not_crash():
    payload, error = _normalize_add_payload({"name": "Bob", "email": None})
    assert error is None
    assert payload == {
        "name": "Bob", "email": "", "phones": [], "address": "",
    }


def test_phone_only_contact_is_allowed():
    payload, error = _normalize_add_payload({
        "name": "Bob", "email": None, "phone": "0805412 7841",
    })
    assert error is None
    assert payload["phones"] == ["0805412 7841"]
