#!/usr/bin/env python3
"""Minimal Mock HTTP API for Student Scores."""

import argparse
import json
import logging
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8000
DEFAULT_HOST = "127.0.0.1"
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_FILE = BASE_DIR / "students_data.json"

def load_dataset(file_path: Path) -> list[dict[str, Any]]:
    """Load and validate student records from a JSON file."""
    path = file_path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Data file not found at: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Root JSON entity must be an array of records.")

    return data

def create_handler(records: list[dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    """Return a BaseHTTPRequestHandler bound to records."""
    payload = json.dumps(records).encode("utf-8")

    class StudentScoresHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/api/scores", "/scores"):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif path == "/health":
                health = b'{"status":"ok"}'
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(health)))
                self.end_headers()
                self.wfile.write(health)
            else:
                err = json.dumps({"error": f"Endpoint '{path}' not found."}).encode("utf-8")
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(err)))
                self.end_headers()
                self.wfile.write(err)

        def log_message(self, format: str, *args: Any) -> None:
            logger.info("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)

    return StudentScoresHandler

def run_server(host: str, port: int, data_path: Path) -> None:
    """Start and run the HTTP mock server."""
    try:
        records = load_dataset(data_path)
    except (FileNotFoundError, ValueError) as err:
        logger.error("Failed to load dataset: %s", err)
        sys.exit(1)

    handler = create_handler(records)
    server = HTTPServer((host, port), handler)
    logger.info("Serving %d student records at http://%s:%d/api/scores", len(records), host, port)
    logger.info("Press Ctrl+C to shut down.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down mock server...")
    finally:
        server.server_close()
        logger.info("Server terminated cleanly.")

def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate command line arguments."""
    parser = argparse.ArgumentParser(description="Run a lightweight mock HTTP API serving student test score records.")
    parser.add_argument("--host", type=str, default=DEFAULT_HOST, help=f"Host address to bind to (default: {DEFAULT_HOST})")
    parser.add_argument("-p", "--port", type=int, default=DEFAULT_PORT, help=f"Port number to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("-d", "--data", type=str, default=str(DEFAULT_DATA_FILE), help=f"Path to JSON dataset file (default: '{DEFAULT_DATA_FILE}')")

    parsed = parser.parse_args(args)
    if not (1 <= parsed.port <= 65535):
        parser.error("--port must be an integer between 1 and 65535.")

    return parsed

def main() -> None:
    """Orchestrate mock server lifecycle."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    args = parse_args()
    run_server(host=args.host, port=args.port, data_path=Path(args.data))

if __name__ == "__main__":
    main()
