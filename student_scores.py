#!/usr/bin/env python3
"""Fetch, clean, analyze, and visualize student test score records."""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8000/api/scores"
DEFAULT_DASHBOARD_FILE = "scores_dashboard.png"
MIN_VALID_SCORE = 0.0
MAX_VALID_SCORE = 100.0

COLOR_PRIMARY = "#2b5c8f"
COLOR_SECONDARY = "#4f8ebf"
COLOR_BENCHMARK = "#c44e4e"
COLOR_SECTION_A = "#3b7d52"
COLOR_SECTION_B = "#b36b22"


class ScoreDataFetchError(Exception):
    """Base exception for data retrieval failures."""


def fetch_scores_from_api(url: str, timeout: int = 10) -> list[dict[str, Any]]:
    """Fetch raw student data from an HTTP API endpoint."""
    logger.info("Fetching student scores from API: %s (timeout=%ds)...", url, timeout)
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ScoreDataFetchError("API root element must be a JSON array.")
        logger.info("Successfully fetched %d raw student records from API.", len(data))
        return data
    except (requests.RequestException, ValueError) as err:
        raise ScoreDataFetchError(f"Connection to API failed at {url}") from err


def load_scores_from_file(file_path: str | Path) -> list[dict[str, Any]]:
    """Load raw student data from a local JSON file."""
    path = Path(file_path).resolve()
    logger.info("Loading student scores from local file: %s", path)
    if not path.exists():
        raise ScoreDataFetchError(f"File not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ScoreDataFetchError("File root element must be a JSON array.")
        logger.info("Successfully loaded %d raw student records from file.", len(data))
        return data
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, OSError) as err:
        raise ScoreDataFetchError(f"Failed to load file {path}: {err}") from err


def get_student_data(
    url: str | None = None,
    file_path: str | Path | None = None,
    timeout: int = 10,
) -> list[dict[str, Any]]:
    """Retrieve student score records from file or API."""
    if file_path:
        return load_scores_from_file(file_path)
    return fetch_scores_from_api(url or DEFAULT_API_URL, timeout=timeout)


def validate_and_clean_score(
    raw_score: Any,
    min_score: float = MIN_VALID_SCORE,
    max_score: float = MAX_VALID_SCORE,
) -> tuple[float | None, str | None]:
    """Validate and clean an individual test score value."""
    if raw_score is None:
        return None, "missing"
    if isinstance(raw_score, bool):
        return None, "non_numeric"
    if isinstance(raw_score, str) and not raw_score.strip():
        return None, "missing"

    try:
        val = float(raw_score)
        if np.isnan(val) or np.isinf(val):
            return None, "non_numeric"

        if min_score <= val <= max_score:
            return val, "coerced" if isinstance(raw_score, str) else None
        return None, "out_of_range"
    except (ValueError, TypeError):
        return None, "non_numeric"


