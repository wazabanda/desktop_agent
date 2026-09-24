# Run: uv run python tests/test_wake.py
from desktop_agent.main import WakeQueue

q = WakeQueue("hi bits")
assert not q.heard("so I was saying that")
assert q.heard("Hi, Bits!")  # punctuation/case from whisper
assert not q.heard("")  # fired once, queue cleared

q = WakeQueue("hi bits")
assert not q.heard("okay hi")
assert q.heard("bits open firefox")  # phrase split across two listening windows

for heard in ("hibits", "Hi Bitz.", "hi bit s", "Hi, bits, what's up"):  # whisper spelling variants
    assert WakeQueue("hi bits").heard(heard), heard
for heard in ("hello there", "his bills are due", "the kids"):
    assert not WakeQueue("hi bits").heard(heard), heard

assert WakeQueue("hey desktop agent").heard("Hey, desktop agent.")  # longer phrase
assert not WakeQueue("hey desktop agent").heard("the desktop is fine")
print("ok")
