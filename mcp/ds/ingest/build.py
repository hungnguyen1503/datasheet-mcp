"""Stable Stage 4 entry point for the canonical evidence index."""

from __future__ import annotations

import argparse


def build_part(
    part: str,
    *,
    with_prose: bool = True,
    with_graph: bool = True,
    with_enrichment: bool = True,
) -> dict:
    """Build the canonical evidence index for one part.

    ``with_prose`` and ``with_graph`` remain accepted for command compatibility,
    but disabling either is rejected because evidence text and relationships are
    required parts of the public query contract.
    """
    if not with_prose or not with_graph:
        raise ValueError("The evidence index requires both prose and graph data")
    from ..evidence.build import build_part as build_evidence_part
    return build_evidence_part(part, with_enrichment=with_enrichment)


def main() -> None:
    ap = argparse.ArgumentParser(description="Index a part into Qdrant + graph.")
    ap.add_argument("--part", required=True, help="Part name, e.g. ADXL345")
    ap.add_argument("--no-prose", action="store_true", help="Deprecated; evidence requires prose")
    ap.add_argument("--no-graph", action="store_true", help="Deprecated; evidence requires graph data")
    ap.add_argument("--no-enrich", action="store_true", help="Skip local AI enrichment")
    args = ap.parse_args()
    build_part(
        args.part,
        with_prose=not args.no_prose,
        with_graph=not args.no_graph,
        with_enrichment=not args.no_enrich,
    )


if __name__ == "__main__":
    main()
