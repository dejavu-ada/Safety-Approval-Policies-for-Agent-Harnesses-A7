#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from openai import OpenAI

from agentdojo.agent_pipeline.agent_pipeline import PipelineConfig
from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
from agentdojo.attacks.attack_registry import load_attack
from agentdojo.benchmark import benchmark_suite_with_injections, benchmark_suite_without_injections
from agentdojo.logging import OutputLogger
from agentdojo.task_suite.load_suites import get_suite

from approval_policies.audit import AuditLedger
from approval_policies.evaluation import (
    benchmark_case_id,
    escalation_value_rows,
    failed_case_rows,
    metric_rows,
    write_metrics_csv,
)
from approval_policies.models import ModelPricing
from approval_policies.policy import FullApprovePolicy, FullEscalatePolicy, RuleBasedPolicy
from approval_policies.reviewer import OpenAIHumanReviewer
from approval_policies.usage import UsageMeter
from approval_policies.gate import build_gated_pipeline


MODEL_RATES = {
    # Standard text-token rates, USD per million. Verified against OpenAI's public API
    # pricing on 2026-10-27; pass explicit CLI overrides if account pricing differs.
    "gpt-4o": ModelPricing(2.50, 10.00, 1.25),
    "gpt-4o-mini": ModelPricing(0.15, 0.60, 0.075),
}
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _uses_openrouter() -> bool:
    base_url = os.getenv("OPENAI_BASE_URL", "")
    return bool(os.getenv("OPENROUTER_API_KEY")) or "openrouter.ai" in base_url


def _resolve_model_id(model: str) -> str:
    if _uses_openrouter() and model in MODEL_RATES:
        return f"openai/{model}"
    return model


def _new_api_client() -> OpenAI:
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        return OpenAI(
            api_key=openrouter_key,
            base_url=os.getenv("OPENROUTER_BASE_URL", OPENROUTER_BASE_URL),
        )
    return OpenAI()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an AgentDojo approval-policy experiment.")
    parser.add_argument("--suite", default="workspace")
    parser.add_argument("--benchmark-version", default="v1.2.2")
    parser.add_argument("--user-task", action="append", dest="user_tasks")
    parser.add_argument("--injection-task", action="append", dest="injection_tasks")
    parser.add_argument("--attack", default="direct", help="AgentDojo attack name; use 'none' for clean tasks only.")
    default_model = "openai/gpt-4o-mini" if _uses_openrouter() else "gpt-4o-mini"
    parser.add_argument("--agent-model", default=default_model, help="Model ID; OpenRouter accepts provider/model IDs.")
    parser.add_argument("--reviewer-model", help="Reviewer model ID; defaults to --agent-model.")
    parser.add_argument("--k-values", default="0,1,2,4")
    parser.add_argument("--output", type=Path, default=Path("results/new-run"))
    parser.add_argument("--agent-input-rate", type=float)
    parser.add_argument("--agent-output-rate", type=float)
    parser.add_argument("--agent-cached-input-rate", type=float)
    parser.add_argument("--reviewer-input-rate", type=float)
    parser.add_argument("--reviewer-output-rate", type=float)
    parser.add_argument("--reviewer-cached-input-rate", type=float)
    return parser.parse_args()


def _pricing(args: argparse.Namespace) -> dict[str, ModelPricing]:
    agent = MODEL_RATES.get(args.agent_model.removeprefix("openai/"), ModelPricing())
    reviewer_default = MODEL_RATES.get(args.reviewer_model.removeprefix("openai/"), ModelPricing())
    return {
        "policy": ModelPricing(0.0, 0.0, 0.0),
        "agent": ModelPricing(
            args.agent_input_rate if args.agent_input_rate is not None else agent.input_usd_per_million,
            args.agent_output_rate if args.agent_output_rate is not None else agent.output_usd_per_million,
            args.agent_cached_input_rate if args.agent_cached_input_rate is not None else agent.cached_input_usd_per_million,
        ),
        "reviewer": ModelPricing(
            args.reviewer_input_rate if args.reviewer_input_rate is not None else reviewer_default.input_usd_per_million,
            args.reviewer_output_rate if args.reviewer_output_rate is not None else reviewer_default.output_usd_per_million,
            args.reviewer_cached_input_rate if args.reviewer_cached_input_rate is not None else reviewer_default.cached_input_usd_per_million,
        ),
    }


