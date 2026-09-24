# Verification results

[日本語](evidence.md) | **English**

Every configuration in `profiles/` carries labels that say how far it was
verified. For the per-configuration overview, see the
[hardware matrix](hardware-matrix_en.md).

## Labels

| Field | Value | Meaning |
|---|---|---|
| `duration_class` | `full` | The whole narration was generated |
| | `short` | Only a short clip of about 5.6 seconds was generated |
| | `gate` | A capacity check stopped part-way; no finished video |
| `gpu_evidence` | `physical` | Run on a GPU with that amount of VRAM |
| | `capacity-simulated` | Run on a larger GPU with part of its memory reserved to reproduce a smaller one |
| `visual_review` | `passed` | A person watched the video and accepted it |
| | `pending` | Nobody has watched it |
| | `failed` | A person watched it and rejected it |

`short` only shows that the configuration fits in memory; it cannot reveal
defects that build up in a long video. `capacity-simulated` results had more
usable memory than a real 8 GiB or 12 GiB card. `status` is one of
`recommended`, `acceptable`, `experimental` and `not-recommended`. For any
configuration that is not `full`, `physical` and `passed`, `plan` shows the
limitation.

## Settings policy

- **No tiled VAE.** It builds up a grid-like pattern in long videos.
  `linux-wan21-720p-vram24-tiled` is kept for reference and is not recommended.
- **Block swapping only affects speed.** `blocks_to_swap=40` gave the same
  quality as no swapping.
- **No unnecessary block swapping.** Swapping on a GPU with spare VRAM is slower
  and can hang the GPU, so each VRAM size has its own configuration.

## Measured results

About 30 seconds of narration through generation, face refinement, frame
interpolation and the 60 fps finish. Run time varies considerably with GPU
cooling and power limits.

### Linux

| Model | Resolution | GPU | `blocks_to_swap` | Time | Review |
|---|---|---|---:|---:|---|
| Wan 2.1 | 480p | RTX A4000 16 GiB | 16 | ~49 min | passed |
| Wan 2.1 | 720p | RTX A4000 16 GiB | 28 | ~147 min | passed |
| Wan 2.1 | 480p | RTX 3090 24 GiB | 0 | ~26 min | passed |
| Wan 2.1 | 720p | RTX 3090 24 GiB | 8 | ~81 min | passed |
| Wan 2.2 | 480p | RTX A4000 16 GiB | 20 | ~32 min | passed |
| Wan 2.2 | 720p | RTX A4000 16 GiB | 32 | ~95 min | passed |
| Wan 2.2 | 480p | RTX 3090 24 GiB | 0 | ~19 min | passed |
| Wan 2.2 | 720p | RTX 3090 24 GiB | 11 | ~72 min | passed |

### Windows + WSL2

| Model | Resolution | GPU | Physical RAM | `blocks_to_swap` | Time | Review |
|---|---|---|---:|---:|---:|---|
| Wan 2.1 | 480p | RTX A4000 / RTX 5060 Ti 16 GiB | 32 GiB | 19 | — | passed |
| Wan 2.1 | 720p | RTX 5060 Ti 16 GiB | 32 GiB | 31 | ~159 min | pending |
| Wan 2.2 | 480p | RTX 5060 Ti 16 GiB | 32 GiB | 22 | ~45 min | passed |
| Wan 2.2 | 480p | RTX A4000 16 GiB | 32 GiB | 22 | ~56 min | passed |
| Wan 2.2 | 720p | RTX 5060 Ti 16 GiB | 24 GiB | 40 | ~213 min | passed |

Windows 720p Wan 2.2 was run as generation and post-processing in separate
steps.

### 12 GiB and below

Configurations for 12 GiB and below use Q4_K_S models. Wan 2.2 at 480p completed
full-length runs at both 8 GiB and 12 GiB equivalents and passed visual review
(capacity-simulated, including a resume). The other configurations for 12 GiB
and below are short-only, ran out of memory in the short test, or are untested
candidates. See [Low-VRAM model selection](low-vram-model-policy_en.md).

## Notes

- RAM and swap requirements are the values of the machines used for testing,
  not minimums.
- With the same settings, different GPU models do not produce bit-identical
  output.
- Time estimates use measurements from well-cooled machines.

## Not yet verified

- Runs on physical 8 GiB and 12 GiB GPUs
- Visual review of the Windows 720p Wan 2.1 output
- Single-run completion for the 12 GiB-and-below configurations and for Windows
  720p Wan 2.2
