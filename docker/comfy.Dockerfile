# ComfyUI + WanVideoWrapper, for both the Wan2.1 InfiniteTalk and Wan2.2 S2V
# recipes. Nothing is installed on the host.
#
# Differences from a typical ComfyUI image, all of them for reproducibility:
#
#   * the base image is pinned by digest, not by tag
#   * every custom node is pinned to a commit, not cloned at --depth 1 from HEAD
#   * a failed dependency install fails the build instead of printing a warning
#   * ComfyUI-Manager is deliberately absent: it updates nodes in place, which
#     is the opposite of what a reproduction image is for
#   * no model weights and no input media are baked in

ARG BASE_IMAGE=pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel
ARG BASE_DIGEST
FROM ${BASE_IMAGE}@${BASE_DIGEST}

ARG TRANSFORMERS_VERSION
ARG COMFYUI_COMMIT
ARG WANVIDEOWRAPPER_COMMIT
ARG KJNODES_COMMIT
ARG VIDEOHELPER_COMMIT
ARG FRAMEINTERP_COMMIT
ARG GGUF_COMMIT
ARG SAM2_COMMIT


ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_HOME=/cache/huggingface

RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl ffmpeg aria2 \
        libgl1 libglib2.0-0 build-essential \
    && rm -rf /var/lib/apt/lists/*

# Pin the base image's torch so that no later `pip install` can silently swap it
# for a CPU-only build pulled in as a transitive dependency. InfiniteTalk also
# depends on the Wav2Vec2 hidden-state API from the locked Transformers release.
RUN pip freeze | grep -E '^(torch|torchvision|torchaudio)==' > /opt/runtime-constraints.txt \
    && printf 'transformers==%s\n' "${TRANSFORMERS_VERSION}" >> /opt/runtime-constraints.txt \
    && cat /opt/runtime-constraints.txt

WORKDIR /opt
RUN git clone https://github.com/comfyanonymous/ComfyUI.git \
 && git -C ComfyUI checkout --detach "${COMFYUI_COMMIT}"

WORKDIR /opt/ComfyUI
RUN pip install --no-cache-dir -c /opt/runtime-constraints.txt -r requirements.txt

WORKDIR /opt/ComfyUI/custom_nodes
RUN set -eux; \
    clone() { git clone "$1" "$2" && git -C "$2" checkout --detach "$3"; }; \
    clone https://github.com/kijai/ComfyUI-WanVideoWrapper.git         ComfyUI-WanVideoWrapper         "${WANVIDEOWRAPPER_COMMIT}"; \
    clone https://github.com/kijai/ComfyUI-KJNodes.git                 ComfyUI-KJNodes                 "${KJNODES_COMMIT}"; \
    clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git  ComfyUI-VideoHelperSuite        "${VIDEOHELPER_COMMIT}"; \
    clone https://github.com/Fannovel16/ComfyUI-Frame-Interpolation.git ComfyUI-Frame-Interpolation    "${FRAMEINTERP_COMMIT}"; \
    clone https://github.com/city96/ComfyUI-GGUF.git                   ComfyUI-GGUF                    "${GGUF_COMMIT}"; \
    clone https://github.com/kijai/ComfyUI-segment-anything-2.git      ComfyUI-segment-anything-2      "${SAM2_COMMIT}"

# The official Wan2.2 S2V runner can begin from the reference image. The pinned
# wrapper does not expose that switch, so apply the narrow equivalent locally.
COPY docker/patches/wanvideowrapper-start-from-ref.patch /tmp/wanvideowrapper-start-from-ref.patch
RUN git -C /opt/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper apply \
        /tmp/wanvideowrapper-start-from-ref.patch \
 && rm /tmp/wanvideowrapper-start-from-ref.patch

# Opt-in native BF16/FP16 nearest expansion avoids large temporary FP32
# buffers. The loader input keeps the default path and cache identity intact.
COPY docker/patches/wanvideowrapper-native-upsample.patch /tmp/wanvideowrapper-native-upsample.patch
RUN git -C /opt/ComfyUI/custom_nodes/ComfyUI-WanVideoWrapper apply \
        /tmp/wanvideowrapper-native-upsample.patch \
 && rm /tmp/wanvideowrapper-native-upsample.patch

# Repository-owned glue nodes contain no weights. They connect the pinned face
# detector to SAM2 without adding a large third-party node pack.
COPY docker/nvg_nodes /opt/ComfyUI/custom_nodes/narration-video-gen

# Keep local calibration on exactly the same CUDA/Python runtime as normal
# generation. The CLI starts this harness only in a one-off container.
COPY bin/ /opt/nvg/bin/
COPY src/ /opt/nvg/src/
COPY recipes/ /opt/nvg/recipes/
COPY profiles/ /opt/nvg/profiles/
COPY manifests/ /opt/nvg/manifests/
COPY calibration/ /opt/nvg/calibration/
COPY scripts/hold-vram-ballast.py /opt/nvg/scripts/hold-vram-ballast.py
COPY assets/characters/aoi/manifest.yaml /opt/nvg/assets/characters/aoi/manifest.yaml
COPY assets/characters/aoi/images/aoi-portrait-angled-01.png /opt/nvg/assets/characters/aoi/images/aoi-portrait-angled-01.png
COPY assets/characters/aoi/audio/aoi-narration-take3.wav /opt/nvg/assets/characters/aoi/audio/aoi-narration-take3.wav
RUN chmod 0755 /opt/nvg/bin/narration-video-gen \
        /opt/nvg/calibration/calibrate.py

ENV NVG_ROOT=/opt/nvg \
    NVG_WORKSPACE=/workspace \
    PYTHONPATH=/opt/nvg/src

# A node whose dependencies did not install is a node that will fail at run time
# with a confusing error, hours into a generation. Fail here instead.
RUN set -eux; \
    for d in /opt/ComfyUI/custom_nodes/*/; do \
      if [ -f "$d/requirements.txt" ]; then \
        echo "installing $d"; \
        pip install --no-cache-dir -c /opt/runtime-constraints.txt -r "$d/requirements.txt"; \
      fi; \
    done

