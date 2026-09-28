"""Migration splitter edge cases: quoted semicolons, triggers, block comments."""

from __future__ import annotations

from Database.migrate import MigrationError, apply_migration, split_statements

# --- split_statements atomic tests ---


def test_split_basic() -> None:
    stmts = split_statements("CREATE TABLE a (id INTEGER); CREATE TABLE b (id INTEGER);")
    assert len(stmts) == 2


def test_split_semicolon_in_single_quoted_value() -> None:
    sql = "INSERT INTO example(value) VALUES ('a;b');\nCREATE TABLE t (id INTEGER);"
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert "VALUES" in stmts[0] and stmts[0].count("VALUES") == 1
    assert "CREATE TABLE t" in stmts[1]


def test_split_semicolon_in_double_quoted_identifier() -> None:
    sql = 'SELECT * FROM "my;table"; DROP TABLE x;'
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert '"my;table"' in stmts[0]


def test_split_escaped_quote_with_semicolon() -> None:
    sql = "INSERT INTO t(v) VALUES ('it''s;a'); DELETE FROM t;"
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert "it''s;a" in stmts[0]
    assert "DELETE" in stmts[1]


def test_split_line_comment_with_semicolon() -> None:
    sql = "-- header; note; semicolons\nCREATE TABLE a (id INTEGER);"
    stmts = split_statements(sql)
    assert len(stmts) == 1
    assert "CREATE TABLE a" in stmts[0]


def test_split_block_comment_with_semicolon() -> None:
    sql = "/* block ; comment ; */ CREATE TABLE b (id INTEGER);"
    stmts = split_statements(sql)
    assert len(stmts) == 1
    assert "CREATE" in stmts[0]


def test_split_multiline_sql() -> None:
    sql = "CREATE TABLE c (\n  id INTEGER\n);\nCREATE TABLE d (\n  val TEXT\n);"
    stmts = split_statements(sql)
    assert len(stmts) == 2


def test_split_trigger_with_statements() -> None:
    sql = (
        "CREATE TABLE log (id INTEGER, msg TEXT);\n"
        "CREATE TRIGGER trg AFTER INSERT ON a\n"
        "BEGIN\n"
        "INSERT INTO log (id, msg) VALUES (NEW.id, 'triggered');\n"
        "END;"
    )
    stmts = split_statements(sql)
    assert len(stmts) == 2
    assert "TRIGGER" in stmts[1]


def test_migration_with_quoted_semicolons_applies(conn, tmp_path, factories) -> None:
    """A valid migration containing VALUES ('a;b') must not break."""
    d = tmp_path / "mig"
    d.mkdir()
    path = d / "0001_qsm.sql"
    path.write_text(
        "CREATE TABLE qsm_t (v TEXT);\n"
        "INSERT INTO qsm_t(v) VALUES ('a;b');\n",
        encoding="utf-8",
    )
    assert apply_migration(conn, path) == "0001_qsm"
    row = conn.execute("SELECT v FROM qsm_t WHERE v='a;b'").fetchone()
    assert str(row["v"]) == "a;b"


def test_broken_migration_rolls_back_fully(tmp_path) -> None:
    """A syntactically broken migration must not leave any table or version row."""
    from Database.connection import connect
    from Database.migrate import migrate

    db = tmp_path / "brk.db"
    d = tmp_path / "bad_mig"
    d.mkdir()
    (d / "0001_good.sql").write_text("CREATE TABLE good_t (id INTEGER);", encoding="utf-8")
    migrate(str(db), d)
    (d / "0002_bad.sql").write_text(
        "CREATE TABLE bad_t (id INTEGER);\nTHIS IS NOT SQL;\n", encoding="utf-8",
    )
    try:
        migrate(str(db), d)
        assert False, "expected MigrationError"
    except MigrationError:
        pass
    conn = connect(str(db))
    try:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        versions = {
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations")
        }
    finally:
        conn.close()
    assert "good_t" in tables  # first migration applied
    assert "bad_t" not in tables  # second migration fully rolled back
    assert "0002_bad" not in versions