#!/usr/bin/env bash
# Adds (or refreshes) niri window-rules that float the desktop-agent mic button at
# bottom center with a transparent background, and its chat bubble just above it. Safe to re-run.
set -euo pipefail

config="${XDG_CONFIG_HOME:-$HOME/.config}/niri/config.kdl"
mic_marker="// desktop-agent mic button"
bubble_marker="// desktop-agent chat bubble"

cp "$config" "$config.bak"
# drop any previous version of the rules (marker comment through its closing brace)
for marker in "$mic_marker" "$bubble_marker"; do
    sed -i "\|^$marker|,/^}/d" "$config"
done
# the bubble rule comes last so its position overrides the mic rule, which also matches it
cat >> "$config" <<EOF
$mic_marker: float at bottom center, transparent, don't steal focus
window-rule {
    match app-id="^desktop-agent\$"
    open-floating true
    open-focused false
    default-floating-position x=0 y=20 relative-to="bottom"
    draw-border-with-background false
    focus-ring { off; }
    border { off; }
}
$bubble_marker: sits above the mic (mic is 56px tall at y=20)
window-rule {
    match app-id="^desktop-agent\$" title="^desktop-agent-bubble\$"
    default-floating-position x=0 y=88 relative-to="bottom"
}
EOF

if ! niri validate -c "$config"; then
    mv "$config.bak" "$config"
    echo "niri rejected the config, restored backup" >&2
    exit 1
fi
echo "desktop-agent rules written to $config (backup at $config.bak)"
