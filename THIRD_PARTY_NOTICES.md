# Third-party notices

This repository's own code is licensed under Apache-2.0. The third-party
components below are not included in this repository; they are downloaded at
build or run time from the revisions pinned in `manifests/`. Check the upstream
terms before use, as they may change.

## Container contents

| Component | Licence | Source |
|---|---|---|
| ComfyUI | GPL-3.0 | https://github.com/comfyanonymous/ComfyUI |
| ComfyUI-WanVideoWrapper | Apache-2.0 | https://github.com/kijai/ComfyUI-WanVideoWrapper |
| ComfyUI-KJNodes | GPL-3.0 | https://github.com/kijai/ComfyUI-KJNodes |
| ComfyUI-VideoHelperSuite | GPL-3.0 | https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite |
| ComfyUI-Frame-Interpolation (RIFE) | MIT | https://github.com/Fannovel16/ComfyUI-Frame-Interpolation |
| ComfyUI-GGUF | Apache-2.0 | https://github.com/city96/ComfyUI-GGUF |
| ComfyUI-segment-anything-2 | Apache-2.0 | https://github.com/kijai/ComfyUI-segment-anything-2 |
| MuseTalk | MIT | https://github.com/TMElyralab/MuseTalk |
| PyTorch | BSD-3-Clause | https://github.com/pytorch/pytorch |
| FFmpeg | LGPL-2.1+ / GPL-2+ depending on build | https://ffmpeg.org |

Container images are built locally from the `Dockerfile`s in `docker/`. Every
upstream component is pinned to a commit in `manifests/containers.lock.yaml`.
Patches applied to ComfyUI-WanVideoWrapper are in `docker/patches/`. If you
distribute a built image, the GPL-3.0 components require you to make their
corresponding source available.

The CLI in this repository communicates with ComfyUI over its HTTP API and is
not linked into it.

## Models

Per-model licences, sources and file hashes are in
`manifests/models.lock.yaml`.

| Family | Licence | Notes |
|---|---|---|
| Wan2.1 / Wan2.2 (Wan-AI) | Apache-2.0 | |
| InfiniteTalk (MeiGen-AI) | Apache-2.0 | |
| LightX2V step-distillation LoRAs | Apache-2.0 | |
| SAM 2.1 (Meta) | Apache-2.0 | |
| YuNet face detector (OpenCV Zoo) | MIT | |
| CLIP-ViT-H-14 (LAION) | MIT | |
| wav2vec2 audio encoders | Apache-2.0 | |
| MuseTalk 1.5 and Stable Diffusion VAE weights | CreativeML Open RAIL-M | Optional lip-sync stage |
| Whisper Tiny | Apache-2.0 | MuseTalk audio features |
| OpenAI Whisper Base | MIT | Transcript check for generated narration |
| DWPose | Apache-2.0 | MuseTalk preprocessing |
| face-parsing.PyTorch / ResNet-18 | MIT / BSD-3-Clause | MuseTalk preprocessing |
| Irodori-TTS code and weights | MIT | See the usage terms below |
| Semantic-DACVAE-Japanese-32dim | MIT | Codec used by Irodori-TTS |
| SilentCipher (Sony) | MIT | Watermark embedded in synthesised audio |

### Irodori-TTS usage terms

The Irodori-TTS model card states these terms in addition to the MIT licence:

* Do not imitate a real person's voice without that person's explicit consent.
* Do not generate misinformation or deepfakes intended to deceive.
* A voice generated from text alone may coincidentally resemble a real person.
* The user is responsible for how the output is used.

## Trademarks

Product and company names are trademarks of their respective owners. This
project is not affiliated with or endorsed by Alibaba, MeiGen-AI, Meta, Sony,
NVIDIA, Microsoft or the ComfyUI project.
