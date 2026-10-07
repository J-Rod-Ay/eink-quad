// Copy to secrets.h (which is gitignored) and fill in.
#pragma once

// Where your quad server lives. HTTPS on 443 is assumed (see main.cpp).
#define NOTE_HOST "your.server.example"
#define NOTE_PORT 443
#define NOTE_PATH "/quad/api/device/poll"
#define NOTE_TOKEN "change-me"          // must match QUAD_TOKEN on the server

// Optional: seeded into NVS on first boot only if no network is stored.
// Leave empty and use the setup portal instead (hold right button, tap left).
#define DEV_WIFI_SSID ""
#define DEV_WIFI_PASS ""

#define FW_BUILD "quad"
