import json
import shutil
import subprocess
from pathlib import Path

import pytest

from routes.email_helpers import (
    EMAIL_CLASSIFICATION_TAGS,
    EMAIL_CONTENT_TAGS,
    EMAIL_VISIBLE_TAGS,
    normalize_email_tags,
)
from routes.email_routes import _sanitize_visible_email_tags


_REPO = Path(__file__).resolve().parents[1]
_TAG_MODULE = _REPO / "static" / "js" / "emailTagTaxonomy.js"
_BUILTIN_ACTIONS = _REPO / "src" / "builtin_actions.py"
_SERVICE_WORKER = _REPO / "static" / "sw.js"
_HAS_NODE = shutil.which("node") is not None

_RESTORED_CONTENT_TAGS = {
    "work", "personal", "finance", "legal", "newsletter", "marketing",
    "notification", "security", "social", "shopping", "support",
}


def test_classifier_and_api_preserve_the_complete_email_tag_taxonomy():
    """Tags accepted from the model must survive the list API sanitizer."""
    assert _RESTORED_CONTENT_TAGS <= EMAIL_CONTENT_TAGS
    assert EMAIL_CLASSIFICATION_TAGS | {"reply-soon"} == EMAIL_VISIBLE_TAGS

    model_output = [
        " Newsletter ", "promo", "work", "notification", "security",
        "social", "shopping", "legal", "support", "unknown", "newsletter",
    ]
    classified = normalize_email_tags(model_output, allowed_tags=EMAIL_CONTENT_TAGS)

    assert classified == [
        "newsletter", "marketing", "work", "notification", "security",
        "social", "shopping", "legal", "support",
    ]
    assert _sanitize_visible_email_tags(classified) == classified


def test_email_tag_normalization_keeps_existing_response_semantics():
    tags = ["URGENT", "reply_soon", "action_needed", "receipt", "travel", "receipt"]

    assert _sanitize_visible_email_tags(tags) == [
        "urgent", "reply-soon", "action-needed", "receipt", "travel",
    ]
    assert _sanitize_visible_email_tags(tags, is_answered=True) == ["receipt", "travel"]
    assert normalize_email_tags("newsletter", allowed_tags=EMAIL_CONTENT_TAGS, limit=1) == ["newsletter"]
    assert normalize_email_tags(tags, limit=0) == []


def test_email_tags_task_invalidates_stale_classification_caches():
    source = _BUILTIN_ACTIONS.read_text(encoding="utf-8")

    assert "TRIAGE_VERSION = 13" in source
    assert "CATEGORY_TAGS = EMAIL_CONTENT_TAGS" in source
    assert "allowed_tags=CATEGORY_TAGS" in source


def test_email_tag_taxonomy_is_available_offline_after_release():
    source = _SERVICE_WORKER.read_text(encoding="utf-8")

    assert "const CACHE_NAME = 'restia-v371'" in source
    assert "'/static/js/emailLibrary.js'" in source
    assert "'/static/js/emailTagTaxonomy.js'" in source


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_email_filter_ui_exposes_every_api_tag_from_one_frontend_taxonomy():
    script = f"""
      const mod = await import('{_TAG_MODULE.as_uri()}');
      console.log(JSON.stringify(mod.EMAIL_FILTERABLE_TAGS));
    """
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr

    frontend_tags = json.loads(proc.stdout)
    assert len(frontend_tags) == len(set(frontend_tags))
    assert set(frontend_tags) == set(EMAIL_VISIBLE_TAGS) | {"spam"}
