# Run: uv run python tests/test_history.py
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel

from desktop_agent import agent as a


def fake(messages, info):
    """Turn 1 calls print_to_ui with a long message; every reply reports how many prompts it can see."""
    prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
    if prompts[-1] == "long" and not isinstance(messages[-1].parts[-1], ToolReturnPart):
        return ModelResponse(parts=[ToolCallPart("print_to_ui", {"message": "x" * 1000})])
    return ModelResponse(parts=[TextPart(f"seen: {prompts}")])


with a.agent.override(model=FunctionModel(fake)):
    a.forget()
    a.ask("make hello.py", say=lambda _: None)
    assert "make hello.py" in a.ask("run the program", say=lambda _: None)  # remembers turn 1

    a.forget()
    a.ask("long", say=lambda _: None)
    for i in range(a.HISTORY_TURNS + 4):
        a.ask(f"turn {i}", say=lambda _: None)
    prompts = [p.content for m in a._history for p in m.parts if isinstance(p, UserPromptPart)]
    assert len(prompts) == a.HISTORY_TURNS and prompts[-1] == f"turn {a.HISTORY_TURNS + 3}"

    a.forget()
    assert a._history == []


def resetter(messages, info):
    """Calls new_conversation when asked to start over."""
    if messages[-1].parts[-1].part_kind == "user-prompt" and messages[-1].parts[-1].content == "start over":
        return ModelResponse(parts=[ToolCallPart("new_conversation", {})])
    return ModelResponse(parts=[TextPart("ok")])


with a.agent.override(model=FunctionModel(resetter)):
    a.ask("make hello.py", say=lambda _: None)
    assert a._history
    a.ask("start over", say=lambda _: None)  # the agent chose to clear its own context
    assert a._history == []

# long tool output (e.g. an element list) is cut, the short action log survives untouched
long, short = "[0] button 'x'\n" * 100, "Typed 20 chars in window 7 (ghostty: ~)"
msgs: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart("hi")]),
        ModelRequest(parts=[ToolReturnPart("list_elements", long, "c1"), ToolReturnPart("type_text", short, "c2")])]
out = [p.content for m in a.compact(msgs) for p in m.parts if isinstance(p, ToolReturnPart)]
assert len(out[0]) < a.OLD_TOOL_CHARS + 20 and out[0].endswith("(trimmed)") and out[1] == short
assert msgs[1].parts[0].content == long  # original messages not mutated
print("ok")
