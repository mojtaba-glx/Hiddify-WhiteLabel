"""Redaction for logs, exceptions and terminal output.

Never log raw bot tokens, Fernet payloads or key/value secret content.

Design:
- :class:`RedactingFilter` scrubs the record early (msg/args).
- :class:`RedactingFormatter` scrubs the *final* rendered string, which is
  what actually reaches handlers. This covers ``logger.exception``
  tracebacks, ``exc_info`` blocks and any secret inside ``str(obj)`` /
  ``repr(dict)`` / JSON payloads.
- The core regex handles both single- and double-quoted secret values that
  may contain spaces (``"password": "multi word value"``) by consuming text
  until the terminating quote or a closing bracket/comma/newline.
- ``BOT_TOKEN_RE`` and ``FERNET_RE`` act as catch-all safeguards for any
  remaining location (traceback frames, unmatched boundaries).
"""

from __future__ import annotations

import logging
import re

# Telegram bot token: <bot-id>:<30+ chars>.
BOT_TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_\-]{30,}")
# Fernet ciphertext always starts with gAAAA.
FERNET_RE = re.compile(r"gAAAA[A-Za-z0-9_\-=%]{40,}")

# Match a secret key/value assignment in any common rendering:
#   token=abc   password='multi word value'   "webhook_secret": "secret with spaces"
#   {'password': "it's;complex"}   secret: foo;   api_key=   encryption-key=
# The key fragment is matched case-insensitively. The value is captured
# until the delimiter after the first quote or until space/bracket/comma.
_SECRET_KEY_FRAGMENT = r"(?:token|secret|passwd|password|api[_-]?key|encryption[_-]?key|private[_-]?key)"

# Match: <prefix-with-key> <colon/equals> <optional quote> <value...>
# The value capture group greedily takes everything until the closing
# quote or a boundary char, so multi-word quoted values are fully masked.
_QUOTED_SINGLE = r"'(?:''|\\.|[^'\\])*'"  # SQL '' and backslash escapes
_QUOTED_DOUBLE = r'"(?:""|\\.|[^"\\])*"'  # SQL "" and backslash escapes
_UNQUOTED = r"[^\s\"',}\]]+"

_VAL = r"(?:" + _QUOTED_SINGLE + r"|" + _QUOTED_DOUBLE + r"|" + _UNQUOTED + r")"

SECRET_KV_RE = re.compile(
    r"(?i)([\"']?[\w.\-]*" + _SECRET_KEY_FRAGMENT + r"[\w.\-]*[\"']?\s*[:=]\s*)(" + _VAL + r")",
    re.DOTALL,
)

REDACTED = "[REDACTED]"


def redact_text(text: str) -> str:
    """Redact tokens/secrets from arbitrary text (idempotent)."""
    if not text:
        return text
    redacted = BOT_TOKEN_RE.sub(REDACTED, text)
    redacted = FERNET_RE.sub(REDACTED, redacted)
    return SECRET_KV_RE.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)


class RedactingFilter(logging.Filter):
    """Logging filter that redacts sensitive data from records (early scrub)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_text(record.msg)
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: (redact_text(str(value)) if isinstance(value, str) else value)
                        for key, value in record.args.items()
                    }
                elif isinstance(record.args, (tuple, list)):
                    cleaned = []
                    for value in record.args:
                        cleaned.append(redact_text(value) if isinstance(value, str) else value)
                    record.args = tuple(cleaned) if isinstance(record.args, tuple) else cleaned
        except Exception:
            record.msg = REDACTED
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that redacts the final rendered output."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return redact_text(super().format(record))
        except Exception:
            return REDACTED

    def formatException(self, exc_info) -> str:
        try:
            return redact_text(super().formatException(exc_info))
        except Exception:
            return REDACTED

    def formatStack(self, stack_info) -> str:
        try:
            return redact_text(super().formatStack(stack_info))
        except Exception:
            return REDACTED


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """Module logger with redaction filter + redacting formatter (handler added once)."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    has_filter = any(isinstance(item, RedactingFilter) for item in logger.filters)
    if not has_filter:
        logger.addFilter(RedactingFilter())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(RedactingFilter())
        handler.setFormatter(
            RedactingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
        )
        logger.addHandler(handler)
    logger.propagate = False
    return logger


def configure_safe_logging(level: int = logging.INFO) -> None:
    """Redact final output from root, Telegram and HTTP client loggers.

    Tenant tokens arrive as Telegram message text and are also used in Bot API
    request URLs. Applying the formatter at the process boundary prevents a
    third-party traceback or HTTP log from bypassing module-level filters.
    """
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    targets = (
        root,
        logging.getLogger("telegram"),
        logging.getLogger("httpx"),
        logging.getLogger("httpcore"),
    )
    for target in targets:
        if target is not root:
            target.setLevel(logging.WARNING)
        for handler in target.handlers:
            if not any(isinstance(item, RedactingFilter) for item in handler.filters):
                handler.addFilter(RedactingFilter())
            handler.setFormatter(
                RedactingFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            )


def safe_format_exception(exc: BaseException) -> str:
    """One-line exception summary with secrets redacted (type + cleaned message)."""
    try:
        message = redact_text(str(exc) or "")
    except Exception:
        message = REDACTED
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
