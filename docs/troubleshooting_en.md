[日本語](troubleshooting.md) | **English**

# Troubleshooting

When a generation fails, start with `./bin/narration-video-gen status`, which
shows the cause and suggested fixes. The full log is `outputs/<run-id>/run.log`.

## Windows Docker Desktop fails after agent-operated setup

For `remove` / `rename` failures on `sailor-ingest.sock`, `dockerInference` or
`docker-secrets-engine\engine.sock` with `The file cannot be accessed by the
system` (error 1920), first check how Docker was launched.

Commands launched from MSIX-packaged Codex can inherit Windows AppData
virtualization. The same apparent path can refer to different, package-private
files than a normal terminal sees. Windows AF_UNIX socket connections, renames
and deletion fail in this execution context. The 2026-09-28 investigation
reproduced this in a new AppData folder without Docker; the same operations
succeeded in the user's PowerShell, including inside an ordinary Job object.
Job membership alone is not a reliable cause or safety check. A ReparsePoint
attribute or error 1920 alone does not establish socket corruption.

### Split setup at WSL integration

Agents use `setup.cmd -Agent` to prepare the machine up to WSL integration.
When Docker Desktop startup/restart or integration is needed, setup displays a
handoff and stops. **The user must double-click "Narration Video Gen" on the
desktop and continue the wizard themselves.** The agent must not launch that
shortcut or answer the integration prompt. If shortcut creation failed, the
user should double-click `setup.cmd` in the downloaded folder instead.

The user-run wizard still performs integration automatically. Docker startup
waits finish at the first successful response, without three-success or
60-second stability requirements. Afterwards, the agent can run
`setup.cmd -Check` to verify Windows/Ubuntu Docker access and resume work in WSL.

### If startup has already failed

1. Stop launching/restarting Docker from the agent and end its setup session.
2. Preserve relevant fresh logs from `%LOCALAPPDATA%\Docker\log\host` locally.
   Upload logs only with the user's approval.
3. Have the user quit Docker Desktop normally and reopen **Narration Video Gen**
   from the desktop. If normal Quit cannot finish, have the user save their work,
   restart Windows and open the same shortcut.
4. Run `setup.cmd -Check` afterwards. If startup also fails from the user's
   desktop, investigate logs from that startup; do not repeatedly restart it
   from the agent.

Deleting/moving sockets or runtime directories, Factory Reset and reinstalling
Docker are not automatic remedies for this execution-context problem.

References: [Microsoft AppData virtualization](https://learn.microsoft.com/en-us/windows/msix/desktop/desktop-to-uwp-behind-the-scenes),
[virtualization setting precedence](https://learn.microsoft.com/en-us/windows/msix/desktop/flexible-virtualization),
[Docker diagnostics/logs](https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/).

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
