# Run: uv run python tests/test_read_file.py
import tempfile
from pathlib import Path

from desktop_agent.agent import WEB_CHARS, read_file

d = Path(tempfile.mkdtemp())
(d / "sub").mkdir()
(d / "a.txt").write_text("x" * (WEB_CHARS + 10))
assert read_file(str(d)) == "a.txt\nsub/"  # folder -> listing
first = read_file(str(d / "a.txt"))
assert first.startswith("x" * WEB_CHARS) and f"offset={WEB_CHARS}" in first  # long file is paged
assert read_file(str(d / "a.txt"), WEB_CHARS) == "x" * 10
assert read_file(str(d / "missing")).startswith("Can't read")
print("ok")
