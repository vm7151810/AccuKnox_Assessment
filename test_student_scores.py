"""Unit tests for the student scores ETL and visualization pipeline."""

import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import requests
from student_scores import (
    ScoreDataFetchError,
    clean_student_data,
    compute_statistics,
    fetch_scores_from_api,
    get_student_data,
    load_scores_from_file,
    main,
    parse_args,
    validate_and_clean_score,
)


class TestScoreValidationAndCleaning(unittest.TestCase):
    def test_valid_numeric_scores(self) -> None:
        self.assertEqual(validate_and_clean_score(85.5), (85.5, None))
        self.assertEqual(validate_and_clean_score(0), (0.0, None))
        self.assertEqual(validate_and_clean_score(100.0), (100.0, None))

    def test_string_coercion(self) -> None:
        self.assertEqual(validate_and_clean_score(" 95.5 "), (95.5, "coerced"))
        self.assertEqual(validate_and_clean_score("0"), (0.0, "coerced"))

    def test_missing_and_empty_scores(self) -> None:
        self.assertEqual(validate_and_clean_score(None), (None, "missing"))
        self.assertEqual(validate_and_clean_score("   "), (None, "missing"))

    def test_out_of_range_scores(self) -> None:
        self.assertEqual(validate_and_clean_score(-5.0), (None, "out_of_range"))
        self.assertEqual(validate_and_clean_score(105), (None, "out_of_range"))
        self.assertEqual(validate_and_clean_score(" 150 "), (None, "out_of_range"))

    def test_non_numeric_and_booleans(self) -> None:
        self.assertEqual(validate_and_clean_score(True), (None, "non_numeric"))
        self.assertEqual(validate_and_clean_score(False), (None, "non_numeric"))
        self.assertEqual(validate_and_clean_score("abc"), (None, "non_numeric"))
        self.assertEqual(validate_and_clean_score(float("inf")), (None, "non_numeric"))
        self.assertEqual(validate_and_clean_score(float("nan")), (None, "non_numeric"))

    def test_clean_student_data_drops_invalid_keeps_valid(self) -> None:
        raw = [{
            "id": "1", "name": "Alice", "section": "A",
            "scores": {"Math": 95, "History": "invalid", "Art": 120, "Science": "88.5"}
        }]
        cleaned, audit = clean_student_data(raw)
        self.assertEqual(len(cleaned), 1)
        self.assertEqual(cleaned[0]["scores"], {"Math": 95.0, "Science": 88.5})
        self.assertEqual(cleaned[0]["valid_subject_count"], 2)
        
        actions = {a["subject"]: a["action"] for a in audit}
        self.assertEqual(actions["History"], "dropped_non_numeric")
        self.assertEqual(actions["Art"], "dropped_out_of_range")
        self.assertEqual(actions["Science"], "coerced_numeric_string")

    def test_clean_student_data_drops_entire_student_if_no_valid_scores(self) -> None:
        raw = [{"id": "1", "scores": {"Math": "bad", "Sci": -10}}]
        cleaned, audit = clean_student_data(raw)
        self.assertEqual(len(cleaned), 0)
        self.assertEqual(len(audit), 2)


class TestStatisticsComputation(unittest.TestCase):
    def test_compute_statistics(self) -> None:
        cleaned = [
            {"id": "1", "name": "A", "section": "S1", "scores": {"Math": 90, "Sci": 80}, "valid_subject_count": 2},
            {"id": "2", "name": "B", "section": "S2", "scores": {"Math": 100, "Art": 70}, "valid_subject_count": 2},
        ]
        stats = compute_statistics(cleaned)
        self.assertEqual(stats["total_students"], 2)
        self.assertEqual(stats["overall_class_average"], 85.0)
        self.assertEqual(stats["expected_subjects_count"], 3)
        self.assertEqual(set(stats["curriculum_subjects"]), {"Math", "Sci", "Art"})

        math_stats = stats["subject_stats"]["Math"]
        self.assertEqual(math_stats["mean"], 95.0)
        self.assertEqual(math_stats["count"], 2)

        self.assertEqual(stats["section_stats"]["S1"]["mean"], 85.0)
        self.assertEqual(stats["section_stats"]["S2"]["mean"], 85.0)

    def test_compute_statistics_empty(self) -> None:
        stats = compute_statistics([])
        self.assertEqual(stats["total_students"], 0)
        self.assertEqual(stats["overall_class_average"], 0.0)


