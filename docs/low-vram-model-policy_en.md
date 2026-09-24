# Low-VRAM model selection

[日本語](low-vram-model-policy.md) | **English**

Configurations for GPUs with 12 GiB of VRAM or less use smaller Q4_K_S base
models.

| Model | 12 GiB and below | 16 GiB and above |
|---|---|---|
| Wan 2.1 InfiniteTalk | `wan21-i2v-14b-480p-q4ks` | Q6 |
| Wan 2.2 S2V | `wan22-s2v-14b-q4ks` | FP8 |

- The Q4_K_S models are set in the `*-q4ks` recipes. Models are never replaced
  automatically at run time.
- Wan 2.1 at 720p also uses the 480p base model and outputs 720p.
- The audio adapter, text encoder and face refinement (VACE) models are not Q4.

## Verification status

| Configuration | Result |
|---|---|
| Windows, Wan 2.2, 480p, 8 GiB equivalent (24 GiB physical RAM, 16 GiB WSL, 48 GiB swap) | Full length completed and passed visual review. WSL stopped once and the run was resumed |
| Linux, Wan 2.2, 480p, 12 GiB equivalent (16 GiB RAM, 32 GiB swap) | Full length completed and passed visual review. Generation and post-processing ran as separate steps |
| Wan 2.2, 720p, 8 GiB and 12 GiB equivalent | Out of memory in the short test |
| Wan 2.1 at 12 GiB and below | Not tested |

The 8 GiB and 12 GiB equivalent results were produced by reserving part of a
larger GPU. They have not been confirmed on physical 8 GiB or 12 GiB GPUs.
