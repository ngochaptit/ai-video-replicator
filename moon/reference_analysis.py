"""Moon's adapter to the existing reference blueprint enrichment contract."""
from __future__ import annotations

from typing import Any

from jsonschema import ValidationError, validate
from schemas.artifacts import load_schema, validate_artifact
from tools.analysis.reference_blueprint_builder import ReferenceBlueprintBuilder


def enrichment_contract() -> dict[str, Any]:
    blueprint = load_schema("reference_blueprint")
    segment = blueprint["properties"]["segments"]["items"]
    # These are the canonical blueprint fields, not another blueprint schema.
    fields = ("id", "start_seconds", "end_seconds", "boundary_basis", "semantic",
              "camera", "spatial", "motion", "edit", "text", "audio", "confidence")
    return {
        "type": "object", "artifact": "semantic_enrichment", "required": ["segments"],
        "additionalProperties": False,
        "properties": {
            "segments": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["id", "semantic"],
                "properties": {name: segment["properties"][name] for name in fields},
                "additionalProperties": False,
            }},
            "choreography": blueprint["properties"]["choreography"],
        },
        "rules": [
            "Inspect reference_blueprint_scaffold and its measured images before describing actions.",
            "Use scaffold segment IDs to inherit timing and boundary_basis; do not recreate source or evidence fields.",
            "To refine segments, supply start_seconds, end_seconds and boundary_basis using measured timestamps only.",
            "Cover the whole reference contiguously; every segment requires in-range measured image evidence.",
            "Do not supply source overrides or new evidence paths/timestamps.",
        ],
    }


def enrich_reference(scaffold: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Validate before persistence and merge using the builder's measured boundaries."""
    try:
        validate(payload, enrichment_contract())
        validate_artifact("reference_blueprint", scaffold)
        result = ReferenceBlueprintBuilder().apply_semantic_enrichment(scaffold, payload)
        validate_artifact("reference_blueprint", result)
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        detail = exc.message if isinstance(exc, ValidationError) else str(exc)
        raise ValueError(f"analyze handoff requires valid measured semantic enrichment: {detail}") from exc
    return result
