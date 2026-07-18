"""Contact import payload normalization rejects non-vCard values cleanly."""

from routes.contacts_routes import _normalize_import_payload


def test_non_string_vcf_degrades_cleanly():
    _text, _csv, error = _normalize_import_payload({"vcf": 123})
    assert error == "No vCard data found"


def test_non_string_csv_degrades_cleanly():
    _text, _csv, error = _normalize_import_payload({"csv": ["a", "b"]})
    assert error is None


def test_empty_body_reports_no_data():
    _text, _csv, error = _normalize_import_payload({})
    assert error == "No contact data found"
