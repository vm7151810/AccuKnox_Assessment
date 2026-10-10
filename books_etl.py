#!/usr/bin/env python3
"""ETL pipeline from Open Library API to SQLite."""

import argparse
from contextlib import closing
import logging
import sqlite3
import sys
from typing import Any

import requests

logger = logging.getLogger(__name__)

class APIFetchError(Exception):
    """Base exception for API communication errors."""

def fetch_books(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch book metadata from the Open Library API."""
    url = "https://openlibrary.org/search.json"
    params = {"q": query, "limit": limit}
    logger.info("Fetching up to %d books for query: '%s'...", limit, query)

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        docs = data.get("docs", [])
        logger.info("Successfully retrieved %d raw records from API.", len(docs))
        return docs
    except (requests.RequestException, ValueError) as err:
        raise APIFetchError(f"API request failed: {err}") from err

def clean_record(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and normalize a raw book record."""
    if not isinstance(raw, dict):
        return None

    ol_key = str(raw.get("key", "")).strip()
    title = str(raw.get("title", "")).strip()
    if not ol_key or not title:
        return None

    raw_authors = raw.get("author_name")
    author = None
    if isinstance(raw_authors, list):
        valid = [str(a).strip() for a in raw_authors if str(a).strip()]
        if valid:
            author = ", ".join(valid)
    elif raw_authors:
        author = str(raw_authors).strip()

    try:
        publication_year = int(raw.get("first_publish_year", ""))
    except (ValueError, TypeError):
        publication_year = None

    return {
        "ol_key": ol_key,
        "title": title,
        "author": author,
        "publication_year": publication_year,
    }

def init_db(conn: sqlite3.Connection) -> None:
    """Initialize the SQLite database schema."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ol_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                author TEXT,
                publication_year INTEGER
            );
        """)
    logger.info("Database schema initialized.")

def insert_books(conn: sqlite3.Connection, books: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert cleaned book records into SQLite idempotently."""
    if not books:
        return 0, 0

    rows = [(b["ol_key"], b["title"], b["author"], b["publication_year"]) for b in books]
    before = conn.total_changes
    with conn:
        conn.executemany("INSERT OR IGNORE INTO books (ol_key, title, author, publication_year) VALUES (?, ?, ?, ?);", rows)
    inserted = conn.total_changes - before
    return inserted, len(books) - inserted

def display_books(conn: sqlite3.Connection) -> None:
    """Retrieve and display all books from the database."""
    rows = conn.execute("SELECT id, ol_key, title, author, publication_year FROM books ORDER BY id ASC;").fetchall()
    
    if not rows:
        print("\n[DB] No books found in database.")
        return

    def trunc(val: Any, max_len: int) -> str:
        s = str(val) if val is not None else "-"
        return (s[: max_len - 3] + "...") if len(s) > max_len else s

    header = f"{'ID':<5} | {'Key':<18} | {'Title':<40} | {'Author':<30} | {'Year':<6}"
    print("\n" + "=" * len(header))
    print("STORED BOOKS IN DATABASE")
    print("=" * len(header))
    print(header)
    print(f"{'-'*5}-+-{'-'*18}-+-{'-'*40}-+-{'-'*30}-+-{'-'*6}")

    for r in rows:
        print(f"{trunc(r[0], 5):<5} | {trunc(r[1], 18):<18} | {trunc(r[2], 40):<40} | {trunc(r[3], 30):<30} | {trunc(r[4], 6):<6}")

    print("=" * len(header))
    print(f"Total records in database: {len(rows)}\n")

def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate command line arguments."""
    parser = argparse.ArgumentParser(description="ETL script to fetch books from Open Library and store in SQLite.")
    parser.add_argument("-q", "--query", type=str, default="python", help="Search query (default: 'python')")
    parser.add_argument("-l", "--limit", type=int, default=10, help="Maximum number of books (default: 10)")
    parser.add_argument("--db", type=str, default="books.db", help="Path to the SQLite database file (default: 'books.db')")
    
    parsed = parser.parse_args(args)
    if parsed.limit <= 0:
        parser.error("--limit must be a positive integer greater than 0.")
    if not parsed.query.strip():
        parser.error("--query must not be empty.")
    return parsed

def main() -> None:
    """Orchestrate the ETL workflow."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    args = parse_args()

    try:
        raw_books = fetch_books(query=args.query.strip(), limit=args.limit)
        
        valid_books = []
        for raw in raw_books:
            cleaned = clean_record(raw)
            if cleaned:
                valid_books.append(cleaned)
            else:
                logger.warning("Skipping malformed or incomplete record: %s", raw)

        with closing(sqlite3.connect(args.db)) as conn:
            init_db(conn)
            inserted, skipped = insert_books(conn, valid_books)
            logger.info("ETL Summary: fetched %d, valid %d, inserted %d, skipped %d", 
                        len(raw_books), len(valid_books), inserted, skipped)
            display_books(conn)

    except APIFetchError as err:
        logger.error("ETL pipeline aborted due to API fetch failure: %s", err)
        sys.exit(1)
    except sqlite3.Error as db_err:
        logger.error("SQLite error encountered: %s", db_err)
        sys.exit(1)

if __name__ == "__main__":
    main()
