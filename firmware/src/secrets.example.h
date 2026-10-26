// server/setup-mac.sh writes secrets.h (gitignored) with real values -- use that.
// This file shows the shape only.
#pragma once

// The server, found by its Bonjour name; SERVER_IP is the fallback if that lookup fails.
#define SERVER_MDNS "your-mac-mini"     // without .local
#define SERVER_IP ""                     // e.g. "192.168.1.20", or empty
#define SERVER_PORT 8743
// SHA-256 of the server's certificate (64 hex characters). The board refuses any other server.
#define SERVER_FP "0000000000000000000000000000000000000000000000000000000000000000"
// Sent as "Authorization: Bearer ..."; must match QUAD_TOKEN in server/quad.env.
#define DEVICE_TOKEN "change-me"

// Optional: seeded into NVS on first boot only if no network is stored.
// Leave empty and use the setup portal instead (hold the left button, tap the right).
#define DEV_WIFI_SSID ""
#define DEV_WIFI_PASS ""

#define FW_BUILD "quad-2"
