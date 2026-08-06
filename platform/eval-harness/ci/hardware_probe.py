"""Report local training feasibility inputs without starting a trainer."""
from __future__ import annotations

import json
import platform
import shutil
import sys


def probe() -> dict:
    result = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "ollama_available": shutil.which("ollama") is not None,
        "cuda_available": False,
        "mps_available": False,
    }
    try:
        import torch
        result["cuda_available"] = bool(torch.cuda.is_available())
        result["mps_available"] = bool(hasattr(torch.backends, "mps") and torch.backends.mps.is_available())
    except ImportError:
        result["torch_installed"] = False
    return result


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False))
