from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_profile_settings_exposes_device_management_and_security_posture():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")

    assert 'id="settings-device-sessions"' in html
    assert 'id="settings-security-posture"' in html
    assert 'id="settings-security-export"' in html
    assert 'id="settings-security-copy-backup"' in html
    assert "fetch('/api/auth/sessions'" in script
    assert "fetch('/api/life/security-posture'" in script
    assert "fetch('/api/life/privacy-export'" in script
    assert "method: 'DELETE'" in script
    assert 'data-connector-method="${method}"' in script
    assert 'id="uf-api-paths"' in script
    assert "require_action_approval_for_writes: true" in script
