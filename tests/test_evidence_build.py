from __future__ import annotations

import pytest

from ds.ingest.build import build_part


@pytest.mark.parametrize(
    "options",
    ({"with_prose": False}, {"with_graph": False}),
)
def test_canonical_build_rejects_incomplete_evidence(options):
    with pytest.raises(ValueError, match="requires both prose and graph"):
        build_part("ANY_PART", **options)
