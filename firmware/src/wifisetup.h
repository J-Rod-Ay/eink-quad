#pragma once
#include <Arduino.h>

// Credentials live in NVS, not in the build. The display is a gift that lives
// in someone else's house: the person setting it up has a phone and no laptop,
// so joining a new network has to be possible from the device itself.
//
// Adapted from ../../flightwall-tui/firmware/src/wifisetup.cpp, with one
// design change that matters here -- the verdict is reported on the e-paper
// panel, not back to the phone. See runPortal().

namespace wifisetup {

// True when a network has been stored. Outputs are left alone when false.
bool load(String *ssid, String *pass);

// Join the stored network. False on no credentials or timeout.
bool connect(uint32_t timeoutMs);

// Store a network and mark it unproven, so the next boot knows a failure
// means "you typed the password wrong" rather than "the router is down".
void save(const String &ssid, const String &pass);

void forget();

// Saved but never successfully joined yet.
bool isFresh();
void clearFresh();

// Soft-AP name, derived from the MAC so two devices never collide.
const char *apName();

// Serve the setup page until someone submits a network (in which case this
// reboots and never returns) or nobody does for idleTimeoutMs (in which case
// it returns and the caller should sleep).
void runPortal(uint32_t idleTimeoutMs);

}  // namespace wifisetup
