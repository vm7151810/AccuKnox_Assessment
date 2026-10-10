#!/usr/bin/env python3
"""CSV Data Import to SQLite Database."""

import argparse
from contextlib import closing
from dataclasses import dataclass
import csv
import logging
import os
import re
import sqlite3
import sys
from typing import Any

logger = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REQUIRED_COLUMNS = ("name", "email")

class CSVImportError(Exception):
    """Base exception for CSV import errors."""

class CSVFileNotFoundError(CSVImportError):
    """Raised when the specified CSV file does not exist."""

class HeaderValidationError(CSVImportError):
    """Raised when the CSV header is missing or lacks required columns."""

@dataclass
class ImportSummary:
    """Stores row-level metrics for the CSV import operation."""
    rows_read: int = 0
    inserted: int = 0
    duplicates: int = 0
    invalid: int = 0

    @property
    def is_consistent(self) -> bool:
        return self.rows_read == (self.inserted + self.duplicates + self.invalid)

def validate_headers(fieldnames: list[str] | None) -> dict[str, str]:
    """Validate CSV header fields against required column names."""
    if not fieldnames:
        raise HeaderValidationError("CSV file is empty or missing a header row.")

    normalized = {col.strip().lower(): col for col in fieldnames if col}
    mapping = {}
    missing = []

    for req in REQUIRED_COLUMNS:
        if req in normalized:
            mapping[req] = normalized[req]
        else:
            missing.append(req)

    if missing:
        raise HeaderValidationError(
            f"CSV header is missing required column(s): {', '.join(missing)}. Found: {fieldnames}"
        )
    return mapping

def inspect_csv_file(path: str) -> dict[str, str]:
    """Verify CSV file existence and validate headers."""
    if not os.path.isfile(path):
        raise CSVFileNotFoundError(f"CSV file not found: '{path}'")

    try:
        with open(path, mode="r", encoding="utf-8-sig", newline="") as f:
            return validate_headers(csv.DictReader(f).fieldnames)
    except (UnicodeDecodeError, csv.Error, OSError) as err:
        raise CSVImportError(f"Failed to read CSV file '{path}': {err}") from err

def normalize_text(value: str | None) -> str:
    """Normalize text by stripping whitespace."""
    return value.strip() if value else ""

def normalize_email(email: str | None) -> str:
    """Normalize email by stripping whitespace and lowercasing."""
    return normalize_text(email).lower()

def validate_user(name: str, email: str) -> tuple[bool, str | None]:
    """Validate user fields for required format and non-empty values."""
    if not name:
        return False, "Missing or empty name"
    if not email:
        return False, "Missing or empty email"
    if not EMAIL_REGEX.match(email):
        return False, "Invalid email format"
    return True, None

def init_db(conn: sqlite3.Connection) -> None:
    """Initialize the SQLite database schema."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL CHECK (length(trim(name)) > 0),
                email TEXT NOT NULL UNIQUE COLLATE NOCASE
            );
        """)
    logger.info("Database schema initialized.")

def insert_user(cursor: sqlite3.Connection | sqlite3.Cursor, name: str, email: str) -> bool:
    """Insert a user record into the database."""
    return cursor.execute(
        "INSERT OR IGNORE INTO users (name, email) VALUES (?, ?);",
        (name, email)
    ).rowcount == 1

def import_csv(
    path: str,
    conn: sqlite3.Connection,
    column_map: dict[str, str] | None = None,
) -> ImportSummary:
    """Read, validate, normalize, and import user records from CSV into SQLite."""
    if column_map is None:
        column_map = inspect_csv_file(path)

    name_key, email_key = column_map["name"], column_map["email"]
    summary = ImportSummary()

    try:
        with open(path, mode="r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            cursor = conn.cursor()

            with conn:
                for row in reader:
                    summary.rows_read += 1
                    clean_name = normalize_text(row.get(name_key))
                    clean_email = normalize_email(row.get(email_key))

                    is_valid, reason = validate_user(clean_name, clean_email)
                    if not is_valid:
                        summary.invalid += 1
                        logger.warning("Row %d skipped: %s.", reader.line_num, reason)
                        continue

                    if insert_user(cursor, clean_name, clean_email):
                        summary.inserted += 1
                        logger.info("Row %d: inserted successfully.", reader.line_num)
                    else:
                        summary.duplicates += 1
                        logger.info("Row %d skipped: duplicate email.", reader.line_num)

    except (UnicodeDecodeError, csv.Error, OSError) as err:
        raise CSVImportError(f"Failed to read CSV file '{path}': {err}") from err

    if not summary.is_consistent:
        raise AssertionError(f"Accounting invariant violated: {summary}")

    return summary

def display_users(conn: sqlite3.Connection) -> None:
    """Retrieve and display all stored users from the database."""
    rows = conn.execute("SELECT id, name, email FROM users ORDER BY id ASC;").fetchall()

    if not rows:
        print("\n[DB] No users found in database.")
        return

    def trunc(val: Any, max_len: int) -> str:
        s = str(val) if val is not None else "-"
        return (s[: max_len - 3] + "...") if len(s) > max_len else s

    header = f"{'ID':<5} | {'Name':<30} | {'Email':<35}"
    print("\n" + "=" * len(header))
    print("STORED USERS IN DATABASE")
    print("=" * len(header))
    print(header)
    print(f"{'-'*5}-+-{'-'*30}-+-{'-'*35}")

    for row in rows:
        print(f"{trunc(row[0], 5):<5} | {trunc(row[1], 30):<30} | {trunc(row[2], 35):<35}")

    print("=" * len(header))
    print(f"Total records in database: {len(rows)}\n")

def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate command line arguments."""
    parser = argparse.ArgumentParser(description="Import user records from a CSV file into a SQLite database.")
    parser.add_argument("--csv", type=str, default="users.csv", help="Path to the input CSV file (default: 'users.csv')")
    parser.add_argument("--db", type=str, default="users.db", help="Path to the SQLite database file (default: 'users.db')")

    parsed = parser.parse_args(args)
    if not parsed.csv.strip():
        parser.error("--csv path cannot be empty.")
    if not parsed.db.strip():
        parser.error("--db path cannot be empty.")
    return parsed

def main() -> None:
    """Orchestrate the CSV data import process."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    args = parse_args()
    path, db_path = args.csv.strip(), args.db.strip()

    try:
        column_map = inspect_csv_file(path)
        with closing(sqlite3.connect(db_path)) as conn:
            init_db(conn)
            summary = import_csv(path, conn, column_map=column_map)

            logger.info("Import Summary: %d read, %d inserted, %d duplicates, %d invalid", 
                        summary.rows_read, summary.inserted, summary.duplicates, summary.invalid)

            print("\n" + "=" * 40)
            print("IMPORT EXECUTION SUMMARY")
            print("=" * 40)
            print(f"Rows Read:   {summary.rows_read}")
            print(f"Inserted:    {summary.inserted}")
            print(f"Duplicates:  {summary.duplicates}")
            print(f"Invalid:     {summary.invalid}")
            print("=" * 40)

            display_users(conn)

    except (CSVFileNotFoundError, HeaderValidationError, CSVImportError, sqlite3.Error) as err:
        logger.error("%s", err)
        sys.exit(1)

if __name__ == "__main__":
    main()
