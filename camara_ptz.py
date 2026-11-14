#!/usr/bin/env python3
"""SARI Eyes node entry point (vision, PTZ tracking, alerts, authenticated MJPEG stream).

All logic lives in the sari_eyes package; see sari_eyes/app.py.
"""
from sari_eyes.app import main

if __name__ == "__main__":
    main()
