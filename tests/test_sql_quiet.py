"""aisquare.pipe.core.sql: quiet_sqlglot() and the sqlglot version floor."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import textwrap
import threading

import pytest

from aisquare.pipe.core.sql import (
    SQLGLOT_LOGGER,
    quiet_sqlglot,
    sqlglot_at_least,
    sqlglot_version,
)

SECRET = "member.ssn-123-45-6789"


@pytest.fixture
def sqlglot_records(caplog):
    """Every record logged through "sqlglot" (and a child), at DEBUG and up:
    through caplog's root handler and through a handler on the logger."""
    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    keep = Keep(level=logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=SQLGLOT_LOGGER)
    logger = logging.getLogger(SQLGLOT_LOGGER)
    logger.addHandler(keep)
    try:
        yield records
    finally:
        logger.removeHandler(keep)


def _log_everything(text: str) -> None:
    logger = logging.getLogger(SQLGLOT_LOGGER)
    for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
        logger.log(level, "Invalid JSON path syntax. %s", text)


def test_nothing_sqlglot_logs_inside_gets_out_at_any_level(caplog, sqlglot_records):
    child = logging.getLogger(SQLGLOT_LOGGER + ".optimizer")
    with quiet_sqlglot():
        _log_everything(SECRET)
        child.warning(SECRET)
    assert sqlglot_records == []
    assert SECRET not in caplog.text


def test_every_logger_sqlglot_makes_is_covered():
    """The filter goes on each "sqlglot" logger that exists when a block
    starts. sqlglot makes its loggers when its modules are imported: import
    every one the guards reach, and each must carry the filter."""
    import importlib
    import pkgutil

    sqlglot = pytest.importorskip("sqlglot")
    for package in ("sqlglot", "sqlglot.dialects", "sqlglot.optimizer"):
        module = importlib.import_module(package)
        for info in pkgutil.iter_modules(module.__path__, package + "."):
            if info.name.endswith(".__main__"):
                continue  # the CLI: it parses argv on import
            try:
                importlib.import_module(info.name)
            except ImportError:  # an optional extra (e.g. sqlglotrs)
                continue
    with quiet_sqlglot():
        pass
    names = [
        n
        for n in logging.Logger.manager.loggerDict
        if n == SQLGLOT_LOGGER or n.startswith(SQLGLOT_LOGGER + ".")
    ]
    assert SQLGLOT_LOGGER in names, sqlglot.__version__
    for name in names:
        filters = logging.getLogger(name).filters
        assert any(type(f).__name__ == "_DropWhileQuiet" for f in filters), name


def test_sqlglot_logs_as_usual_outside(sqlglot_records):
    with quiet_sqlglot():
        pass
    logging.getLogger(SQLGLOT_LOGGER).warning("after")
    assert [r.getMessage() for r in sqlglot_records] == ["after"]
    assert logging.root.manager.disable == logging.NOTSET
    assert not logging.getLogger(SQLGLOT_LOGGER).disabled


def test_blocks_nest(sqlglot_records):
    with quiet_sqlglot():
        with quiet_sqlglot():
            _log_everything("inner")
        _log_everything("outer")
    _log_everything("out")
    assert {r.getMessage() for r in sqlglot_records} == {
        "Invalid JSON path syntax. out"
    }


def test_other_threads_logging_meanwhile_are_untouched(sqlglot_records):
    inside = threading.Event()
    done = threading.Event()

    def other() -> None:
        inside.wait(5)
        logging.getLogger(SQLGLOT_LOGGER).warning("other thread")
        done.set()

    thread = threading.Thread(target=other)
    thread.start()
    with quiet_sqlglot():
        inside.set()
        done.wait(5)
        _log_everything(SECRET)
    thread.join(5)
    assert [r.getMessage() for r in sqlglot_records] == ["other thread"]


def test_other_asyncio_tasks_are_untouched(sqlglot_records):
    async def quiet() -> None:
        with quiet_sqlglot():
            await asyncio.sleep(0.01)
            _log_everything(SECRET)

    async def loud() -> None:
        await asyncio.sleep(0)
        logging.getLogger(SQLGLOT_LOGGER).warning("loud task")

    async def both() -> None:
        await asyncio.gather(quiet(), loud())

    asyncio.run(both())
    assert [r.getMessage() for r in sqlglot_records] == ["loud task"]


def test_a_host_that_clears_the_filters_does_not_bring_the_leak_back(sqlglot_records):
    logging.getLogger(SQLGLOT_LOGGER).filters.clear()
    with quiet_sqlglot():
        _log_everything(SECRET)
    assert sqlglot_records == []


def test_a_child_logger_made_later_is_covered_too(sqlglot_records):
    child = logging.getLogger(SQLGLOT_LOGGER + ".made_later_for_this_test")
    with quiet_sqlglot():
        child.error(SECRET)
    assert sqlglot_records == []


def test_python_last_resort_prints_nothing():
    """With no logging configured at all, Python prints WARNING records to
    stderr (logging.lastResort). Nothing logged inside gets there."""
    script = textwrap.dedent(
        f"""
        import logging
        from aisquare.pipe.core.sql import quiet_sqlglot
        with quiet_sqlglot():
            logging.getLogger("sqlglot").warning("inside {SECRET}")
            logging.getLogger("sqlglot").error("inside {SECRET}")
        logging.getLogger("sqlglot").warning("outside")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert SECRET not in proc.stderr
    assert "outside" in proc.stderr  # the control: lastResort does print


def test_real_sqlglot_json_path_warning_is_dropped(sqlglot_records):
    sqlglot = pytest.importorskip("sqlglot")
    sql = f"SELECT JSON_EXTRACT(p, '{SECRET}') FROM claims"
    with quiet_sqlglot():
        sqlglot.parse_one(sql, read="bigquery")
    assert sqlglot_records == []
    sqlglot.parse_one(sql, read="bigquery")  # the control: it does log
    assert any(SECRET in r.getMessage() for r in sqlglot_records)


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("27.29.0", (27, 29, 0)),
        ("26.25.0", (26, 25, 0)),
        ("26.25.0.dev3", (26, 25, 0)),
        ("26.25rc1", (26, 25)),
        ("", ()),
        ("unknown", ()),
    ],
)
def test_sqlglot_version(text, parsed):
    assert sqlglot_version(text) == parsed


@pytest.mark.parametrize(
    ("installed", "ok"),
    [
        ("26.25.0", True),
        ("26.25", True),
        ("27.0.1", True),
        ("26.24.9", False),
        ("25.34.0", False),
        ("", False),
        ("x.y", False),
    ],
)
def test_sqlglot_at_least(installed, ok):
    assert sqlglot_at_least(installed, (26, 25)) is ok
