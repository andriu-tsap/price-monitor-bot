import sqlite3
from pathlib import Path
from monitor import Book, parse, process

def test_fixture():
    books, nxt = parse(Path("tests/fixture.html").read_text(), "https://books.toscrape.com/")
    assert books == [Book("https://books.toscrape.com/sample.html", "Sample Book", 1890)]
    assert nxt is None

def test_history_and_alerts():
    with sqlite3.connect(":memory:") as db:
        for pence in [2100, 1900, 1900, 1700, 2300]:
            process([Book("https://example.test/book", "Sample Book", pence)], db, "SAMPLE", 2000)
        assert db.execute("SELECT pence FROM history").fetchall() == [(2100,), (1900,), (1700,), (2300,)]
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 2
