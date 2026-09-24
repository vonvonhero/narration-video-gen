[日本語](troubleshooting.md) | **English**

# Troubleshooting

When a generation fails, start with `./bin/narration-video-gen status`, which
shows the cause and suggested fixes. The full log is `outputs/<run-id>/run.log`.

## The chosen configuration cannot run

`plan` and `select` list the missing requirements and the configurations you
can use instead. To also see excluded configurations, run
`./bin/narration-video-gen select --explain`.

The most common cause is too little swap. Every configuration needs at least
16 GiB, and 720p on Linux needs 32 GiB. `scripts/setup-linux.sh` keeps your
existing swap and offers to add more up to 32 GiB in total.

**Physical Windows RAM could not be measured** means WSL could not run
`powershell.exe`. Configurations that need this value cannot be selected.

## Not enough disk space

Model weights need 55 GiB of free space for Wan 2.1 and 80 GiB for Wan 2.2.
`./bin/narration-video-gen detect` shows the current free space.

## CUDA out of memory

**During generation (sampler):** another program may be using GPU memory. The
16 GiB configurations have only a few hundred MiB to spare, so close browsers
and other CUDA processes and try again. If it still fails, `calibrate` searches
for a setting that fits this machine.

**During face refinement:** raise `face_detailer_blocks_to_swap` or lower
`face_detailer_size`, as suggested by `status`, in a
[custom profile](../examples/profiles/README.md).

**During VAE work at 720p on 24 GiB:** 720p VAE work does not fit in 24 GiB.
Choose a configuration that uses block swapping, such as
`linux-wan21-720p-vram16`.

`blocks_to_swap` goes up to 40. If 40 does not fit, lower the resolution or use
a larger GPU.

## The GPU hangs (Xid 119, GSP RPC timeout)

This can happen when a large `blocks_to_swap` is used on a GPU with spare VRAM.
Use the configuration that matches your GPU's VRAM; on 24 GiB,
`blocks_to_swap=0` is fastest. Check with `journalctl -k | grep -i xid`.

## The video degrades as it goes on

A grid-like pattern that gets stronger towards the end is caused by tiled VAE.
If you use a custom profile with tiled VAE enabled, disable it.

## The video turns gray part-way through

This has been seen once with Wan 2.2 at 720p on Windows. Run it again with the
same configuration.

## The narration voice changes between sentences

The seed is not fixed. The narration page and `tts generate` fix a seed per
character. If you synthesise audio yourself, fix the seed and use a reference
clip.

## Video and audio lengths differ by a few frames

A difference of up to four frames is normal and accepted by `verify`. For larger
differences, check that `--audio` points to the file the generation actually
used. For a short test, use `outputs/<run-id>/input-short-test.wav`.

## `verify` passes but the video looks wrong

`verify` checks only resolution, frame count, audio presence and sync. It cannot
see lip sync or facial artefacts, so watch the video.

## Windows: allocator failure with expandable segments

This happens with older Windows NVIDIA drivers
([PyTorch #192330](https://github.com/pytorch/pytorch/issues/192330)). A
workaround is enabled by default on WSL2. To disable it with driver 616.92 or
later, run inside WSL:

```bash
export NVG_WSL2_ALLOCATOR_WORKAROUND=0
scripts/up.sh up -d comfy
```

## Windows: memory needed for 720p

| Model | Physical RAM | WSL RAM | Swap |
|---|---:|---:|---:|
| Wan 2.2 720p | 24 GiB | 16 GiB | 48 GiB |
| Wan 2.1 720p | 32 GiB | 20 GiB | 48 GiB |

Raising WSL memory or swap does not change the physical RAM requirement.

## Cannot reach ComfyUI

```bash
scripts/up.sh ps
scripts/up.sh logs comfy
```

ComfyUI has no authentication, so port 8188 listens on 127.0.0.1 only. To use it
from another computer, keep the binding and use an SSH tunnel.

## A model hash does not match

```bash
./bin/narration-video-gen hashes
```

`size-mismatch` means the download was cut off; delete the file and download it
again. `hash-mismatch` means it is a different file; download it again from the
URL in `manifests/models.lock.yaml`.
