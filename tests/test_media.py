# Run: uv run python tests/test_media.py
from desktop_agent.desktop import _choose

ps = {"firefox.instance_1_42": "Paused", "spotify": "Playing", "vlc": "Stopped"}
assert _choose(ps, "") == "spotify"
assert _choose({"a": "Stopped", "b": "Paused"}, "") == "b"
assert _choose(ps, "Spotify") == "spotify" and _choose(ps, "fire") == "firefox.instance_1_42"
for players, q in (({}, ""), (ps, "mpv")):
    try:
        _choose(players, q)
        raise AssertionError(f"{q!r} should fail")
    except LookupError:
        pass
print("ok")
