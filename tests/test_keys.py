# Run: uv run python tests/test_keys.py
from evdev import ecodes as e

from desktop_agent.desktop import _char, _key

assert _char("a") == (e.KEY_A, False) and _char("A") == (e.KEY_A, True)
assert _char("1") == (e.KEY_1, False) and _char("!") == (e.KEY_1, True)
assert _char("~") == (e.KEY_GRAVE, True) and _char("/") == (e.KEY_SLASH, False)
assert _char(" ") == (e.KEY_SPACE, False) and _char("\n") == (e.KEY_ENTER, False)
assert _key("ctrl") == e.KEY_LEFTCTRL and _key("Enter") == e.KEY_ENTER and _key("super") == e.KEY_LEFTMETA
assert _key("l") == e.KEY_L and _key("f5") == e.KEY_F5 and _key("pgdn") == e.KEY_PAGEDOWN
for bad in ("é", "€"):
    try:
        _char(bad)
        raise AssertionError(f"{bad!r} should be rejected")
    except ValueError:
        pass

# typing aborts once focus leaves the target window
import desktop_agent.desktop as d  # noqa: E402

focus, typed = iter(['{"id": 1}', '{"id": 2}']), []
d._niri = lambda *a: next(focus)
d._combo = typed.append
try:
    d._type("abcdefgh", {"id": 1, "app_id": "x", "title": "t"})
    raise AssertionError("should stop when focus moves")
except RuntimeError as err:
    assert "after 4 chars" in str(err) and len(typed) == d.FOCUS_CHECK_EVERY, (err, typed)
print("ok")
