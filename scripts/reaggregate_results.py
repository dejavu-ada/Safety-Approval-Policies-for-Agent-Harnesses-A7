#!/usr/bin/env python3
"""Rebuild derived experiment tables from saved AgentDojo results and audit logs.

This utility makes no model/API calls. It is useful after changing metric or failure
analysis code, and never substitutes for an actual benchmark run.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(PROJECT_ROOT))

from approval_policies.evaluation import (  # noqa: E402
    escalation_value_rows,
    failed_case_rows,
    metric_rows,
    write_metrics_csv,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _restore_results(serialized: dict[str, Any]) -> dict[str, Any]:
    def restore(rows: list[dict[str, Any]]) -> dict[tuple[str, str], bool]:
        return {
            (row["task_id"], row.get("injection_task_id") or ""): bool(row["result"])
            for row in rows
        }

    return {
        "utility_results": restore(serialized["utility_results"]),
        "security_results": restore(serialized["security_results"]),
    }


def reaggregate(output_dir: Path) -> None:
    metrics_path = output_dir / "metrics.csv"
    if not metrics_path.exists():
        raise FileNotFoundError(f"No metrics.csv found under {output_dir}")
    with metrics_path.open(encoding="utf-8", newline="") as stream:
        old_rows = list(csv.DictReader(stream))
    if not old_rows:
        raise ValueError(f"No measured rows found in {metrics_path}")

    metric_records = []
    case_records = []
    failure_records = []
    escalation_records = []
    for old in old_rows:
        audit_file = old["audit_file"]
        audit_path = output_dir / audit_file
        benchmark_path = audit_path.parent / "benchmark_results.json"
        if not audit_path.exists() or not benchmark_path.exists():
            raise FileNotFoundError(
                f"Cannot reaggregate row {old.get('policy')} k={old.get('k')}: "
                f"expected audit and benchmark files at {audit_path} and {benchmark_path}"
            )
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
        results = _restore_results(benchmark["results"])
        computed, cases = metric_rows(benchmark["suite"], results, audit_path)
        policy = old["policy"]
        k = int(old["k"])
        metric_records.append(
            {
                "policy": policy,
                "k": k,
                "attack": old.get("attack", benchmark.get("attack", "unknown")),
                "audit_file": audit_file,
                **computed,
            }
        )
        case_records.extend(
            {"policy": policy, "k": k, **case, "audit_file": audit_file}
            for case in cases
        )
        failure_records.extend(
            failed_case_rows(policy, k, cases, audit_file, audit_path)
        )
        escalation_records.extend(escalation_value_rows(policy, k, cases, audit_path))

    write_metrics_csv(metrics_path, metric_records)
    _write_jsonl(output_dir / "cases.jsonl", case_records)
    _write_jsonl(output_dir / "failures.jsonl", failure_records)
    write_metrics_csv(output_dir / "escalation_value.csv", escalation_records)
    print(
        "Reaggregated saved benchmark results without API calls: "
        f"{len(metric_records)} policy-budget rows, {len(case_records)} cases, "
        f"{len(failure_records)} failures under {output_dir}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Existing experiment output directory")
    args = parser.parse_args()
    reaggregate(args.input)


if __name__ == "__main__":
    main()
