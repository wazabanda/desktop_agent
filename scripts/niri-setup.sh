#!/usr/bin/env bash
# Adds (or refreshes) a niri window-rule that floats the desktop-agent mic button at
# bottom center with a transparent background. Safe to re-run.
set -euo pipefail

config="${XDG_CONFIG_HOME:-$HOME/.config}/niri/config.kdl"
marker="// desktop-agent mic button"

cp "$config" "$config.bak"
# drop any previous version of the rule (marker comment through its closing brace)
sed -i "\|^$marker|,/^}/d" "$config"
cat >> "$config" <<EOF
$marker: float at bottom center, transparent, don't steal focus
window-rule {
    match app-id="^desktop-agent\$"
    open-floating true
    open-focused false
    default-floating-position x=0 y=20 relative-to="bottom"
    draw-border-with-background false
    focus-ring { off; }
    border { off; }
}
EOF

if ! niri validate -c "$config"; then
    mv "$config.bak" "$config"
    echo "niri rejected the config, restored backup" >&2
    exit 1
fi
echo "desktop-agent rule written to $config (backup at $config.bak)"
