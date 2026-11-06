"""Invariant 5: no tool argument schema the LLM sees may carry identity.

Identity reaches tools only through the server-built ToolContext. This walks every
registered tool's JSON schema (nested objects and $defs included) for every vertical.
"""

from typing import Any, get_args

import pytest

from app.agents.tools.registry import REGISTRY, tools_for
from app.tenancy.orgs import OrgConfig

FORBIDDEN = {"org_id", "user_id", "customer_id", "role"}


def property_names(node: Any) -> set[str]:
    """Every key under any `properties` object, at any depth."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                found |= set(value)
            found |= property_names(value)
    elif isinstance(node, list):
        for item in node:
            found |= property_names(item)
    return found


def all_tools() -> list[tuple[str, Any]]:
    return [(vertical, tool) for vertical, tools in REGISTRY.items() for tool in tools]


def test_every_vertical_has_a_registry() -> None:
    verticals = set(get_args(OrgConfig.model_fields["vertical"].annotation))
    assert verticals <= set(REGISTRY), verticals - set(REGISTRY)


def test_unknown_vertical_is_rejected() -> None:
    with pytest.raises(KeyError):
        tools_for("bank")


@pytest.mark.parametrize(("vertical", "tool"), all_tools(), ids=lambda v: getattr(v, "name", v))
def test_no_identity_fields_in_tool_schemas(vertical: str, tool: Any) -> None:
    schema = tool.spec().parameters
    names = property_names(schema)
    assert not names & FORBIDDEN, f"{vertical}/{tool.name} exposes {names & FORBIDDEN}"
    # Extra keys are rejected too, so the model can't smuggle an org_id past the schema.
    assert schema.get("additionalProperties") is False, f"{tool.name} must forbid extra fields"


def test_search_knowledge_is_available_to_every_vertical() -> None:
    for vertical in REGISTRY:
        assert "search_knowledge" in tools_for(vertical)


def test_tool_names_are_unique_per_vertical() -> None:
    for vertical, tools in REGISTRY.items():
        names = [t.name for t in tools]
        assert len(names) == len(set(names)), vertical


def keys_anywhere(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys_anywhere(v)}
    if isinstance(node, list):
        return {k for v in node for k in keys_anywhere(v)}
    return set()


@pytest.mark.parametrize(("vertical", "tool"), all_tools(), ids=lambda v: getattr(v, "name", v))
def test_schemas_use_the_portable_subset(vertical: str, tool: Any) -> None:
    """No $ref/$defs (Gemini rejects them) and no anyOf-with-null for optional fields."""
    keys = keys_anywhere(tool.spec().parameters)
    assert not keys & {"$ref", "$defs", "anyOf"}, f"{vertical}/{tool.name}: {keys & {'$ref', '$defs', 'anyOf'}}"


def test_store_tools_registered() -> None:
    assert set(tools_for("store")) == {
        "search_knowledge", "search_products", "create_order", "update_order", "cancel_order",
        "get_order_status", "list_my_orders", "reorder", "confirm_proposal",
    }  # fmt: skip
