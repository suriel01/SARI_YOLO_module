#!/usr/bin/env python3
"""SARI Jetson telemetry agent entry point (see sari_eyes/telemetry.py, deploy/sari-jetson.service)."""
from sari_eyes.telemetry import main

if __name__ == "__main__":
    main()
