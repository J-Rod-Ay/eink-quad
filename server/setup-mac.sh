#!/usr/bin/env bash
# One-step setup of the quad server on a Mac (tested target: an always-on Mac mini).
#
#   ./setup-mac.sh              everything: venv, keys, certificate, config, launchd service
#   ./setup-mac.sh --no-service the same without launchd, sleep or firewall changes (try it first)
#
# Safe to run again: existing keys, certificate and quad.env are kept, so the board keeps working.
# It writes ../firmware/src/secrets.h for the board, so nothing has to be copied by hand.
set -euo pipefail

SERVICE=1
[ "${1:-}" = "--no-service" ] && SERVICE=0

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="$HERE/quad.env"
TLS="$HERE/tls"
SECRETS="$ROOT/firmware/src/secrets.h"
LABEL="com.quadboard.server"
PLIST="/Library/LaunchDaemons/$LABEL.plist"
DEVICE_PORT=8743

say()  { printf '\n== %s\n' "$*"; }
stop() { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }

[ "$(uname)" = "Darwin" ] || stop "this script is for macOS"
case "$ROOT" in
  "$HOME/Desktop"*|"$HOME/Documents"*|"$HOME/Downloads"*)
    stop "move the repo out of Desktop/Documents/Downloads first (macOS privacy rules stop a background service from reading there). For example: mv \"$ROOT\" ~/eink-quad" ;;
esac
command -v python3 >/dev/null || stop "python3 not found (xcode-select --install, or brew install python)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || stop "python 3.9 or newer is needed"

say "Python environment"
[ -x "$HERE/.venv/bin/python" ] || python3 -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" -q install --upgrade pip
"$HERE/.venv/bin/pip" -q install -r "$HERE/requirements.txt"
PY="$HERE/.venv/bin/python"

say "Names"
HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
HOST_LC="$(printf '%s' "$HOST" | tr '[:upper:]' '[:lower:]')"
LAN_IP=""
for ifc in en0 en1 en2 en3; do
  LAN_IP="$(ipconfig getifaddr "$ifc" 2>/dev/null || true)"
  [ -n "$LAN_IP" ] && break
done
echo "Bonjour name: $HOST_LC.local    LAN address: ${LAN_IP:-unknown}"
[ -n "$LAN_IP" ] || echo "(No LAN address found. The board will rely on the Bonjour name only.)"

