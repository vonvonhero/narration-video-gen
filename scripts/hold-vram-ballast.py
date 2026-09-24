#!/usr/bin/env python3
"""Hold an explicit CUDA allocation; never launch generation or tune its settings.

Read hardware settings from a profile-shaped YAML candidate. --check needs no
PyTorch/GPU. Start an allocation only when no other generation is active, and
keep this process alive through all measured stages.
"""
import argparse
import json
from pathlib import Path
import time


def read_settings(path):
    import yaml
    profile = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    settings = profile.get("settings", {})
    mib = settings.get("vram_ballast_mib")
    device_index = settings.get("vram_ballast_device_index", 0)
    if type(mib) is not int or mib not in (4096, 8192):
        raise ValueError("settings.vram_ballast_mib must be 4096 or 8192")
    if type(device_index) is not int or device_index < 0:
        raise ValueError("settings.vram_ballast_device_index must be a nonnegative integer")
    return {"profile": profile["id"], "mib": mib,
            "bytes": mib * 1024 * 1024, "device_index": device_index}


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def hold(config):
    import torch
    device = torch.device("cuda", config["device_index"])
    torch.cuda.set_device(device)
    free_before, total = torch.cuda.mem_get_info(device)
    if free_before <= config["bytes"]:
        raise RuntimeError("insufficient free VRAM for ballast; do not evict an active run")
    # uint8 makes the element count exactly equal to the requested byte count.
    # Filling and synchronizing materializes the allocation before readiness.
    ballast = torch.empty(config["bytes"], dtype=torch.uint8, device=device)
    ballast.fill_(0)
    torch.cuda.synchronize(device)
    free_after, _ = torch.cuda.mem_get_info(device)
    emit("ready", **config, gpu=torch.cuda.get_device_name(device),
         total_bytes=total, free_before_bytes=free_before,
         free_after_bytes=free_after,
         allocated_bytes=torch.cuda.memory_allocated(device),
         reserved_bytes=torch.cuda.memory_reserved(device))
    try:
        while True:
            time.sleep(10)
            free_now, _ = torch.cuda.mem_get_info(device)
            emit("held", profile=config["profile"], free_bytes=free_now,
                 ballast_bytes=ballast.numel() * ballast.element_size(),
                 allocated_bytes=torch.cuda.memory_allocated(device))
    finally:
        del ballast
        torch.cuda.empty_cache()
        emit("released", profile=config["profile"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile-file", required=True)
    parser.add_argument("--check", action="store_true",
                        help="validate hardware settings without CUDA allocation")
    args = parser.parse_args()
    config = read_settings(args.profile_file)
    if args.check:
        emit("checked-not-allocated", **config)
        return
    try:
        hold(config)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