# Post-processing and verification: ffprobe comes from ffmpeg above,
# faster-whisper is used to check that synthesised narration says what the
# script says.
RUN pip install --no-cache-dir -c /opt/runtime-constraints.txt \
        "huggingface_hub[hf_transfer]" soundfile librosa faster-whisper

RUN python - <<'PY'
import inspect
import torch
import transformers
from transformers.models.wav2vec2.modeling_wav2vec2 import Wav2Vec2Encoder

assert torch.version.cuda, "CPU-only torch was installed"
parameters = inspect.signature(Wav2Vec2Encoder.forward).parameters
assert "output_hidden_states" in parameters, (
    "Transformers Wav2Vec2Encoder no longer exposes output_hidden_states"
)
print("torch", torch.__version__, "cuda", torch.version.cuda)
print("transformers", transformers.__version__)
PY

# Keep an opt-in escape hatch for Windows drivers older than R615/616.92 that
# advertise a broken GPUDirect RDMA allocation path under WSL2. Current drivers
# do not load this narrowly scoped, source-tracked compatibility shim by default.
COPY docker/vmm_rdma_interpose.c /tmp/vmm_rdma_interpose.c
RUN gcc -O2 -fPIC -shared -Wall -Wextra -Werror -Wl,-z,defs \
        -I/usr/local/cuda/include \
        -o /opt/vmm-rdma-interpose.so /tmp/vmm_rdma_interpose.c -ldl \
 && rm /tmp/vmm_rdma_interpose.c

WORKDIR /opt/ComfyUI
EXPOSE 8188
ARG RUNTIME_BUILD_SHA
LABEL io.narration-video-gen.runtime-build-sha="${RUNTIME_BUILD_SHA}"

# --disable-pinned-memory : the default pins most of host RAM, which starves
#                           block swapping on a 20 GiB WSL allocation
# --disable-cuda-malloc   : cudaMallocAsync is unstable combined with block swap
# --disable-async-offload : two-stream weight offload, same reason
# Listen on the container interface so Compose can forward the port. The host
# side remains restricted to 127.0.0.1 in compose.yaml.
CMD ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188", \
     "--disable-pinned-memory", "--disable-cuda-malloc", "--disable-async-offload"]
