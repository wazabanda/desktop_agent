# Run: uv run python tests/test_find_app.py
from pathlib import Path

from desktop_agent.agent import _norm, find_app


def apps(*titles):
    return {_norm(t): (t, Path(f"{t}.desktop")) for t in titles}


names = apps("Visual Studio Code", "Text Editor", "Firefox", "Avahi SSH Server Browser", "Avahi VNC Server Browser")
aliases = {"code": names["texteditor"], "vscode": names["visualstudiocode"], "browser": names["firefox"]}


def found(q):
    return [t for t, _ in find_app(q, names, aliases)]


assert found("fire fox") == ["Firefox"]  # exact name, spacing ignored
assert found("code") == ["Visual Studio Code"]  # unique name substring beats a keyword alias
assert found("VS code.") == ["Visual Studio Code"]  # alias
assert found("browser") == ["Firefox"]  # alias beats ambiguous name substrings
assert found("server browser") == ["Avahi SSH Server Browser", "Avahi VNC Server Browser"]  # ambiguous
assert found("firefix") == ["Firefox"]  # fuzzy
assert found("") == [] and found("zzz") == []
print("ok")
