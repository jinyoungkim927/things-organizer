#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USER_HOME="$HOME"
USERNAME="$(whoami)"
PLIST_NAME="com.things-organizer.plist"

echo "=== Things 3 Auto-Organizer Setup ==="
echo ""

# 1. Create venv and install deps
echo "[1/4] Setting up Python environment..."
python3 -m venv "$SCRIPT_DIR/.venv"
"$SCRIPT_DIR/.venv/bin/pip" install --quiet anthropic
echo "  Done."

# 2. Check for .env
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo ""
    echo "[2/4] Creating .env file..."
    read -p "  Anthropic API key: " API_KEY
    read -p "  Things URL scheme auth token (Settings > General > Enable Things URLs): " AUTH_TOKEN
    cat > "$SCRIPT_DIR/.env" <<EOF
ANTHROPIC_API_KEY=$API_KEY
THINGS_AUTH_TOKEN=$AUTH_TOKEN
EOF
    echo "  Saved to .env"
else
    echo "[2/4] .env already exists — skipping."
fi

# 3. Find Things database
echo ""
echo "[3/4] Locating Things database..."
THINGS_DB=$(find "$USER_HOME/Library/Group Containers" -name "main.sqlite-wal" -path "*ThingsMac*" 2>/dev/null | head -1)
if [ -z "$THINGS_DB" ]; then
    echo "  ERROR: Could not find Things 3 database. Is Things installed?"
    exit 1
fi
echo "  Found: $THINGS_DB"

# 4. Install launchd agent
echo ""
echo "[4/4] Installing launchd agent..."
mkdir -p "$SCRIPT_DIR/logs"

cat > "$USER_HOME/Library/LaunchAgents/$PLIST_NAME" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.things-organizer</string>
    <key>ProgramArguments</key>
    <array>
        <string>$SCRIPT_DIR/.venv/bin/python</string>
        <string>$SCRIPT_DIR/organize_things.py</string>
    </array>
    <key>WatchPaths</key>
    <array>
        <string>$THINGS_DB</string>
    </array>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/logs/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/logs/launchd-stderr.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl unload "$USER_HOME/Library/LaunchAgents/$PLIST_NAME" 2>/dev/null || true
launchctl load "$USER_HOME/Library/LaunchAgents/$PLIST_NAME"

echo "  Installed and started."
echo ""
echo "=== Setup complete! ==="
echo ""
echo "The organizer will now run automatically whenever you edit tasks in Things."
echo ""
echo "Useful commands:"
echo "  Dry run:    $SCRIPT_DIR/.venv/bin/python $SCRIPT_DIR/organize_things.py --dry-run --force"
echo "  View logs:  tail -f $SCRIPT_DIR/logs/launchd-stdout.log"
echo "  Stop:       launchctl unload ~/Library/LaunchAgents/$PLIST_NAME"
echo "  Restart:    launchctl load ~/Library/LaunchAgents/$PLIST_NAME"
