"""Unit tests for the books ETL pipeline."""

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import requests

from books_etl import (
    APIFetchError,
    clean_record,
    display_books,
    fetch_books,
    init_db,
    insert_books,
    main,
    parse_args,
)


class TestCleanRecord(unittest.TestCase):
    def test_valid_minimal_record(self) -> None:
        raw = {"key": "OL1W", "title": "Test Book"}
        cleaned = clean_record(raw)
        self.assertEqual(cleaned, {"ol_key": "OL1W", "title": "Test Book", "author": None, "publication_year": None})

    def test_missing_mandatory_fields_returns_none(self) -> None:
        self.assertIsNone(clean_record({"title": "No Key"}))
        self.assertIsNone(clean_record({"key": "OL1W"}))
        self.assertIsNone(clean_record({}))
        self.assertIsNone(clean_record(None))

    def test_empty_or_whitespace_mandatory_fields_returns_none(self) -> None:
        self.assertIsNone(clean_record({"key": "  ", "title": "Test"}))
        self.assertIsNone(clean_record({"key": "OL1W", "title": ""}))

    def test_authors_list_normalization(self) -> None:
        raw = {"key": "OL1", "title": "T", "author_name": ["Author 1", "  Author 2  ", "", None]}
        self.assertEqual(clean_record(raw)["author"], "Author 1, Author 2, None")

    def test_authors_string_normalization(self) -> None:
        raw = {"key": "OL1", "title": "T", "author_name": "  Single Author  "}
        self.assertEqual(clean_record(raw)["author"], "Single Author")

    def test_publication_year_coercion(self) -> None:
        self.assertEqual(clean_record({"key": "OL1", "title": "T", "first_publish_year": "2020"})["publication_year"], 2020)
        self.assertEqual(clean_record({"key": "OL1", "title": "T", "first_publish_year": 2020.0})["publication_year"], 2020)
        self.assertIsNone(clean_record({"key": "OL1", "title": "T", "first_publish_year": "invalid"})["publication_year"])

class TestDatabaseOperations(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_init_db_creates_schema_idempotently(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='books';")
        self.assertIsNotNone(cursor.fetchone())
        init_db(self.conn)

    def test_insert_books_empty_list(self) -> None:
        inserted, skipped = insert_books(self.conn, [])
        self.assertEqual((inserted, skipped), (0, 0))

    def test_insert_books_and_idempotency(self) -> None:
        books = [
            {"ol_key": "OL1W", "title": "Book 1", "author": "A", "publication_year": 2001},
            {"ol_key": "OL2W", "title": "Book 2", "author": "B", "publication_year": 2002},
        ]
        inserted, skipped = insert_books(self.conn, books)
        self.assertEqual((inserted, skipped), (2, 0))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM books;").fetchone()[0], 2)

        inserted_2, skipped_2 = insert_books(self.conn, books)
        self.assertEqual((inserted_2, skipped_2), (0, 2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM books;").fetchone()[0], 2)

    def test_insert_books_mixed_duplicates(self) -> None:
        insert_books(self.conn, [{"ol_key": "OL1W", "title": "B1", "author": "A1", "publication_year": 2000}])
        mixed = [
            {"ol_key": "OL1W", "title": "B1", "author": "A1", "publication_year": 2000},
            {"ol_key": "OL2W", "title": "B2", "author": "A2", "publication_year": 2005},
        ]
        inserted, skipped = insert_books(self.conn, mixed)
        self.assertEqual((inserted, skipped), (1, 1))

    def test_display_books_runs_cleanly(self) -> None:
        display_books(self.conn)
        insert_books(self.conn, [{"ol_key": "OL1W", "title": "Long "*10, "author": "Name "*10, "publication_year": 2020}])
        display_books(self.conn)

class TestFetchBooks(unittest.TestCase):
    @patch("books_etl.requests.get")
    def test_fetch_books_success(self, mock_get: MagicMock) -> None:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {"docs": [{"key": "OL1W", "title": "T1"}, {"key": "OL2W", "title": "T2"}]}
        docs = fetch_books("python", limit=2)
        self.assertEqual(len(docs), 2)

    @patch("books_etl.requests.get", side_effect=requests.exceptions.Timeout("timed out"))
    def test_fetch_books_timeout(self, mock_get: MagicMock) -> None:
        with self.assertRaises(APIFetchError) as ctx:
            fetch_books("python")
        self.assertIn("API request failed", str(ctx.exception))

    @patch("books_etl.requests.get", side_effect=requests.exceptions.ConnectionError("conn err"))
    def test_fetch_books_connection_error(self, mock_get: MagicMock) -> None:
        with self.assertRaises(APIFetchError) as ctx:
            fetch_books("python")
        self.assertIn("API request failed", str(ctx.exception))

class TestMainFlow(unittest.TestCase):
    @patch("books_etl.fetch_books", side_effect=APIFetchError("Network down"))
    @patch("books_etl.parse_args")
    def test_main_aborts_on_fetch_failure(self, mock_parse: MagicMock, mock_fetch: MagicMock) -> None:
        mock_parse.return_value.query = "python"
        mock_parse.return_value.limit = 5
        mock_parse.return_value.db = ":memory:"
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

    @patch("books_etl.fetch_books")
    @patch("books_etl.parse_args")
    def test_main_connection_closed_after_execution(self, mock_parse: MagicMock, mock_fetch: MagicMock) -> None:
        mock_parse.return_value.query = "python"
        mock_parse.return_value.limit = 5
        mock_parse.return_value.db = ":memory:"
        mock_fetch.return_value = [{"key": "OL1", "title": "T1", "author_name": ["A1"], "first_publish_year": 2020}]

        orig_connect = sqlite3.connect
        cap_conn = None
        def spy_connect(*args, **kwargs):
            nonlocal cap_conn
            cap_conn = orig_connect(*args, **kwargs)
            return cap_conn

        with patch("books_etl.sqlite3.connect", side_effect=spy_connect):
            main()
        with self.assertRaises(sqlite3.ProgrammingError):
            cap_conn.execute("SELECT 1;")

class TestArgParsing(unittest.TestCase):
    def test_default_arguments(self) -> None:
        args = parse_args([])
        self.assertEqual((args.query, args.limit, args.db), ("python", 10, "books.db"))

    def test_custom_arguments(self) -> None:
        args = parse_args(["-q", "algo", "-l", "25", "--db", "custom.db"])
        self.assertEqual((args.query, args.limit, args.db), ("algo", 25, "custom.db"))

    def test_invalid_limit(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["-l", "0"])

    def test_empty_query(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["-q", "   "])

if __name__ == "__main__":
    unittest.main()
