#!/bin/sh

# Remove only the files and service definitions owned by Fables.

set -eu

LABEL="com.srijanshukla.fables"
INSTALL_DIR="${FABLES_HOME:-$HOME/.local/share/fables}"
BIN_DIR="${FABLES_BIN_DIR:-$HOME/.local/bin}"
PLATFORM="${FABLES_PLATFORM:-$(uname -s)}"

case "$PLATFORM" in
    Darwin)
        PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
        launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
        [ ! -f "$PLIST" ] || rm -f "$PLIST"
        ;;
    Linux)
        UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
        UNIT="$UNIT_DIR/fables.service"
        systemctl --user disable --now fables.service >/dev/null 2>&1 || true
        [ ! -f "$UNIT" ] || rm -f "$UNIT"
        systemctl --user daemon-reload >/dev/null 2>&1 || true
        rmdir "$UNIT_DIR" >/dev/null 2>&1 || true
        ;;
    *)
        printf 'Fables uninstaller: unsupported operating system: %s\n' "$PLATFORM" >&2
        exit 1
        ;;
esac

CONTROL="$BIN_DIR/fables"
if [ -f "$CONTROL" ] && grep -q '^# Fables control command$' "$CONTROL"; then
    rm -f "$CONTROL"
fi

for file in \
    "$INSTALL_DIR/serve.py" \
    "$INSTALL_DIR/providers.py" \
    "$INSTALL_DIR/fables-cli.py" \
    "$INSTALL_DIR/fables_library.py" \
    "$INSTALL_DIR/fables-mcp.py" \
    "$INSTALL_DIR/fables-mcp.ts" \
    "$INSTALL_DIR/install-mcp.py" \
    "$INSTALL_DIR/mcp_protocol.py" \
    "$INSTALL_DIR/index.html" \
    "$INSTALL_DIR/fables.css" \
    "$INSTALL_DIR/fables-core.js" \
    "$INSTALL_DIR/fables-app.js" \
    "$INSTALL_DIR/fables-worker.js" \
    "$INSTALL_DIR/skills/fables/SKILL.md" \
    "$INSTALL_DIR/.port" \
    "$INSTALL_DIR/install.sh" \
    "$INSTALL_DIR/uninstall.sh"
do
    [ ! -f "$file" ] || rm -f "$file"
done
rmdir "$INSTALL_DIR/skills/fables" >/dev/null 2>&1 || true
rmdir "$INSTALL_DIR/skills" >/dev/null 2>&1 || true
rm -rf "$INSTALL_DIR/__pycache__"
rmdir "$INSTALL_DIR" >/dev/null 2>&1 || true

printf 'Fables has been uninstalled.\n'
if [ -f "$INSTALL_DIR/library.db" ] || [ -d "$INSTALL_DIR/objects" ] || [ -d "$INSTALL_DIR/imports" ]; then
    printf 'The durable session library was preserved at %s\n' "$INSTALL_DIR"
fi
if [ "$PLATFORM" = "Darwin" ]; then
    printf 'Logs were kept in %s\n' "$HOME/Library/Logs/Fables"
fi
