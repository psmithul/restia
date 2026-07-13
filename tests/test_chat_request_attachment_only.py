"""JSON chat clients may send an attachment without filler text."""

import pytest
from pydantic import ValidationError

from src.request_models import ChatRequest


def test_attachment_only_chat_request_is_valid():
    req = ChatRequest(session="s1", message="  ", attachments=["abc.png"])
    assert req.message == ""
    assert req.attachments == ["abc.png"]


def test_empty_chat_request_is_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(session="s1", message="  ", attachments=[])
