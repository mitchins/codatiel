"""SDK-free unit tests for the Haiku provider adapter's message mapping.

No agents SDK, no API keys, no network: exercises the pure translation
functions so the multi-turn tool-continuation protocol stays correct.
"""

from __future__ import annotations

from codatiel_i2.runtime_sdk import (
    HaikuModel,
    _responses_input_to_anthropic,
)


def _adapter() -> HaikuModel:
    obj = HaikuModel.__new__(HaikuModel)
    obj._thinking = {"type": "adaptive"}
    obj._thinking_groups = []
    obj.thinking_blocks_seen = 0
    return obj


def test_tool_results_reach_model_not_user_text():
    items = [
        {"role": "user", "type": "message", "content": "go"},
        {"type": "function_call", "call_id": "c1", "name": "read_file",
         "arguments": '{"path": "a.py"}'},
        {"type": "function_call_output", "call_id": "c1", "output": '{"lines": 3}'},
    ]
    _, messages = _responses_input_to_anthropic(items)
    assert messages[1] == {"role": "assistant", "content": [{
        "type": "tool_use", "id": "c1", "name": "read_file",
        "input": {"path": "a.py"}}]}
    assert messages[2] == {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "c1",
        "content": '{"lines": 3}'}]}


def test_thinking_reemitted_before_matching_turn_only():
    adapter = _adapter()
    thought_1 = {"type": "thinking", "thinking": "first", "signature": "s1"}
    thought_3 = {"type": "thinking", "thinking": "third", "signature": "s3"}
    adapter._thinking_groups = [
        {"ids": ["c1"], "thinking": [thought_1]},
        {"ids": ["c3"], "thinking": [thought_3]},
    ]
    items = [
        {"type": "message", "role": "user", "content": "review this"},
        {"type": "function_call", "call_id": "c1", "name": "read_file",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "r1"},
        {"type": "function_call", "call_id": "c3", "name": "diff_window",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c3", "output": "r3"},
    ]
    _, messages = adapter._input_to_anthropic(items)
    turn_1 = messages[1]["content"]
    turn_2 = messages[3]["content"]
    assert turn_1[0] == thought_1
    assert turn_1[-1]["id"] == "c1"
    assert turn_2[0] == thought_3
    assert turn_2[-1]["id"] == "c3"
    # No cross-contamination: latest thought is not on the older turn.
    assert thought_3 not in turn_1
    assert thought_1 not in turn_2


def test_unknown_tool_use_gets_no_thought():
    adapter = _adapter()
    adapter._thinking_groups = [
        {"ids": ["c1"], "thinking": [{"type": "thinking", "thinking": "t",
                                      "signature": "s"}]}]
    items = [{"type": "function_call", "call_id": "c9", "name": "search_code",
              "arguments": "{}"}]
    _, messages = adapter._input_to_anthropic(items)
    assert messages[0]["content"] == [{
        "type": "tool_use", "id": "c9", "name": "search_code", "input": {}}]


def test_parallel_tool_fragments_share_one_emission():
    """Two tool_use from one response arrive as separate fragments; the
    signed thinking block must be emitted once, before the first fragment —
    never duplicated or interleaved after a tool_use."""
    adapter = _adapter()
    thought = {"type": "thinking", "thinking": "plan", "signature": "sig"}
    adapter._thinking_groups = [{"ids": ["c1", "c2"], "thinking": [thought]}]
    items = [
        {"type": "function_call", "call_id": "c1", "name": "read_file",
         "arguments": "{}"},
        {"type": "function_call", "call_id": "c2", "name": "search_code",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "r1"},
        {"type": "function_call_output", "call_id": "c2", "output": "r2"},
    ]
    _, messages = adapter._input_to_anthropic(items)
    # Parallel fragments merge into one assistant turn: single emission,
    # thinking first, both tool_uses last, no duplication or interleave.
    assert [m["role"] for m in messages] == ["assistant", "user"]
    turn = messages[0]["content"]
    assert turn[0] == thought
    assert [c.get("id") for c in turn[1:]] == ["c1", "c2"]
    results = messages[1]["content"]
    assert [r.get("tool_use_id") for r in results] == ["c1", "c2"]
    flat = [c for m in messages for c in m["content"]]
    assert sum(1 for c in flat if c.get("type") == "thinking") == 1


def test_merge_consecutive_assistant_fragments():
    from codatiel_i2.runtime_sdk import _merge_same_role

    thought = {"type": "thinking", "thinking": "t", "signature": "s"}
    tool = {"type": "tool_use", "id": "c1", "name": "read_file", "input": {}}
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "checking"},
        {"role": "assistant", "content": [thought, tool]},
    ]
    merged = _merge_same_role(messages)
    assert len(merged) == 2
    content = merged[1]["content"]
    # Canonical order: thinking, text, then tool_use.
    assert content[0] == thought
    assert content[1] == {"type": "text", "text": "checking"}
    assert content[2] == tool


def test_merge_consecutive_user_tool_results():
    from codatiel_i2.runtime_sdk import _merge_same_role

    r1 = {"type": "tool_result", "tool_use_id": "c1", "content": "a"}
    r2 = {"type": "tool_result", "tool_use_id": "c2", "content": "b"}
    messages = [
        {"role": "user", "content": [r1]},
        {"role": "user", "content": [r2]},
    ]
    merged = _merge_same_role(messages)
    assert len(merged) == 1
    assert merged[0]["content"] == [r1, r2]


def test_alternation_holds_end_to_end():
    adapter = _adapter()
    adapter._thinking_groups = [
        {"ids": ["c1"], "thinking": [{"type": "thinking", "thinking": "t",
                                      "signature": "s"}]}]
    items = [
        {"type": "message", "role": "user", "content": "go"},
        {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "looking"}]},
        {"type": "function_call", "call_id": "c1", "name": "read_file",
         "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
    ]
    _, messages = adapter._input_to_anthropic(items)
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"]
    turn = messages[1]["content"]
    assert turn[0]["type"] == "thinking"
    assert turn[-1]["type"] == "tool_use"
    assert any(c.get("type") == "text" for c in turn)


def test_empty_thinking_not_retained():
    from codatiel_i2.runtime_sdk import _retain_thinking_block

    assert _retain_thinking_block("thinking", "", "sig") is None
    assert _retain_thinking_block("thinking", "  ", "sig") is None
    kept = _retain_thinking_block("thinking", "plan", "sig")
    assert kept == {"type": "thinking", "thinking": "plan", "signature": "sig"}


def test_assistant_blocks_ordered_thinking_text_tool_use():
    from codatiel_i2.runtime_sdk import _order_assistant_blocks

    tool = {"type": "tool_use", "id": "c1", "name": "r", "input": {}}
    text = {"type": "text", "text": "looking"}
    thought = {"type": "thinking", "thinking": "t", "signature": "s"}
    assert _order_assistant_blocks([tool, text], "assistant") == [text, tool]
    assert _order_assistant_blocks([tool, thought, text], "assistant") == [
        thought, text, tool]
    other = [{"type": "tool_result", "tool_use_id": "c", "content": "x"}]
    assert _order_assistant_blocks(other, "user") == other