say "Keys (kept if they already exist)"
token() { "$PY" -c 'import secrets; print(secrets.token_urlsafe(32))'; }
get() { [ -f "$ENV_FILE" ] && sed -n "s/^$1=//p" "$ENV_FILE" | head -1 || true; }
DEVICE_TOKEN="$(get QUAD_TOKEN)"; [ ${#DEVICE_TOKEN} -ge 24 ] || DEVICE_TOKEN="$(token)"
ADMIN_TOKEN="$(get QUAD_ADMIN)";  [ ${#ADMIN_TOKEN} -ge 24 ] || ADMIN_TOKEN="$(token)"
CONTACT="$(get QUAD_CONTACT)"
if [ -z "$CONTACT" ]; then
  read -r -p "Email for api.weather.gov's User-Agent (they ask for a contact; optional): " CONTACT || true
fi
TZ_NAME="$(get TZ)"
[ -n "$TZ_NAME" ] || TZ_NAME="$(readlink /etc/localtime | sed 's#.*/zoneinfo/##')"

say "Certificate (kept if it already exists)"
mkdir -p "$TLS"
chmod 700 "$TLS"
if [ ! -f "$TLS/quad.crt" ] || [ ! -f "$TLS/quad.key" ]; then
  SAN="DNS:$HOST_LC.local"
  [ -n "$LAN_IP" ] && SAN="$SAN,IP:$LAN_IP"
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
    -subj "/CN=$HOST_LC.local" -addext "subjectAltName=$SAN" \
    -keyout "$TLS/quad.key" -out "$TLS/quad.crt" 2>/dev/null
  chmod 600 "$TLS/quad.key"
  echo "made a new certificate for $SAN (valid 10 years)"
fi
FP="$(openssl x509 -in "$TLS/quad.crt" -noout -fingerprint -sha256 | sed 's/.*=//; s/://g' | tr '[:upper:]' '[:lower:]')"
echo "certificate fingerprint (the board checks this): $FP"

say "quad.env"
# Settings this script doesn't manage (QUAD_CALENDAR_ICS, CLAUDE_CODE_OAUTH_TOKEN, ...) are kept.
EXTRA=""
if [ -f "$ENV_FILE" ]; then
  EXTRA="$(grep -vE '^(#|$|(TZ|QUAD_TOKEN|QUAD_ADMIN|QUAD_CONTACT|QUAD_BIND|QUAD_PORT|QUAD_DEVICE_BIND|QUAD_DEVICE_PORT|QUAD_CERT|QUAD_KEY)=)' "$ENV_FILE" || true)"
fi
umask 077
cat > "$ENV_FILE" <<EOF
# Written by setup-mac.sh. One KEY=VALUE per line, no comments after values.
TZ=$TZ_NAME
QUAD_TOKEN=$DEVICE_TOKEN
QUAD_ADMIN=$ADMIN_TOKEN
QUAD_CONTACT=$CONTACT
QUAD_BIND=127.0.0.1
QUAD_PORT=8740
QUAD_DEVICE_BIND=0.0.0.0
QUAD_DEVICE_PORT=$DEVICE_PORT
QUAD_CERT=$TLS/quad.crt
QUAD_KEY=$TLS/quad.key
EOF
[ -n "$EXTRA" ] && printf '%s\n' "$EXTRA" >> "$ENV_FILE"
umask 022
echo "wrote $ENV_FILE (readable only by you)"

say "firmware/src/secrets.h"
WIFI_SSID=""; WIFI_PASS=""
if [ -f "$SECRETS" ]; then
  WIFI_SSID="$(sed -n 's/^#define DEV_WIFI_SSID "\(.*\)".*/\1/p' "$SECRETS")"
  WIFI_PASS="$(sed -n 's/^#define DEV_WIFI_PASS "\(.*\)".*/\1/p' "$SECRETS")"
fi
umask 077
cat > "$SECRETS" <<EOF
// Written by server/setup-mac.sh -- run it again rather than editing these by hand.
#pragma once

// The server, found by its Bonjour name; SERVER_IP is the fallback if that lookup fails.
#define SERVER_MDNS "$HOST_LC"
#define SERVER_IP "$LAN_IP"
#define SERVER_PORT $DEVICE_PORT
// SHA-256 of the server's certificate. The board refuses any other server.
#define SERVER_FP "$FP"
// Sent as "Authorization: Bearer ..."; must match QUAD_TOKEN in server/quad.env.
#define DEVICE_TOKEN "$DEVICE_TOKEN"

// Optional: seeded into NVS on first boot only if no network is stored.
// Leave empty and use the setup portal instead (hold the left button, tap the right).
#define DEV_WIFI_SSID "$WIFI_SSID"
#define DEV_WIFI_PASS "$WIFI_PASS"

#define FW_BUILD "quad-2"
EOF
umask 022
echo "wrote $SECRETS (gitignored). Build and flash the firmware after this."

if [ "$SERVICE" = 1 ]; then
  say "Background service (needs your password once)"
  LOGS="$HERE/logs"; mkdir -p "$LOGS"
  TMP_PLIST="$(mktemp)"
  cat > "$TMP_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>UserName</key><string>$(id -un)</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$HERE/app.py</string></array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>QUAD_ENV</key><string>$ENV_FILE</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/server.log</string>
  <key>StandardErrorPath</key><string>$LOGS/server.log</string>
</dict>
</plist>
EOF
  plutil -lint "$TMP_PLIST" >/dev/null
  sudo launchctl bootout system "$PLIST" 2>/dev/null || true
  sudo install -m 644 -o root -g wheel "$TMP_PLIST" "$PLIST"
  rm -f "$TMP_PLIST"
  sudo launchctl bootstrap system "$PLIST"
  echo "service $LABEL runs at boot as $(id -un) (not root), restarts if it stops; log: $LOGS/server.log"

  say "Keep the Mac awake and on after a power cut"
  sudo pmset -a sleep 0 autorestart 1
  echo "system sleep off (the display may still sleep), restarts after power failure"

  FW=/usr/libexec/ApplicationFirewall/socketfilterfw
  if [ -x "$FW" ] && "$FW" --getglobalstate | grep -q enabled; then
    say "macOS firewall: allow the server's Python"
    REAL_PY="$("$PY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
    sudo "$FW" --add "$REAL_PY" >/dev/null
    sudo "$FW" --unblockapp "$REAL_PY" >/dev/null
    echo "allowed incoming connections for $REAL_PY"
  fi

  say "Check"
  ok=0
  for _ in $(seq 1 20); do
    if curl -s --max-time 3 --cacert "$TLS/quad.crt" --resolve "$HOST_LC.local:$DEVICE_PORT:127.0.0.1" \
        "https://$HOST_LC.local:$DEVICE_PORT/healthz" | grep -q ok; then ok=1; break; fi
    sleep 1
  done
  [ "$ok" = 1 ] && echo "device port answers with the pinned certificate: OK" \
                || echo "device port not answering yet; see $LOGS/server.log"
fi

say "Done"
echo "Settings page (on this Mac):  http://127.0.0.1:8740/?k=$ADMIN_TOKEN"
echo "From another computer:        ssh -L 8740:127.0.0.1:8740 $(id -un)@$HOST_LC.local, then open the same URL"
[ "$SERVICE" = 0 ] && echo "Start it by hand:              $PY $HERE/app.py"
echo "Tip: give this Mac a fixed address in your router (DHCP reservation) so SERVER_IP stays right."
