"""Desktop tools: windows via niri IPC, keyboard via a uinput device, app UIs via the AT-SPI tree."""

import json
import subprocess
import time
from urllib.parse import urlparse

import gi
from evdev import UInput, ecodes
from pydantic_ai.toolsets import FunctionToolset

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

toolset = FunctionToolset()
OURS = "desktop-agent"  # our own app-id: never a target for keys
KEY_DELAY = 0.008  # s between key events; raise if apps drop characters
MAX_ELEMENTS, MAX_VISITED = 150, 4000  # caps on list_elements output and tree walk cost


# --- niri windows -------------------------------------------------------------------------------


def _niri(*args: str) -> str:
    return subprocess.run(["niri", "msg", *args], capture_output=True, text=True, timeout=2, check=True).stdout


def _windows() -> list[dict]:
    return [w for w in json.loads(_niri("--json", "windows")) if w["app_id"] != OURS]


def _pick(window_id: int | None) -> dict:
    """The given window, else the most recently focused non-agent window."""
    wins = _windows()
    if window_id is not None:
        win = next((w for w in wins if w["id"] == window_id), None)
        if win is None:
            raise ValueError(f"no window {window_id}, call list_windows")
    else:
        # clicking the mic can leave focus on our own window, so pick by timestamp, not is_focused
        win = max(wins, key=lambda w: (w["focus_timestamp"] or {}).get("secs", 0) * 1e9
                  + (w["focus_timestamp"] or {}).get("nanos", 0))
    return win


def _target(window_id: int | None) -> dict:
    """Like _pick, then gives it keyboard focus so key events land there."""
    win = _pick(window_id)
    if not win["is_focused"]:
        _niri("action", "focus-window", "--id", str(win["id"]))
        time.sleep(0.15)  # let the compositor move keyboard focus before we type
    return win


def _label(win: dict) -> str:
    return f"window {win['id']} ({win['app_id']}: {win['title']})"  # ids let later requests refer back


@toolset.tool_plain
def list_windows() -> str:
    """List open windows as `id: app — title`, the focused one marked."""
    return "\n".join(
        f"{w['id']}: {w['app_id']} — {w['title']}{'  (focused)' if w['is_focused'] else ''}" for w in _windows()
    )


@toolset.tool_plain
def focus_window(window_id: int) -> str:
    """Bring a window (id from list_windows) to the front and give it keyboard focus."""
    return f"Focused {_label(_target(window_id))}"


