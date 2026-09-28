"""Redaction: raw tokens never reach logs, exceptions or terminal text."""

from __future__ import annotations

import logging

from Shared.redaction import (
    RedactingFilter,
    get_logger,
    redact_text,
    safe_format_exception,
)
from Tests.conftest import make_fake_token


def test_redact_text_removes_token_and_secret() -> None:
    fake = make_fake_token("redact")
    text = f"login failed for {fake} with bot_token={fake} and webhook_secret=hunter2abc"
    cleaned = redact_text(text)
    assert fake not in cleaned
    assert "hunter2abc" not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_is_idempotent() -> None:
    fake = make_fake_token("idem")
    once = redact_text(f"boom {fake}")
    assert redact_text(once) == once
    assert fake not in once


def test_filter_mutates_record() -> None:
    fake = make_fake_token("filter")
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "token %s", (fake,), None)
    assert RedactingFilter().filter(record) is True
    assert fake not in str(record.args)


def test_safe_exception_hides_token() -> None:
    fake = make_fake_token("exc")
    try:
        raise ConnectionError(f"send to {fake} failed")
    except ConnectionError as exc:
        summary = safe_format_exception(exc)
    assert fake not in summary
    assert "ConnectionError" in summary


def test_logger_output_has_no_raw_token(caplog) -> None:
    fake = make_fake_token("caplog")
    logger = logging.getLogger("whitelabel.test.redaction")
    logger.addFilter(RedactingFilter())
    logger.setLevel(logging.INFO)
    with caplog.at_level(logging.INFO, logger="whitelabel.test.redaction"):
        logger.warning("polling failed for %s", fake)
    assert fake not in caplog.text
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.filters = [item for item in logger.filters if not isinstance(item, RedactingFilter)]


def test_module_logger_carries_filter() -> None:
    logger = get_logger("whitelabel.test.modlogger")
    assert any(isinstance(item, RedactingFilter) for item in logger.filters)
