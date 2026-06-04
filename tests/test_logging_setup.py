"""Tests for structured logging configuration (Phase 3 slice 1)."""

from __future__ import annotations

import json
import logging
import sys

import logging_setup


def test_json_formatter_emits_envelope_and_extra_fields():
    rec = logging.LogRecord(
        name="tidalhires.worker",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="grab %s done",
        args=("abc",),
        exc_info=None,
    )
    rec.jid = "abc12345"
    rec.event = "import_complete"
    out = json.loads(logging_setup.JsonFormatter().format(rec))
    assert out["level"] == "INFO"
    assert out["component"] == "tidalhires.worker"
    assert out["message"] == "grab abc done"
    assert out["jid"] == "abc12345"
    assert out["event"] == "import_complete"
    assert "ts" in out


def test_json_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    out = json.loads(logging_setup.JsonFormatter().format(rec))
    assert "exc" in out
    assert "boom" in out["exc"]


def _with_restored_root(fn):
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        fn(root)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_defaults_to_text(monkeypatch):
    monkeypatch.delenv("MINTARR_LOG_FORMAT", raising=False)

    def check(root):
        logging_setup.configure_logging()
        assert not isinstance(root.handlers[0].formatter, logging_setup.JsonFormatter)

    _with_restored_root(check)


def test_configure_logging_json_when_requested(monkeypatch):
    monkeypatch.setenv("MINTARR_LOG_FORMAT", "json")

    def check(root):
        logging_setup.configure_logging()
        assert isinstance(root.handlers[0].formatter, logging_setup.JsonFormatter)

    _with_restored_root(check)


def test_configure_logging_respects_level(monkeypatch):
    monkeypatch.setenv("MINTARR_LOG_LEVEL", "WARNING")

    def check(root):
        logging_setup.configure_logging()
        assert root.level == logging.WARNING

    _with_restored_root(check)