def clean_student_data(raw_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Clean student records per-value without discarding whole student profiles."""
    students: list[dict[str, Any]] = []
    audit_log: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            continue

        student_id = str(raw.get("id", f"UNKNOWN_{idx}")).strip()
        name = str(raw.get("name", f"Student {idx}")).strip()
        section = str(raw.get("section", "Unassigned")).strip()
        scores = raw.get("scores")

        if not isinstance(scores, dict):
            continue

        clean_scores: dict[str, float] = {}

        for subject, raw_val in scores.items():
            val, action = validate_and_clean_score(raw_val)

            if action == "coerced" and val is not None:
                clean_scores[subject] = val
                audit_log.append({
                    "student_id": student_id, "student_name": name,
                    "subject": subject, "raw_value": raw_val,
                    "action": "coerced_numeric_string", "resolved_value": val
                })
                logger.info("Student %s [%s] score coerced from %r to %.1f.", student_id, subject, raw_val, val)
            elif action:
                audit_log.append({
                    "student_id": student_id, "student_name": name,
                    "subject": subject, "raw_value": raw_val,
                    "action": f"dropped_{action}", "resolved_value": None
                })
                logger.warning("Student %s [%s] score dropped (%s): %r.", student_id, subject, action, raw_val)
            elif val is not None:
                clean_scores[subject] = val

        if clean_scores:
            students.append({
                "id": student_id, "name": name, "section": section,
                "scores": clean_scores, "valid_subject_count": len(clean_scores),
            })

    logger.info("Data cleaning complete: %d students retained, %d score anomalies handled.", len(students), len(audit_log))
    return students, audit_log


def compute_statistics(cleaned_students: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute analytical summary statistics."""
    if not cleaned_students:
        return {
            "total_students": 0, "overall_class_average": 0.0, "expected_subjects_count": 0,
            "curriculum_subjects": [], "student_stats": [], "subject_stats": {},
            "section_stats": {}, "section_subject_stats": {}, "section_gap_b_minus_a": {},
        }

    subjects = sorted({subj for s in cleaned_students for subj in s["scores"]})
    student_stats = []
    all_scores = []

    for s in cleaned_students:
        vals = list(s["scores"].values())
        avg = float(np.mean(vals)) if vals else 0.0
        all_scores.extend(vals)
        student_stats.append({
            "id": s["id"], "name": s["name"], "section": s["section"],
            "average": avg, "scores": s["scores"], "valid_count": len(vals),
            "is_partial": len(vals) < len(subjects),
        })

    overall_avg = float(np.mean(all_scores)) if all_scores else 0.0
    for stat in student_stats:
        stat["deviation_from_class_mean"] = stat["average"] - overall_avg

    subj_buckets: dict[str, list[float]] = {sub: [] for sub in subjects}
    for s in cleaned_students:
        for sub, val in s["scores"].items():
            subj_buckets[sub].append(val)

    subject_stats = {
        sub: {
            "mean": float(np.mean(vals)) if vals else 0.0,
            "min": float(np.min(vals)) if vals else 0.0,
            "max": float(np.max(vals)) if vals else 0.0,
            "count": len(vals), "values": vals,
        }
        for sub, vals in subj_buckets.items()
    }

    sec_buckets: dict[str, list[float]] = {}
    sec_avgs: dict[str, list[float]] = {}
    for stat in student_stats:
        sec_avgs.setdefault(stat["section"], []).append(stat["average"])
        sec_buckets.setdefault(stat["section"], []).extend(stat["scores"].values())

    section_stats = {
        sec: {"mean": float(np.mean(avgs)), "student_count": len(avgs), "score_count": len(sec_buckets.get(sec, []))}
        for sec, avgs in sec_avgs.items()
    }

    section_subject_stats: dict[str, dict[str, float]] = {sec: {} for sec in section_stats}
    for sec in section_stats:
        for sub in subjects:
            vals = [s["scores"][sub] for s in cleaned_students if s["section"] == sec and sub in s["scores"]]
            section_subject_stats[sec][sub] = float(np.mean(vals)) if vals else 0.0

    mean_a = section_subject_stats.get("Section A", {})
    mean_b = section_subject_stats.get("Section B", {})
    gap = {sub: mean_b.get(sub, 0.0) - mean_a.get(sub, 0.0) for sub in subjects}

    return {
        "total_students": len(cleaned_students),
        "overall_class_average": overall_avg,
        "expected_subjects_count": len(subjects),
        "curriculum_subjects": subjects,
        "student_stats": student_stats,
        "subject_stats": subject_stats,
        "section_stats": section_stats,
        "section_subject_stats": section_subject_stats,
        "section_gap_b_minus_a": gap,
    }


def display_summary_table(stats: dict[str, Any], audit_log: list[dict[str, Any]]) -> None:
    """Format and print an analytical statistical summary to standard output.

    Args:
        stats: Computed statistics dictionary.
        audit_log: List of cleaning audit events.
    """
    total = stats["total_students"]
    class_avg = stats["overall_class_average"]

    print("\n" + "=" * 78)
    print("STUDENT TEST SCORES ANALYTICS SUMMARY")
    print("=" * 78)
    print(f"Total Evaluated Students: {total}")
    print(f"Overall Class Benchmark Average: {class_avg:.2f} / 100")
    print("-" * 78)

    # Section Breakdown
    print("SECTION PERFORMANCE:")
    for sec, s_data in sorted(stats["section_stats"].items()):
        print(f"  * {sec:<15}: Mean = {s_data['mean']:>5.2f}  (Students: {s_data['student_count']})")
    print("-" * 78)

    # Subject Breakdown
    print("SUBJECT PERFORMANCE:")
    for subj, subj_data in sorted(stats["subject_stats"].items()):
        print(
            f"  * {subj:<12}: Mean = {subj_data['mean']:>5.2f} | "
            f"Min = {subj_data['min']:>5.1f} | Max = {subj_data['max']:>5.1f} | "
            f"Count = {subj_data['count']}"
        )
    print("-" * 78)

    # Section Gaps
    print("SECTION PERFORMANCE GAP (Section B - Section A):")
    for subj, diff in sorted(stats.get("section_gap_b_minus_a", {}).items()):
        lead_str = f"Section B +{diff:.1f}" if diff >= 0 else f"Section A +{-diff:.1f}"
        print(f"  * {subj:<12}: {diff:>+5.2f} pts ({lead_str})")
    print("-" * 78)

    # Audit Log Summary
    if audit_log:
        print(f"DATA CLEANING AUDIT TRAIL ({len(audit_log)} issues handled):")
        for entry in audit_log:
            res_str = f"-> {entry['resolved_value']}" if entry["resolved_value"] is not None else "-> DROPPED"
            print(
                f"  - [{entry['action']}] {entry['student_id']} ({entry['student_name']}) "
                f"{entry['subject']}: {entry['raw_value']!r} {res_str}"
            )
        print("=" * 78 + "\n")
    else:
        print("DATA CLEANING AUDIT TRAIL: 0 issues found (clean dataset).")
        print("=" * 78 + "\n")


# -----------------------------------------------------------------------------
# Visualization Routines: Individual Plots & Combined Dashboard
# -----------------------------------------------------------------------------

COLOR_SECTION_A = "#2b5c8f"
COLOR_SECTION_B = "#d95f02"
COLOR_BENCHMARK = "#c0392b"


def plot_student_averages(stats: dict[str, Any], ax: plt.Axes) -> None:
    """Plot individual student averages."""
    metrics = sorted(stats.get("student_stats", []), key=lambda s: s["average"])
    names = [s["name"] for s in metrics]
    averages = [s["average"] for s in metrics]

    ax.hlines(y=names, xmin=0, xmax=averages, color="#cbd5e1", linewidth=3.0)
    ax.scatter(averages, names, s=120, color=COLOR_PRIMARY, edgecolor="white", zorder=3)

    mean = stats.get("overall_class_average", 0.0)
    ax.axvline(x=mean, color=COLOR_BENCHMARK, linestyle="--", linewidth=1.5, zorder=1)

    ax.set_xlim(0, 105)
    ax.set_title("Individual Student Averages", fontsize=11, fontweight="bold", pad=10)
    ax.set_xlabel("Average Score (%)", fontsize=10, fontweight="bold")
    ax.grid(axis="x", linestyle=":", alpha=0.6)
    
    legend = [Line2D([0], [0], color=COLOR_BENCHMARK, linestyle="--", linewidth=1.8, label=f"Class Mean ({mean:.1f}%)")]
    ax.legend(handles=legend, loc="lower right", fontsize=8.5)


def plot_subject_distribution(stats: dict[str, Any], ax: plt.Axes) -> None:
    """Plot subject score distributions."""
    subjects = sorted(stats.get("curriculum_subjects", []))
    data = [stats["subject_stats"][sub]["values"] for sub in subjects]

    box = ax.boxplot(data, orientation="vertical", patch_artist=True, widths=0.4, showfliers=False)
    for patch in box["boxes"]:
        patch.set_facecolor("#e2e8f0")
        patch.set_edgecolor("#94a3b8")
    for median in box["medians"]:
        median.set_color("#111111")
        median.set_linewidth(2.0)

    rng = np.random.default_rng(42)
    for i, (sub, vals) in enumerate(zip(subjects, data), 1):
        jitter = rng.uniform(-0.15, 0.15, size=len(vals))
        ax.scatter(i + jitter, vals, color=COLOR_SECONDARY, edgecolor="w", s=40, alpha=0.7, zorder=3)
        mean = stats["subject_stats"][sub]["mean"]
        ax.plot([i - 0.25, i + 0.25], [mean, mean], color=COLOR_BENCHMARK, linestyle=":", linewidth=2, zorder=4)

    ax.set_xticks(range(1, len(subjects) + 1))
    ax.set_xticklabels(subjects, fontsize=10, fontweight="bold")
    ax.set_title("Score Distributions by Subject", fontsize=11, fontweight="bold", pad=10)
    ax.set_ylabel("Score (%)", fontsize=10, fontweight="bold")
    ax.grid(axis="y", linestyle=":", alpha=0.6)

    handles = [
        Line2D([0], [0], color="#111111", linewidth=2.2, label="Median"),
        Line2D([0], [0], color=COLOR_BENCHMARK, linestyle=":", linewidth=1.8, label="Subject Mean"),
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#2b5c8f", markersize=7, alpha=0.8, label="Student Score"),
    ]
    ax.legend(handles=handles, loc="lower right", fontsize=8.5)


def plot_section_gap_diverging_bars(stats: dict[str, Any], ax: plt.Axes) -> None:
    """Plot diverging bar chart of subject score differences between sections."""
    gap_dict = stats.get("section_gap_b_minus_a", {})
    subjects = sorted(stats.get("curriculum_subjects", []))
    differences = [gap_dict.get(s, 0.0) for s in subjects]

    y_pos = np.arange(len(subjects))
    colors = [COLOR_SECTION_B if d >= 0 else COLOR_SECTION_A for d in differences]

    bars = ax.barh(y_pos, differences, height=0.45, color=colors, alpha=0.9, edgecolor="#333333")
    ax.axvline(0, color="#333333", linestyle="-", linewidth=1.5)

    for bar, diff in zip(bars, differences):
        offset = 0.25 if diff >= 0 else -0.25
        ha = "left" if diff >= 0 else "right"
        label = f"+{diff:.1f}" if diff >= 0 else f"{diff:.1f}"
        ax.text(diff + offset, bar.get_y() + bar.get_height() / 2.0, label, va="center", ha=ha, fontsize=9.5, fontweight="bold", color="#222222")

    ax.set_yticks(y_pos)
    ax.set_yticklabels(subjects, fontsize=10, fontweight="bold")
    ax.set_xlabel("Section Mean Difference (Section B − Section A, points)", fontsize=10, fontweight="bold")

    limit = max(max([abs(d) for d in differences] + [4.0]) * 1.5, 5.0)
    ax.set_xlim(-limit, limit)
    ax.grid(axis="x", linestyle=":", alpha=0.6)

    handles = [
        Patch(facecolor=COLOR_SECTION_B, label="Section B Leads (Higher Mean)"),
        Patch(facecolor=COLOR_SECTION_A, label="Section A Leads (Higher Mean)"),
    ]
    ax.legend(handles=handles, loc="upper left", fontsize=8.5, framealpha=0.9)
    ax.set_title("Performance Gap Between Sections (Section B vs. Section A)", fontsize=11, fontweight="bold", pad=10)


def plot_score_heatmap(stats: dict[str, Any], cleaned_students: list[dict[str, Any]], ax: plt.Axes, fig: plt.Figure) -> None:
    """Plot student-subject score heatmap matrix."""
    student_stats = stats.get("student_stats", [])
    subjects = sorted(stats.get("curriculum_subjects", []))
    sorted_students = sorted(student_stats, key=lambda s: s["average"])
    names = [s["name"] for s in sorted_students]

    matrix = np.full((len(sorted_students), len(subjects)), np.nan)
    for r, s in enumerate(sorted_students):
        for c, sub in enumerate(subjects):
            if sub in s["scores"]:
                matrix[r, c] = s["scores"][sub]

    valid_scores = [v for s in cleaned_students for v in s["scores"].values()]
    min_val, max_val = min(valid_scores, default=60.0), max(valid_scores, default=100.0)

    cmap = plt.colormaps["YlGnBu"].copy()
    cmap.set_bad(color="#dcdde1")

    im = ax.imshow(matrix, aspect="auto", cmap=cmap, vmin=min_val, vmax=max_val)
    cbar = fig.colorbar(im, ax=ax, shrink=0.8, pad=0.03)
    cbar.set_label("Score (%)", fontsize=9, fontweight="bold")

    for r, s in enumerate(sorted_students):
        for c, sub in enumerate(subjects):
            if sub in s["scores"]:
                val = s["scores"][sub]
                color = "white" if val >= (min_val + max_val) / 2.0 else "#1c2833"
                ax.text(c, r, f"{val:.0f}", ha="center", va="center", fontsize=9, fontweight="bold", color=color)
            else:
                ax.text(c, r, "N/A", ha="center", va="center", fontsize=8.5, fontweight="bold", color="#636e72")

    ax.set_xticks(np.arange(len(subjects)))
    ax.set_xticklabels(subjects, fontsize=9.5, fontweight="bold")
    ax.set_yticks(np.arange(len(sorted_students)))
    ax.set_yticklabels(names, fontsize=8.5)
    ax.set_title("Student Performance Matrix Across Curriculum Subjects", fontsize=11, fontweight="bold", pad=10)


def generate_all_charts(
    stats: dict[str, Any],
    cleaned_students: list[dict[str, Any]],
    output_dir: str | Path = ".",
    dashboard_file: str | Path = DEFAULT_DASHBOARD_FILE,
    show: bool = False,
) -> dict[str, Path]:
    """Generate individual chart PNGs and a combined dashboard PNG."""
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}

    charts = [
        ("chart1", "chart1_student_averages.png", (10, 6.5), lambda ax: plot_student_averages(stats, ax)),
        ("chart2", "chart2_subject_distribution.png", (9, 5.5), lambda ax: plot_subject_distribution(stats, ax)),
        ("chart3", "chart3_section_subject_gap.png", (9, 5.5), lambda ax: plot_section_gap_diverging_bars(stats, ax)),
    ]

    for key, filename, size, plotter in charts:
        fig, ax = plt.subplots(figsize=size, dpi=150)
        fig.patch.set_facecolor("#f9fbfd")
        ax.set_facecolor("#ffffff")
        plotter(ax)
        fig.tight_layout()
        path = out_dir / filename
        fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        paths[key] = path
        logger.info("Saved %s to: %s", key.capitalize(), path)

    # Chart 4 (requires fig reference)
    fig4, ax4 = plt.subplots(figsize=(8, 7), dpi=150)
    fig4.patch.set_facecolor("#f9fbfd")
    ax4.set_facecolor("#ffffff")
    plot_score_heatmap(stats, cleaned_students, ax4, fig4)
    fig4.tight_layout()
    p4 = out_dir / "chart4_student_subject_heatmap.png"
    fig4.savefig(p4, facecolor=fig4.get_facecolor(), bbox_inches="tight")
    plt.close(fig4)
    paths["chart4"] = p4
    logger.info("Saved Chart4 to: %s", p4)

    dash_path = Path(dashboard_file).resolve()
    dash_path.parent.mkdir(parents=True, exist_ok=True)
    fig_dash, axes = plt.subplots(nrows=2, ncols=2, figsize=(18, 13), dpi=150)
    fig_dash.patch.set_facecolor("#f9fbfd")
    for row in axes:
        for ax in row:
            ax.set_facecolor("#ffffff")

    plot_student_averages(stats, axes[0, 0])
    plot_subject_distribution(stats, axes[0, 1])
    plot_section_gap_diverging_bars(stats, axes[1, 0])
    plot_score_heatmap(stats, cleaned_students, axes[1, 1], fig_dash)

    fig_dash.tight_layout(pad=3.0)
    fig_dash.savefig(dash_path, facecolor=fig_dash.get_facecolor(), edgecolor="none")
    if show:
        plt.show()
    plt.close(fig_dash)
    paths["dashboard"] = dash_path
    logger.info("Saved Combined Dashboard to: %s", dash_path)

    return paths


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse and validate command line arguments."""
    parser = argparse.ArgumentParser(description="Fetch, clean, analyze, and visualize student test score records.")
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--url", type=str, default=DEFAULT_API_URL, help=f"API URL (default: {DEFAULT_API_URL})")
    grp.add_argument("--file", type=str, default=None, help="Local JSON file fallback")
    parser.add_argument("-o", "--output", type=str, default=DEFAULT_DASHBOARD_FILE, help=f"Dashboard PNG path (default: {DEFAULT_DASHBOARD_FILE})")
    parser.add_argument("--output-dir", type=str, default=".", help="Directory for individual PNGs (default: '.')")
    parser.add_argument("--timeout", type=int, default=10, help="HTTP timeout (default: 10)")
    parser.add_argument("--show", action="store_true", help="Display interactive dashboard")

    parsed = parser.parse_args(args)
    if parsed.timeout <= 0:
        parser.error("--timeout must be a positive integer greater than 0.")
    return parsed


def main() -> None:
    """Orchestrate the end-to-end scores data processing and visualization pipeline."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    args = parse_args()

    try:
        raw = get_student_data(url=args.url if not args.file else None, file_path=args.file, timeout=args.timeout)
        if not raw:
            logger.error("Pipeline aborted: retrieved data is empty."); sys.exit(1)

        students, log = clean_student_data(raw)
        if not students:
            logger.error("Pipeline aborted: no valid student records remained after cleaning."); sys.exit(1)

        stats = compute_statistics(students)
        display_summary_table(stats, log)

        paths = generate_all_charts(stats, students, args.output_dir, args.output, args.show)
        logger.info("Visual artifacts generation complete. Saved %d images.", len(paths))
    except ScoreDataFetchError as err:
        logger.error("Pipeline aborted: failed to retrieve student data: %s", err)
        sys.exit(1)
    except (ValueError, OSError, RuntimeError) as plot_err:
        logger.error("Failed to render visualization charts: %s", plot_err)
        sys.exit(1)


if __name__ == "__main__":
    main()
