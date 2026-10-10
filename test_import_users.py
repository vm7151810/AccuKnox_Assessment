"""Unit tests for the CSV user import pipeline."""

import codecs
from contextlib import closing
import csv
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from import_users import (
    CSVFileNotFoundError,
    CSVImportError,
    HeaderValidationError,
    ImportSummary,
    display_users,
    init_db,
    inspect_csv_file,
    import_csv,
    insert_user,
    main,
    normalize_email,
    normalize_text,
    parse_args,
    validate_headers,
    validate_user,
)

class TestHeaderValidation(unittest.TestCase):
    def test_valid_exact_headers(self) -> None:
        mapping = validate_headers(["name", "email"])
        self.assertEqual(mapping["name"], "name")
        self.assertEqual(mapping["email"], "email")

    def test_case_insensitive_and_whitespace_tolerant(self) -> None:
        mapping = validate_headers(["  Name  ", " EMAIL "])
        self.assertEqual(mapping["name"], "  Name  ")
        self.assertEqual(mapping["email"], " EMAIL ")

    def test_extra_columns_allowed(self) -> None:
        mapping = validate_headers(["id", "Name", "age", "Email", "notes"])
        self.assertEqual(mapping["name"], "Name")
        self.assertEqual(mapping["email"], "Email")

    def test_missing_column_raises_error(self) -> None:
        with self.assertRaises(HeaderValidationError) as ctx:
            validate_headers(["name", "phone"])
        self.assertIn("missing required column(s): email", str(ctx.exception))

        with self.assertRaises(HeaderValidationError) as ctx:
            validate_headers(["email"])
        self.assertIn("missing required column(s): name", str(ctx.exception))

    def test_none_or_empty_headers_raise_error(self) -> None:
        with self.assertRaises(HeaderValidationError):
            validate_headers(None)
        with self.assertRaises(HeaderValidationError):
            validate_headers([])

class TestNormalization(unittest.TestCase):
    def test_normalize_text(self) -> None:
        self.assertEqual(normalize_text("  Alice Smith  "), "Alice Smith")
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text(None), "")
        self.assertEqual(normalize_text("   "), "")

    def test_normalize_email(self) -> None:
        self.assertEqual(normalize_email("  ALICE@EXAMPLE.COM  "), "alice@example.com")
        self.assertEqual(normalize_email("Bob.Jones@Domain.org"), "bob.jones@domain.org")
        self.assertEqual(normalize_email(None), "")
        self.assertEqual(normalize_email("   "), "")

class TestUserValidation(unittest.TestCase):
    def test_valid_user(self) -> None:
        valid, reason = validate_user("Alice Smith", "alice.smith@example.com")
        self.assertTrue(valid)
        self.assertIsNone(reason)

    def test_empty_or_whitespace_name(self) -> None:
        valid, reason = validate_user("", "alice@example.com")
        self.assertFalse(valid)
        self.assertEqual(reason, "Missing or empty name")

    def test_empty_or_whitespace_email(self) -> None:
        valid, reason = validate_user("Alice Smith", "")
        self.assertFalse(valid)
        self.assertEqual(reason, "Missing or empty email")

    def test_invalid_email_formats(self) -> None:
        invalid_emails = [
            "not-an-email",
            "alice@",
            "@example.com",
            "alice @example.com",
            "alice@example",
            "alice.example.com",
        ]
        for email in invalid_emails:
            with self.subTest(email=email):
                valid, reason = validate_user("Alice", email)
                self.assertFalse(valid)
                self.assertEqual(reason, "Invalid email format")

