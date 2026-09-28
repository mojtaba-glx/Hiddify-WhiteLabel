"""Redaction through real handlers: logger.exception, tracebacks, dict/JSON."""

from __future__ import annotations

import io
import json
import logging

from Shared.redaction import (
    RedactingFilter,
    RedactingFormatter,
    configure_safe_logging,
    redact_text,
)
from Tests.conftest import make_fake_token


def _stringio_logger(name: str, buffer: io.StringIO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.filters = []
    logger.setLevel(logging.DEBUG)
    logger.addFilter(RedactingFilter())
    handler = logging.StreamHandler(buffer)
    handler.addFilter(RedactingFilter())
    handler.setFormatter(RedactingFormatter("%(levelname)s - %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def test_logger_exception_hides_token_in_traceback() -> None:
    fake = make_fake_token("traceback")
    buffer = io.StringIO()
    logger = _stringio_logger("whitelabel.test.traceback", buffer)
    try:
        raise RuntimeError(f"polling failed with token {fake}")
    except RuntimeError:
        logger.exception("unhandled failure")
    output = buffer.getvalue()
    assert fake not in output
    assert "Traceback" in output
    assert "[REDACTED]" in output


def test_global_logging_redacts_third_party_http_error() -> None:
    fake = make_fake_token("third-party-http")
    buffer = io.StringIO()
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    httpx_logger = logging.getLogger("httpx")
    old_httpx_level = httpx_logger.level
    old_httpx_propagate = httpx_logger.propagate
    old_httpx_handlers = list(httpx_logger.handlers)
    try:
        root.handlers = [logging.StreamHandler(buffer)]
        httpx_logger.handlers = []
        httpx_logger.propagate = True
        configure_safe_logging(logging.INFO)
        httpx_logger.error("request failed at https://api.telegram.org/bot%s/getMe", fake)
        output = buffer.getvalue()
        assert fake not in output
        assert "[REDACTED]" in output
    finally:
        root.handlers = old_handlers
        root.setLevel(old_level)
        httpx_logger.handlers = old_httpx_handlers
        httpx_logger.setLevel(old_httpx_level)
        httpx_logger.propagate = old_httpx_propagate


def test_exc_info_and_stack_are_redacted() -> None:
    fake = make_fake_token("excinfo")
    buffer = io.StringIO()
    logger = _stringio_logger("whitelabel.test.excinfo", buffer)
    try:
        raise ValueError(f"bad secret {fake}")
    except ValueError:
        logger.error("boom", exc_info=True)
    assert fake not in buffer.getvalue()


def test_dict_repr_and_json_payloads_redacted() -> None:
    fake = make_fake_token("payload")
    raw_dict = repr({"webhook_secret": fake, "password": "hunter2"})
    assert fake not in redact_text(raw_dict)
    assert "hunter2" not in redact_text(raw_dict)
    raw_json = json.dumps({"api_key": fake, "nested": {"encryption_key": "topsecret"}})
    cleaned = redact_text(raw_json)
    assert fake not in cleaned
    assert "topsecret" not in cleaned


def test_logger_dict_args_redacted() -> None:
    fake = make_fake_token("dictargs")
    buffer = io.StringIO()
    logger = _stringio_logger("whitelabel.test.dictargs", buffer)
    logger.warning("payload %(data)s", {"data": repr({"bot_token": fake})})
    assert fake not in buffer.getvalue()
