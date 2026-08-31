#!/bin/sh

# Install Fables as a per-user background service on macOS or Linux.
# Safe to run repeatedly: application files and service definitions are replaced
# in place, then the service is restarted with the current configuration.

set -eu

LABEL="com.srijanshukla.fables"
INSTALL_DIR="${FABLES_HOME:-$HOME/.local/share/fables}"
BIN_DIR="${FABLES_BIN_DIR:-$HOME/.local/bin}"
PORT="${FABLES_PORT:-8321}"
OPEN_AFTER_INSTALL=1
PLATFORM="${FABLES_PLATFORM:-$(uname -s)}"
SOURCE_DIR=$(CDPATH= cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)

usage() {
    cat <<'EOF'
Usage: ./install.sh [--port PORT] [--no-open]

Installs Fables for the current user and starts it at login.

Options:
  --port PORT  Listen on localhost:PORT (default: 8321)
  --no-open    Do not open Fables in a browser after installation
  -h, --help   Show this help
EOF
}

fail() {
    printf 'Fables installer: %s\n' "$1" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --port)
            [ "$#" -ge 2 ] || fail "--port needs a value"
            PORT=$2
            shift 2
            ;;
        --port=*)
            PORT=${1#*=}
            shift
            ;;
        --no-open)
            OPEN_AFTER_INSTALL=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

case "$PORT" in
    ''|*[!0-9]*) fail "port must be a number from 1 to 65535" ;;
esac
[ "$PORT" -ge 1 ] 2>/dev/null && [ "$PORT" -le 65535 ] 2>/dev/null \
    || fail "port must be a number from 1 to 65535"

case "$PLATFORM" in
    Darwin)
        command -v launchctl >/dev/null 2>&1 \
            || fail "launchctl is required on macOS"
        ;;
    Linux)
        command -v systemctl >/dev/null 2>&1 \
            || fail "systemd user services are required on Linux"
        systemctl --user show-environment >/dev/null 2>&1 \
            || fail "no systemd user session is available; run Fables manually with python3 serve.py"
        ;;
    *)
        fail "unsupported operating system: $PLATFORM (Fables supports macOS and Linux)"
        ;;
esac

PYTHON_BIN="${FABLES_PYTHON:-$(command -v python3 || true)}"
[ -n "$PYTHON_BIN" ] || fail "Python 3.10 or newer is required"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "Python 3.10 or newer is required (found: $PYTHON_BIN)"
PYTHON_BIN=$(CDPATH= cd -P "$(dirname "$PYTHON_BIN")" >/dev/null 2>&1 && pwd)/$(basename "$PYTHON_BIN")

for file in serve.py providers.py fables-mcp.py install-mcp.py fables-mcp.ts mcp_protocol.py index.html fables.css fables-core.js fables-app.js fables-worker.js uninstall.sh bin/fables; do
    [ -f "$SOURCE_DIR/$file" ] || fail "the source checkout is incomplete: missing $file"
done

mkdir -p "$INSTALL_DIR" "$BIN_DIR"

copy_file() {
    source_file=$1
    destination_file=$2
    mode=$3
    if [ "$source_file" = "$destination_file" ]; then
        chmod "$mode" "$destination_file"
    else
        install -m "$mode" "$source_file" "$destination_file"
    fi
}

for file in serve.py providers.py index.html fables.css fables-core.js fables-app.js fables-worker.js; do
    copy_file "$SOURCE_DIR/$file" "$INSTALL_DIR/$file" 0644
done
copy_file "$SOURCE_DIR/fables-mcp.py" "$INSTALL_DIR/fables-mcp.py" 0755
copy_file "$SOURCE_DIR/mcp_protocol.py" "$INSTALL_DIR/mcp_protocol.py" 0644
copy_file "$SOURCE_DIR/install-mcp.py" "$INSTALL_DIR/install-mcp.py" 0755
copy_file "$SOURCE_DIR/fables-mcp.ts" "$INSTALL_DIR/fables-mcp.ts" 0644
copy_file "$SOURCE_DIR/install.sh" "$INSTALL_DIR/install.sh" 0755
copy_file "$SOURCE_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh" 0755
copy_file "$SOURCE_DIR/bin/fables" "$BIN_DIR/fables" 0755
printf '%s\n' "$PORT" > "$INSTALL_DIR/.port"

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

systemd_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/%/%%/g'
}

if [ "$PLATFORM" = "Darwin" ]; then
    AGENT_DIR="$HOME/Library/LaunchAgents"
    LOG_DIR="$HOME/Library/Logs/Fables"
    PLIST="$AGENT_DIR/$LABEL.plist"
    mkdir -p "$AGENT_DIR" "$LOG_DIR"

    PYTHON_XML=$(xml_escape "$PYTHON_BIN")
    SERVER_XML=$(xml_escape "$INSTALL_DIR/serve.py")
    INSTALL_XML=$(xml_escape "$INSTALL_DIR")
    STDOUT_XML=$(xml_escape "$LOG_DIR/fables.log")
    STDERR_XML=$(xml_escape "$LOG_DIR/fables.error.log")
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_XML</string>
    <string>$SERVER_XML</string>
    <string>$PORT</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$INSTALL_XML</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>$STDOUT_XML</string>
  <key>StandardErrorPath</key>
  <string>$STDERR_XML</string>
</dict>
</plist>
EOF

    DOMAIN="gui/$(id -u)"
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    launchctl bootstrap "$DOMAIN" "$PLIST" \
        || fail "launchd could not load $PLIST"
else
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    UNIT="$UNIT_DIR/fables.service"
    mkdir -p "$UNIT_DIR"

    PYTHON_UNIT=$(systemd_escape "$PYTHON_BIN")
    SERVER_UNIT=$(systemd_escape "$INSTALL_DIR/serve.py")
    INSTALL_UNIT=$(systemd_escape "$INSTALL_DIR")
    cat > "$UNIT" <<EOF
[Unit]
Description=Fables local coding-agent session reader
After=default.target

[Service]
Type=simple
ExecStart="$PYTHON_UNIT" "$SERVER_UNIT" "$PORT"
WorkingDirectory="$INSTALL_UNIT"
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable fables.service \
        || fail "systemd could not enable the Fables user service"
    systemctl --user restart fables.service \
        || fail "systemd could not start the Fables user service"
fi

URL="http://localhost:$PORT"
printf '\nFables is installed and will run whenever you are logged in.\n'
printf 'Open:      %s\n' "$URL"
printf 'Control:   %s open|status|restart|logs|uninstall\n' "$BIN_DIR/fables"

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) printf 'PATH note: add %s to your PATH to run `fables` directly.\n' "$BIN_DIR" ;;
esac

if [ "$OPEN_AFTER_INSTALL" -eq 1 ]; then
    # Initial provider discovery can take a moment on large transcript stores.
    "$PYTHON_BIN" - "$PORT" <<'PY' || true
import sys
import time
import urllib.request

url = f"http://127.0.0.1:{sys.argv[1]}/"
for _ in range(60):
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            if response.status == 200:
                raise SystemExit(0)
    except Exception:
        time.sleep(0.25)
raise SystemExit(1)
PY
    if [ "$PLATFORM" = "Darwin" ]; then
        open "$URL" >/dev/null 2>&1 || true
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL" >/dev/null 2>&1 || true
    fi
fi
