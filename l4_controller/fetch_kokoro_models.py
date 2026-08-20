#!/usr/bin/env python3
"""Fetch the Kokoro-82M ONNX model + voice pack into ./models.

Run once after creating l4_controller/.venv and installing
requirements.txt (same convention as l1_wakeword/fetch_models.py).
Model weights are gitignored (*.onnx, *.bin) -- this script is how a
fresh checkout reproduces them. One-time: ~354MB total
(kokoro-v1.0.onnx ~325MB, voices-v1.0.bin ~28MB, all 54 voices bundled
in the one voices file, not fetched separately per voice). No repeat
download at runtime -- kokoro_tts.py refuses (and falls back to `say`,
audibly/logged) rather than fetching anything mid-conversation.
"""
from pathlib import Path
from urllib.request import urlretrieve

TARGET = Path(__file__).parent / "models"

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
FILES = {
    "kokoro-v1.0.onnx": f"{RELEASE}/kokoro-v1.0.onnx",
    "voices-v1.0.bin": f"{RELEASE}/voices-v1.0.bin",
}


def main() -> None:
    TARGET.mkdir(exist_ok=True)
    for name, url in FILES.items():
        dest = TARGET / name
        if dest.exists():
            print(f"already have {name} ({dest.stat().st_size:,} bytes) -- skipping")
            continue
        print(f"fetching {name} from {url} ...")
        urlretrieve(url, dest)
        print(f"  -> {dest} ({dest.stat().st_size:,} bytes)")
    print(f"Kokoro model files ready in {TARGET}")


if __name__ == "__main__":
    main()
