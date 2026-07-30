#!/usr/bin/env python3
"""Evaluate exact-fact retrieval, packet completeness, provenance, and latency."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

import _bootstrap  # noqa: F401

from ds.evidence.service import DatasheetService
from ds.evidence.store_qdrant import EvidenceStoreQdrant


DEFAULT_CASES = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_queries.json"


def _text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).casefold()


def _matched_groups(text: str, groups: list[list[str]]) -> int:
    return sum(any(alias.casefold() in text for alias in group) for group in groups)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return ordered[index]


def evaluate(cases: list[dict], repeats: int) -> dict:
    service = DatasheetService(EvidenceStoreQdrant())
    service.prewarm()
    rows = []
    latencies: list[float] = []
    for case in cases:
        response = None
        for _ in range(max(1, repeats)):
            started = time.perf_counter()
            response = service.query(
                case["part"], case["question"],
                focus=case.get("focus", "auto"), max_tokens=3000,
            )
            latencies.append((time.perf_counter() - started) * 1000)
        assert response is not None
        groups = case["expected_facts"]
        top_five = _text([item.model_dump(mode="json") for item in response.facts[:5]])
        packet = _text(response.model_dump(mode="json"))
        source_ids = {source.source_id for source in response.sources}
        referenced = {
            source.source_id
            for item in [*response.facts, *response.constraints]
            for source in item.sources
        }
        source_valid = referenced <= source_ids
        no_leakage = (
            all(item.part.upper() == case["part"].upper() for item in response.facts)
            and all(source.part.upper() == case["part"].upper() for source in response.sources)
            and all(edge.part.upper() == case["part"].upper() for edge in response.related_entities)
        )
        rows.append({
            "id": case["id"],
            "recall_at_5": _matched_groups(top_five, groups) / len(groups),
            "packet_completeness": _matched_groups(packet, groups) / len(groups),
            "source_valid": source_valid,
            "no_cross_part_leakage": no_leakage,
            "gaps": response.gaps,
        })
    return {
        "cases": rows,
        "mean_recall_at_5": statistics.fmean(row["recall_at_5"] for row in rows),
        "mean_packet_completeness": statistics.fmean(row["packet_completeness"] for row in rows),
        "source_validity": all(row["source_valid"] for row in rows),
        "zero_cross_part_leakage": all(row["no_cross_part_leakage"] for row in rows),
        "query_p95_ms": _percentile(latencies, 0.95),
        "passed": (
            statistics.fmean(row["recall_at_5"] for row in rows) >= 0.90
            and statistics.fmean(row["packet_completeness"] for row in rows) >= 0.85
            and all(row["source_valid"] for row in rows)
            and all(row["no_cross_part_leakage"] for row in rows)
            and _percentile(latencies, 0.95) <= 2000
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--strict", action="store_true", help="Exit nonzero when targets fail")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    result = evaluate(cases, args.repeats)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.strict and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
