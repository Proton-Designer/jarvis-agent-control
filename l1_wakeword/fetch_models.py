#!/usr/bin/env python3
"""Fetch the openWakeWord models this listener needs into ./models.

Run once after creating the venv and installing requirements.txt.
Model weights are gitignored (*.onnx) -- this script is how a fresh
checkout reproduces them.
"""
from pathlib import Path

from openwakeword.utils import download_models

TARGET = Path(__file__).parent / "models"

if __name__ == "__main__":
    TARGET.mkdir(exist_ok=True)
    download_models(["hey_jarvis"], target_directory=str(TARGET))
    print(f"Models fetched into {TARGET}")