class TestDataFetchingAndErrorHandling(unittest.TestCase):
    @patch("student_scores.requests.get")
    def test_fetch_scores_success(self, mock_get: MagicMock) -> None:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = [{"id": "1", "scores": {}}]
        data = fetch_scores_from_api("http://test")
        self.assertEqual(len(data), 1)

    @patch("student_scores.requests.get", side_effect=requests.exceptions.Timeout("timed out"))
    def test_fetch_scores_timeout(self, mock_get: MagicMock) -> None:
        with self.assertRaises(ScoreDataFetchError):
            fetch_scores_from_api("http://test")

    @patch("student_scores.requests.get", side_effect=requests.exceptions.ConnectionError("conn err"))
    def test_fetch_scores_connection_error(self, mock_get: MagicMock) -> None:
        with self.assertRaises(ScoreDataFetchError):
            fetch_scores_from_api("http://test")

    @patch("student_scores.requests.get")
    def test_fetch_scores_http_error(self, mock_get: MagicMock) -> None:
        mock_get.return_value.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
        with self.assertRaises(ScoreDataFetchError):
            fetch_scores_from_api("http://test")

    @patch("student_scores.requests.get")
    def test_fetch_scores_invalid_json(self, mock_get: MagicMock) -> None:
        mock_get.return_value.json.side_effect = ValueError("No JSON object")
        with self.assertRaises(ScoreDataFetchError):
            fetch_scores_from_api("http://test")

    def test_load_scores_from_file_success(self) -> None:
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tmp:
            json.dump([{"id": "1"}], tmp)
            tmp_path = tmp.name
        data = load_scores_from_file(tmp_path)
        self.assertEqual(len(data), 1)
        Path(tmp_path).unlink()

    def test_load_scores_from_file_not_found(self) -> None:
        with self.assertRaises(ScoreDataFetchError):
            load_scores_from_file("does_not_exist.json")

    def test_load_scores_from_file_malformed_json(self) -> None:
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tmp:
            tmp.write("{ invalid json")
            tmp_path = tmp.name
        with self.assertRaises(ScoreDataFetchError):
            load_scores_from_file(tmp_path)
        Path(tmp_path).unlink()

    @patch("student_scores.fetch_scores_from_api")
    def test_get_student_data_routes_correctly(self, mock_api: MagicMock) -> None:
        with tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json") as tmp:
            json.dump([], tmp)
            tmp_path = tmp.name
        
        get_student_data(file_path=tmp_path)
        mock_api.assert_not_called()
        
        get_student_data(url="http://test")
        mock_api.assert_called_with("http://test", timeout=10)
        Path(tmp_path).unlink()


class TestCLIAndMain(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        args = parse_args([])
        self.assertEqual(args.url, "http://127.0.0.1:8000/api/scores")
        self.assertIsNone(args.file)

    def test_parse_args_custom(self) -> None:
        args = parse_args(["--file", "data.json", "--timeout", "5"])
        self.assertEqual(args.file, "data.json")
        self.assertEqual(args.timeout, 5)

    def test_parse_args_invalid_timeout(self) -> None:
        with self.assertRaises(SystemExit):
            parse_args(["--timeout", "0"])

    @patch("student_scores.get_student_data", side_effect=ScoreDataFetchError("fail"))
    @patch("student_scores.parse_args")
    def test_main_aborts_on_fetch_error(self, mock_parse: MagicMock, mock_get: MagicMock) -> None:
        mock_parse.return_value.file = None
        mock_parse.return_value.url = "http://test"
        mock_parse.return_value.timeout = 10
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

    @patch("student_scores.get_student_data", return_value=[])
    @patch("student_scores.parse_args")
    def test_main_aborts_on_empty_data(self, mock_parse: MagicMock, mock_get: MagicMock) -> None:
        mock_parse.return_value.file = None
        mock_parse.return_value.url = "http://test"
        mock_parse.return_value.timeout = 10
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
