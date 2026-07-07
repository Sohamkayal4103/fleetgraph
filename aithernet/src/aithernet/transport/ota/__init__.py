"""beta 1.0 OTA peer transport — SDR over-the-air carrier for canonical Aithernet peer envelopes.

This package adds SDR/RF as another *peer transport* beneath the existing peer-message and
MissionEngine layers. The logical message (a signed canonical MessageEnvelope) is identical across
loopback, IP, hosted relay, simulated RF, cabled SDR, and authorized OTA SDR. No mission logic lives
in the modem layer; the receiving node remains authoritative for all policy.
"""