class TestDatabaseOperations(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_schema_created(self) -> None:
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users';")
        self.assertIsNotNone(cursor.fetchone())

    def test_schema_defenses_check_name(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute("INSERT INTO users (name, email) VALUES ('   ', 'test@example.com');")

    def test_schema_defenses_nocase_email(self) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO users (name, email) VALUES ('User 1', 'test@example.com');")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute("INSERT INTO users (name, email) VALUES ('User 2', 'TEST@EXAMPLE.COM');")

    def test_insert_user_success(self) -> None:
        cursor = self.conn.cursor()
        success = insert_user(cursor, "Alice", "alice@example.com")
        self.assertTrue(success)
        cursor.execute("SELECT name, email FROM users WHERE email='alice@example.com';")
        self.assertEqual(cursor.fetchone(), ("Alice", "alice@example.com"))

    def test_insert_user_duplicate_email(self) -> None:
        cursor = self.conn.cursor()
        self.assertTrue(insert_user(cursor, "Alice", "alice@example.com"))
        self.assertFalse(insert_user(cursor, "Alice Duplicate", "alice@example.com"))
        cursor.execute("SELECT COUNT(*) FROM users WHERE email='alice@example.com';")
        self.assertEqual(cursor.fetchone()[0], 1)

class TestImportCSV(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")
        self.conn = sqlite3.connect(self.db_path)
        init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.temp_dir.cleanup()

    def test_file_not_found(self) -> None:
        with self.assertRaises(CSVFileNotFoundError):
            import_csv(os.path.join(self.temp_dir.name, "nonexistent.csv"), self.conn)

    def test_bad_header_raises_error(self) -> None:
        bad_csv = os.path.join(self.temp_dir.name, "bad_header.csv")
        with open(bad_csv, "w", encoding="utf-8") as f:
            f.write("wrong,columns\nvalue1,value2\n")
        with self.assertRaises(HeaderValidationError):
            import_csv(bad_csv, self.conn)

    def test_empty_csv_raises_error(self) -> None:
        empty_csv = os.path.join(self.temp_dir.name, "empty.csv")
        with open(empty_csv, "w", encoding="utf-8") as f:
            pass
        with self.assertRaises(HeaderValidationError):
            import_csv(empty_csv, self.conn)

    def test_utf8_bom_handling(self) -> None:
        bom_csv = os.path.join(self.temp_dir.name, "bom_users.csv")
        with open(bom_csv, "wb") as f:
            f.write(codecs.BOM_UTF8)
            f.write(b"name,email\nAlice Smith,alice@example.com\n")
        
        summary = import_csv(bom_csv, self.conn)
        self.assertEqual(summary.rows_read, 1)
        self.assertEqual(summary.inserted, 1)
        
        cursor = self.conn.cursor()
        cursor.execute("SELECT name, email FROM users;")
        self.assertEqual(cursor.fetchone(), ("Alice Smith", "alice@example.com"))

    def test_mid_file_unicode_decode_error_rolls_back_atomically(self) -> None:
        bad_encoding_csv = os.path.join(self.temp_dir.name, "latin1.csv")
        with open(bad_encoding_csv, "wb") as f:
            f.write(b"name,email\n")
            f.write(b"Alice Smith,alice@example.com\n")
            f.write(b"Jos\xe9,jose@example.com\n")

        with self.assertRaises(CSVImportError) as ctx:
            import_csv(bad_encoding_csv, self.conn)
        self.assertIn("Failed to read CSV file", str(ctx.exception))

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users;")
        self.assertEqual(cursor.fetchone()[0], 0)

    def test_csv_error_rolls_back_atomically(self) -> None:
        large_field_csv = os.path.join(self.temp_dir.name, "large_field.csv")
        with open(large_field_csv, "w", encoding="utf-8") as f:
            f.write("name,email\n")
            f.write("Alice Smith,alice@example.com\n")
            f.write("A" * 100 + ",large@example.com\n")

        old_limit = csv.field_size_limit(50)
        try:
            with self.assertRaises(CSVImportError) as ctx:
                import_csv(large_field_csv, self.conn)
            self.assertIn("Failed to read CSV file", str(ctx.exception))
        finally:
            csv.field_size_limit(old_limit)

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users;")
        self.assertEqual(cursor.fetchone()[0], 0)

    def test_import_with_anomalies_and_accounting_consistency(self) -> None:
        csv_path = os.path.join(self.temp_dir.name, "sample.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(
                "Name, Email\n"
                "Alice Smith, alice@example.com\n"
                "Bob Jones, bob@example.com\n"
                "  Bob Jones  ,  BOB@EXAMPLE.COM  \n"
                ", charlie@example.com\n"
                "David Miller, not-an-email\n"
                "Eve Adams, eve@example.com\n"
            )

        summary = import_csv(csv_path, self.conn)
        self.assertEqual(summary.rows_read, 6)
        self.assertEqual(summary.inserted, 3)
        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(summary.invalid, 2)
        self.assertTrue(summary.is_consistent)

        cursor = self.conn.cursor()
        cursor.execute("SELECT email FROM users ORDER BY id ASC;")
        self.assertEqual(
            [r[0] for r in cursor.fetchall()],
            ["alice@example.com", "bob@example.com", "eve@example.com"],
        )

    def test_duplicate_handling_in_file_and_in_db(self) -> None:
        insert_user(self.conn.cursor(), "Existing Bob", "bob@example.com")
        self.conn.commit()

        csv_path = os.path.join(self.temp_dir.name, "dup_test.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(
                "name,email\n"
                "Alice,alice@example.com\n"
                "Alice Repeat,alice@example.com\n"
                "Bob,bob@example.com\n"
            )

        summary = import_csv(csv_path, self.conn)
        self.assertEqual(summary.rows_read, 3)
        self.assertEqual(summary.inserted, 1)
        self.assertEqual(summary.duplicates, 2)
        self.assertTrue(summary.is_consistent)

    def test_pii_not_logged_on_invalid_row(self) -> None:
        csv_path = os.path.join(self.temp_dir.name, "pii.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("name,email\n,secret@sensitive.com\n")

        with self.assertLogs("import_users", level="WARNING") as log_capture:
            import_csv(csv_path, self.conn)

        log_text = "\n".join(log_capture.output)
        self.assertIn("Row 2 skipped: Missing or empty name.", log_text)
        self.assertNotIn("secret@sensitive.com", log_text)

    def test_idempotent_reimport(self) -> None:
        csv_path = os.path.join(self.temp_dir.name, "idempotent.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("name,email\nAlice,alice@example.com\nBob,bob@example.com\n")

        summary1 = import_csv(csv_path, self.conn)
        self.assertEqual(summary1.inserted, 2)
        self.assertEqual(summary1.duplicates, 0)

        summary2 = import_csv(csv_path, self.conn)
        self.assertEqual(summary2.inserted, 0)
        self.assertEqual(summary2.duplicates, 2)
        self.assertEqual(summary2.rows_read, 2)
        self.assertTrue(summary2.is_consistent)

    def test_summary_is_consistent_property(self) -> None:
        self.assertTrue(ImportSummary(rows_read=10, inserted=5, duplicates=3, invalid=2).is_consistent)
        self.assertFalse(ImportSummary(rows_read=10, inserted=5, duplicates=3, invalid=1).is_consistent)

    def test_import_csv_with_precomputed_column_map(self) -> None:
        csv_path = os.path.join(self.temp_dir.name, "precomputed.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("name,email\nAlice,alice@example.com\n")

        col_map = inspect_csv_file(csv_path)
        summary = import_csv(csv_path, self.conn, column_map=col_map)
        self.assertEqual(summary.inserted, 1)

class TestDisplayUsers(unittest.TestCase):
    def test_display_empty_db(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            init_db(conn)
            display_users(conn)

    def test_display_with_data(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn:
            init_db(conn)
            insert_user(conn.cursor(), "Alice Longname User", "alice@example.com")
            conn.commit()
            display_users(conn)

class TestCLIAndMain(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_parse_args_defaults(self) -> None:
        args = parse_args([])
        self.assertEqual(args.csv, "users.csv")
        self.assertEqual(args.db, "users.db")

    def test_parse_args_custom(self) -> None:
        args = parse_args(["--csv", "custom.csv", "--db", "custom.db"])
        self.assertEqual(args.csv, "custom.csv")
        self.assertEqual(args.db, "custom.db")

    def test_parse_args_empty_values(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--csv", "   "])

    def test_main_missing_csv_does_not_create_db_file(self) -> None:
        stray_db = os.path.join(self.temp_dir.name, "stray_missing.db")
        with patch("sys.argv", ["import_users.py", "--csv", "missing_file_xyz.csv", "--db", stray_db]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(stray_db))

    def test_main_bad_header_does_not_create_db_file(self) -> None:
        bad_csv = os.path.join(self.temp_dir.name, "bad_header.csv")
        with open(bad_csv, "w", encoding="utf-8") as f:
            f.write("wrong,columns\n")
        stray_db = os.path.join(self.temp_dir.name, "stray_bad_header.db")
        with patch("sys.argv", ["import_users.py", "--csv", bad_csv, "--db", stray_db]):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)
        self.assertFalse(os.path.exists(stray_db))

    def test_main_successful_execution(self) -> None:
        csv_name = os.path.join(self.temp_dir.name, "valid.csv")
        with open(csv_name, "w", encoding="utf-8") as f:
            f.write("name,email\nAlice,alice@example.com\n")
        db_name = os.path.join(self.temp_dir.name, "valid.db")
        with patch("sys.argv", ["import_users.py", "--csv", csv_name, "--db", db_name]):
            main()
        with closing(sqlite3.connect(db_name)) as conn:
            self.assertEqual(conn.cursor().execute("SELECT COUNT(*) FROM users;").fetchone()[0], 1)

if __name__ == "__main__":
    unittest.main()