def _serialize_results(results: dict[str, Any]) -> dict[str, Any]:
    def pairs(mapping):
        return [
            {"task_id": task_id, "injection_task_id": injection_id or None, "result": bool(value)}
            for (task_id, injection_id), value in mapping.items()
        ]

    return {
        "utility_results": pairs(results["utility_results"]),
        "security_results": pairs(results["security_results"]),
        "injection_tasks_utility_results": results["injection_tasks_utility_results"],
    }


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def _safe_completion_rows(results: dict[str, Any], suite_name: str) -> list[dict[str, Any]]:
    rows = []
    for pair, utility in results["utility_results"].items():
        task_id, injection_id = pair
        safe = not bool(results["security_results"].get(pair, False)) if injection_id else True
        rows.append(
            {
                "task_id": task_id,
                "injection_task_id": injection_id or None,
                "case_id": benchmark_case_id(suite_name, task_id, injection_id or None),
                "utility_success": bool(utility),
                "safe": safe,
                "safe_task_completion": bool(utility) and safe,
            }
        )
    return rows


def _write_svg(path: Path, rows: list[dict[str, Any]]) -> None:
    policies = ("rule_based", "full_approve", "full_escalate")
    colors = {"rule_based": "#2563eb", "full_approve": "#dc2626", "full_escalate": "#16a34a"}
    width, height = 760, 480
    left, top, plot_width, plot_height = 74, 36, 630, 350
    k_values = sorted({int(row["k"]) for row in rows})
    if not k_values:
        return
    x_min, x_max = min(k_values), max(k_values)
    if x_min == x_max:
        x_max += 1
    y_min, y_max = 0.0, 1.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="380" y="24" text-anchor="middle" font-family="sans-serif" font-size="18">TCR@k</text>',
    ]
    for tick in range(6):
        y = top + plot_height * tick / 5
        value = 1 - tick / 5
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#ddd"/>')
        parts.append(f'<text x="{left - 12}" y="{y + 4:.1f}" text-anchor="end" font-family="sans-serif" font-size="12">{value:.1f}</text>')
    for k in k_values:
        x = left + plot_width * (k - x_min) / (x_max - x_min)
        parts.append(f'<text x="{x:.1f}" y="{top + plot_height + 23}" text-anchor="middle" font-family="sans-serif" font-size="12">{k}</text>')
    for policy in policies:
        points = [row for row in rows if row["policy"] == policy]
        points.sort(key=lambda row: int(row["k"]))
        coords = []
        for row in points:
            x = left + plot_width * (int(row["k"]) - x_min) / (x_max - x_min)
            y = top + plot_height * (1 - (float(row["tcr"]) - y_min) / (y_max - y_min))
            coords.append((x, y))
        if coords:
            polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            parts.append(f'<polyline points="{polyline}" fill="none" stroke="{colors[policy]}" stroke-width="3"/>')
            for x, y in coords:
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colors[policy]}"/>')
    parts.extend(
        [
            f'<line x1="{left}" y1="{top + plot_height}" x2="{left + plot_width}" y2="{top + plot_height}" stroke="#111"/>',
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" stroke="#111"/>',
            f'<text x="{left + plot_width / 2}" y="{top + plot_height + 52}" text-anchor="middle" font-family="sans-serif" font-size="14">Escalation budget k</text>',
            f'<text transform="translate(20 {top + plot_height / 2}) rotate(-90)" text-anchor="middle" font-family="sans-serif" font-size="14">Safe Task Completion Rate</text>',
        ]
    )
    for index, policy in enumerate(policies):
        y = height - 28
        x = left + index * 205
        parts.append(f'<line x1="{x}" y1="{y}" x2="{x + 24}" y2="{y}" stroke="{colors[policy]}" stroke-width="3"/>')
        parts.append(f'<text x="{x + 30}" y="{y + 4}" font-family="sans-serif" font-size="12">{policy}</text>')
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> None:
    args = parse_args()
    if not (os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY")):
        raise SystemExit(
            "Set OPENROUTER_API_KEY for OpenRouter, or OPENAI_API_KEY for OpenAI; do not store the key in source or Git."
        )
    args.agent_model = _resolve_model_id(args.agent_model)
    args.reviewer_model = _resolve_model_id(args.reviewer_model or args.agent_model)
    k_values = sorted({int(item.strip()) for item in args.k_values.split(",") if item.strip()})
    if not k_values or min(k_values) < 0:
        raise SystemExit("--k-values must contain one or more non-negative integers.")

    suite = get_suite(args.benchmark_version, args.suite)
    selected_tasks = args.user_tasks or list(suite.user_tasks)
    selected_injections = args.injection_tasks
    args.output.mkdir(parents=True, exist_ok=True)
    pricing = _pricing(args)
    metric_records: list[dict[str, Any]] = []
    case_records: list[dict[str, Any]] = []
    failure_records: list[dict[str, Any]] = []
    escalation_records: list[dict[str, Any]] = []
    full_approve_cache: tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] | None = None
    policies = {
        "rule_based": RuleBasedPolicy,
        "full_approve": FullApprovePolicy,
        "full_escalate": FullEscalatePolicy,
    }

    for policy_name, policy_factory in policies.items():
        for k in k_values:
            if policy_name == "full_approve" and full_approve_cache is not None:
                # This baseline is invariant to k; reuse its measured row and outcomes.
                prior, prior_cases, prior_escalations = full_approve_cache
                metric_records.append({**prior, "k": k})
                case_records.extend({**case, "k": k} for case in prior_cases)
                failure_records.extend(
                    failed_case_rows(
                        policy_name,
                        k,
                        prior_cases,
                        prior["audit_file"],
                        args.output / prior["audit_file"],
                    )
                )
                escalation_records.extend({**row, "k": k} for row in prior_escalations)
                write_metrics_csv(args.output / "metrics.csv", metric_records)
                _write_jsonl(args.output / "cases.jsonl", case_records)
                _write_jsonl(args.output / "failures.jsonl", failure_records)
                write_metrics_csv(args.output / "escalation_value.csv", escalation_records)
                continue

            run_dir = args.output / policy_name / f"k-{k}"
            run_dir.mkdir(parents=True, exist_ok=True)
            audit_path = run_dir / "approval_audit.jsonl"
            meter = UsageMeter()
            ledger = AuditLedger(audit_path, meter, pricing)
            agent_client = _new_api_client()
            agent_llm = OpenAILLM(agent_client, args.agent_model)
            config = PipelineConfig(
                llm=agent_llm,
                model_id=None,
                defense=None,
                system_message_name=None,
                system_message=None,
            )
            reviewer = OpenAIHumanReviewer(args.reviewer_model, client=_new_api_client())
            prompt_to_id = {task.PROMPT: task.ID for task in suite.user_tasks.values()}
            pipeline = build_gated_pipeline(
                config,
                policy=policy_factory(),
                reviewer=reviewer,
                ledger=ledger,
                usage_meter=meter,
                escalation_budget=k,
                task_id_resolver=lambda prompt, table=prompt_to_id: table.get(prompt, "unknown-task"),
            )
            agentdojo_logdir = run_dir / "agentdojo_logs"
            with OutputLogger(str(agentdojo_logdir)):
                if args.attack.lower() == "none":
                    results = benchmark_suite_without_injections(
                        pipeline,
                        suite,
                        logdir=agentdojo_logdir,
                        force_rerun=True,
                        user_tasks=selected_tasks,
                        benchmark_version=args.benchmark_version,
                    )
                else:
                    attack = load_attack(args.attack, suite, pipeline)
                    results = benchmark_suite_with_injections(
                        pipeline,
                        suite,
                        attack,
                        logdir=agentdojo_logdir,
                        force_rerun=True,
                        user_tasks=selected_tasks,
                        injection_tasks=selected_injections,
                        benchmark_version=args.benchmark_version,
                    )

            metrics, cases = metric_rows(args.suite, results, audit_path)
            audit_file = audit_path.relative_to(args.output).as_posix()
            record = {"policy": policy_name, "k": k, "attack": args.attack, "audit_file": audit_file, **metrics}
            metric_records.append(record)
            case_records.extend(
                {"policy": policy_name, "k": k, **case, "audit_file": audit_file}
                for case in cases
            )
            failure_records.extend(failed_case_rows(policy_name, k, cases, audit_file, audit_path))
            current_escalations = escalation_value_rows(policy_name, k, cases, audit_path)
            escalation_records.extend(current_escalations)
            if policy_name == "full_approve":
                full_approve_cache = (
                    record,
                    [{**case, "audit_file": audit_file} for case in cases],
                    current_escalations,
                )
            (run_dir / "benchmark_results.json").write_text(
                json.dumps(
                    {
                        "suite": args.suite,
                        "benchmark_version": args.benchmark_version,
                        "attack": args.attack,
                        "agent_model": args.agent_model,
                        "reviewer_model": args.reviewer_model,
                        "results": _serialize_results(results),
                        "safe_completion_cases": _safe_completion_rows(results, args.suite),
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            write_metrics_csv(args.output / "metrics.csv", metric_records)
            _write_jsonl(args.output / "cases.jsonl", case_records)
            _write_jsonl(args.output / "failures.jsonl", failure_records)
            write_metrics_csv(args.output / "escalation_value.csv", escalation_records)

    write_metrics_csv(args.output / "metrics.csv", metric_records)
    _write_jsonl(args.output / "cases.jsonl", case_records)
    _write_jsonl(args.output / "failures.jsonl", failure_records)
    write_metrics_csv(args.output / "escalation_value.csv", escalation_records)
    _write_svg(args.output / "tcr_at_k.svg", metric_records)
    print(f"Wrote measured metrics, cases, audit logs, and TCR curve under {args.output}")


if __name__ == "__main__":
    main()
