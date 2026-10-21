"""Mutation operators registry (§18). Detectors add operators in Phase C+."""

from __future__ import annotations

OPERATORS: dict[str, list[str]] = {
    "docs_code": ["change_default", "rename_flag", "remove_endpoint", "alter_enum", "stale_example"],
    "contracts": ["optional_to_required", "rename_serialized_field", "change_wire_type",
                  "remove_enum_case", "remove_migration", "alter_vendor_cardinality"],
    "authorization": ["remove_guard", "remove_tenant_filter", "weaken_role",
                      "reorder_check_after_write", "bypass_guarded_service"],
    "operating_constraints": ["stream_to_read_all", "worker_to_main_thread", "remove_lock",
                              "bounded_cache_to_dict", "atomic_replace_to_direct_write"],
    "semantic_twins": ["fix_one_twin_only", "rename_clone", "duplicate_with_divergence"],
}