@toolset.tool_plain
def open_url(url: str) -> str:
    """Open a web page in the default browser."""
    # trust boundary: xdg-open also runs local files/handlers, so only web links get through
    if urlparse(url).scheme not in ("http", "https"):
        url = "https://" + url
    subprocess.Popen(["xdg-open", url], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"Opened {url}"


# --- keyboard -----------------------------------------------------------------------------------

# ponytail: assumes the US xkb layout (niri config has none set); other layouts need a keymap lookup
SHIFTED = dict(zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./"))
PUNCT = {"`": "GRAVE", "-": "MINUS", "=": "EQUAL", "[": "LEFTBRACE", "]": "RIGHTBRACE", "\\": "BACKSLASH",
         ";": "SEMICOLON", "'": "APOSTROPHE", ",": "COMMA", ".": "DOT", "/": "SLASH", " ": "SPACE",
         "\n": "ENTER", "\t": "TAB"}
NAMES = {"ctrl": "LEFTCTRL", "control": "LEFTCTRL", "shift": "LEFTSHIFT", "alt": "LEFTALT",
         "super": "LEFTMETA", "meta": "LEFTMETA", "win": "LEFTMETA", "return": "ENTER", "escape": "ESC",
         "del": "DELETE", "pgup": "PAGEUP", "pgdn": "PAGEDOWN", "arrowup": "UP", "arrowdown": "DOWN",
         "arrowleft": "LEFT", "arrowright": "RIGHT"}
_ui: UInput | None = None


def _dev() -> UInput:
    global _ui
    if _ui is None:
        keys = [c for n, c in ecodes.ecodes.items() if n.startswith("KEY_") and c < ecodes.KEY_MAX]
        _ui = UInput({ecodes.EV_KEY: keys}, name="desktop-agent-keyboard")
        time.sleep(0.5)  # compositor needs a moment to pick up a new input device
    return _ui


def _char(c: str) -> tuple[int, bool]:
    """(keycode, needs shift) for one character on a US layout."""
    shift = c in SHIFTED or c.isupper()
    c = SHIFTED.get(c, c.lower())
    name = PUNCT.get(c) or (c.upper() if c.isascii() and c.isalnum() else None)
    if name is None:
        raise ValueError(f"can't type {c!r} (US layout, ASCII only)")
    return ecodes.ecodes[f"KEY_{name}"], shift


def _key(name: str) -> int:
    name = name.strip().lower()
    if len(name) == 1:
        return _char(name)[0]
    code = ecodes.ecodes.get(f"KEY_{NAMES.get(name, name).upper()}")
    if not isinstance(code, int):
        raise ValueError(f"unknown key {name!r}")
    return code


def _combo(codes: list[int]) -> None:
    """Press codes in order, release in reverse: [ctrl, l] is ctrl+l."""
    ui = _dev()
    for c in codes:
        ui.write(ecodes.EV_KEY, c, 1)
        ui.syn()
        time.sleep(KEY_DELAY)
    for c in reversed(codes):
        ui.write(ecodes.EV_KEY, c, 0)
        ui.syn()
        time.sleep(KEY_DELAY)


def _type(text: str) -> None:
    chars = [_char(c) for c in text]  # validate everything before typing anything
    for code, shift in chars:
        _combo([ecodes.KEY_LEFTSHIFT, code] if shift else [code])


@toolset.tool_plain
def press_keys(keys: str, window_id: int | None = None) -> str:
    """Press key combos in a window, e.g. "ctrl+l", "ctrl+shift+t", "alt+left", "enter", or several
    separated by spaces: "ctrl+a delete". Default window: the one the user last used."""
    combos = [[_key(k) for k in combo.split("+")] for combo in keys.split()]
    win = _target(window_id)
    for codes in combos:
        _combo(codes)
    return f"Pressed {keys} in {_label(win)}"


@toolset.tool_plain
def type_text(text: str, window_id: int | None = None) -> str:
    """Type text into whatever has keyboard focus in a window. Default window: the one the user last used."""
    win = _target(window_id)
    _type(text)
    return f"Typed {len(text)} chars in {_label(win)}"


# --- accessibility tree -------------------------------------------------------------------------

_last: tuple[int, list[Atspi.Accessible]] = (0, [])  # (window id, elements) from the last list_elements


def enable_accessibility() -> None:
    """Ask apps to expose their UI over AT-SPI (what a screen reader does). Browsers read it at startup."""
    subprocess.run(["busctl", "--user", "set-property", "org.a11y.Bus", "/org/a11y/bus",
                    "org.a11y.Status", "IsEnabled", "b", "true"], capture_output=True, timeout=2)


def _frame(win: dict) -> Atspi.Accessible:
    desktop = Atspi.get_desktop(0)
    apps = [a for i in range(desktop.get_child_count())
            if (a := desktop.get_child_at_index(i)) and a.get_process_id() == win["pid"]]
    frames = [f for a in apps for i in range(a.get_child_count()) if (f := a.get_child_at_index(i))]
    if not frames:
        raise LookupError(f"{win['app_id']} exposes no accessibility tree; it may need a restart "
                          "(browsers check at startup), or it doesn't support AT-SPI")
    # several windows per process (Firefox, VS Code): match by title, else the active one
    return (next((f for f in frames if f.get_name() == win["title"]), None)
            or next((f for f in frames if f.get_state_set().contains(Atspi.StateType.ACTIVE)), frames[0]))


def _describe(el: Atspi.Accessible) -> str:
    line = f"{el.get_role_name()} {el.get_name()!r}"
    if el.is_editable_text():
        text = Atspi.Text.get_text(el, 0, 60) if el.is_text() else ""
        line += f" = {text!r}"
    return line


# GTK4 puts actions on frames/panels/labels too; only these roles are worth showing the model
CONTROLS = {"push button", "toggle button", "button", "check box", "radio button", "link", "menu", "menu item",
            "check menu item", "radio menu item", "page tab", "combo box", "list item", "tree item",
            "table row", "table cell", "spin button", "slider", "icon", "switch"}


def _interesting(el: Atspi.Accessible, states: Atspi.StateSet) -> bool:
    if el.is_editable_text() and states.contains(Atspi.StateType.EDITABLE):
        return True
    # is_action() first: LibreOffice raises on get_n_actions() for elements without the interface
    return el.is_action() and bool(el.get_name()) and el.get_role_name() in CONTROLS


@toolset.tool_plain
def list_elements(window_id: int | None = None, query: str = "") -> str:
    """List clickable/editable elements (buttons, links, fields, tabs...) of a window as `[n] role 'name'`.
    Use `query` to keep only elements whose name contains it. Numbers are for click_element/type_into."""
    global _last
    win = _pick(window_id)  # read-only: don't steal focus
    root, q = _frame(win), query.lower()
    found, stack, visited = [], [root], 0
    # ponytail: plain DFS with caps; AT-SPI Collection.get_matches would be one call but GTK4 lacks it
    while stack and visited < MAX_VISITED and len(found) < MAX_ELEMENTS:
        el = stack.pop()
        visited += 1
        try:
            states = el.get_state_set()
            if el is not root and not states.contains(Atspi.StateType.SHOWING):
                continue  # offscreen subtree: skip it whole
            stack.extend(el.get_child_at_index(i) for i in reversed(range(el.get_child_count())))
            if _interesting(el, states) and q in (el.get_name() or "").lower():
                found.append(el)
        except GLib.Error:
            continue  # element vanished mid-walk (page changed) or half-implements an interface
    _last = (win["id"], found)
    lines = [f"[{i}] {_describe(el)}" for i, el in enumerate(found)]
    if len(found) == MAX_ELEMENTS or visited == MAX_VISITED:
        lines.append("(truncated: pass a query to narrow down)")
    return "\n".join(lines) or f"No matching elements in {win['title']}"


def _element(n: int) -> Atspi.Accessible:
    if not 0 <= n < len(_last[1]):
        raise ValueError(f"no element [{n}], call list_elements again")
    return _last[1][n]


@toolset.tool_plain
def click_element(n: int) -> str:
    """Click/press/activate element [n] from the last list_elements (buttons, links, tabs, menu items)."""
    el = _element(n)
    _target(_last[0])
    if el.is_action() and el.get_n_actions() > 0:
        el.do_action(0)  # "click", "press", "jump"... whatever the element's primary action is
    else:
        el.grab_focus()
        _combo([ecodes.KEY_ENTER])
    return f"Activated {_describe(el)}"


@toolset.tool_plain
def type_into(n: int, text: str, replace: bool = True) -> str:
    """Focus editable element [n] from the last list_elements and type text into it (replacing its content
    by default). Press enter afterwards with press_keys if the text should be submitted."""
    el = _element(n)
    _target(_last[0])
    el.grab_focus()
    time.sleep(0.05)
    # real keystrokes, not EditableText.set_text_contents: web apps (React etc.) ignore silent value changes
    if replace:
        _combo([ecodes.KEY_LEFTCTRL, ecodes.KEY_A])
    _type(text)
    return f"Typed into {_describe(el)}"
