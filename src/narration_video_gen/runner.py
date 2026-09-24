"""Drive a run through the ComfyUI HTTP API, one stage at a time.

Every generation setting comes from the recipe and every hardware-dependent
setting from the profile. There is no way to override them from the command
line: a run with hand-edited settings is not a reproduction of anything
published here, and the way to change one is to write a profile that records
why it changed.

A pipeline is a list of stages (see ``stages.py``). The first may generate video
from an image and audio; the rest transform a video file into another one, which
is what lets the Wan2.2 post-stages be appended to a Wan2.1 recipe or run alone.
"""

from __future__ import annotations

import base64
import json
import re
import socket
import struct
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid
from pathlib import Path

from . import stages as stages_mod
from .verify import frames_for_audio, wav_duration

DEFAULT_SERVER = "127.0.0.1:8188"


class _ProgressSocket:
    """Small dependency-free reader for ComfyUI's local progress WebSocket."""

    def __init__(self, server, client_id):
        host, separator, port = server.rpartition(":")
        if not separator:
            host, port = server, "8188"
        self.socket = socket.create_connection((host, int(port)), timeout=10)
        key = base64.b64encode(uuid.uuid4().bytes).decode("ascii")
        path = "/ws?clientId=%s" % urllib.parse.quote(client_id, safe="")
        request = (
            "GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, server, key)
        )
        self.socket.sendall(request.encode("ascii"))
        response = self._read_headers()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            self.close()
            raise OSError("ComfyUI did not accept the progress WebSocket")

    def _read_headers(self):
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.socket.recv(1024)
            if not chunk:
                raise OSError("ComfyUI closed the progress WebSocket")
            data.extend(chunk)
            if len(data) > 16384:
                raise OSError("ComfyUI sent oversized WebSocket headers")
        headers, _, remainder = bytes(data).partition(b"\r\n\r\n")
        self._buffer = bytearray(remainder)
        return headers

    def _read_exact(self, size):
        data = bytearray(self._buffer[:size])
        del self._buffer[:size]
        while len(data) < size:
            chunk = self.socket.recv(size - len(data))
            if not chunk:
                raise OSError("ComfyUI closed the progress WebSocket")
            data.extend(chunk)
        return bytes(data)

    def receive(self, timeout):
        self.socket.settimeout(timeout)
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        size = second & 0x7F
        if size == 126:
            size = struct.unpack("!H", self._read_exact(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self._read_exact(8))[0]
        if second & 0x80:
            mask = self._read_exact(4)
            payload = bytes(byte ^ mask[index % 4]
                            for index, byte in enumerate(self._read_exact(size)))
        else:
            payload = self._read_exact(size)
        if opcode == 0x8:
            raise OSError("ComfyUI closed the progress WebSocket")
        if opcode != 0x1:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None

    def close(self):
        try:
            self.socket.close()
        except AttributeError:
            pass

# The prompt is part of the recipe's identity: it describes the scene the
# published runs generated. Changing it changes the result, so it lives next to
# the workflow rather than being a command-line option.
POSITIVE_PROMPT = (
    "The same subject as in the reference image speaks directly to the camera with lively, "
    "natural facial expressions, natural head and upper-body movement, and expressive hand "
    "gestures. Preserve the subject's identity, appearance, clothing, background, lighting, "
    "and framing. Photorealistic live-action footage, natural skin texture, stable facial "
    "detail, realistic lips and teeth, stable camera, single continuous shot."
)

NEGATIVE_PROMPT = (
    "bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, "
    "images, static, overall gray, worst quality, low quality, JPEG compression residue, ugly, "
    "incomplete, extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, "
    "misshapen limbs, fused fingers, still picture, messy background, three legs, "
    "many people in the background, walking backwards, frozen body, motionless, hands out of frame"
)

# Recipe model id -> the role(s) a workflow builder asks for. A model that a
# recipe lists but no stage needs simply does not appear here.
#
# One file can fill more than one role. The T2V LoRA is used both by the S2V
# sampler at strength 1.5 and by the face detailer at strength 1.0, and the two
# stages have to be able to ask for it by different names -- otherwise a Wan2.1
# recipe that appends the face detailer would hand it the I2V LoRA instead.
MODEL_ROLES = {
    "wan21-i2v-14b-480p-q6k": ("base",),
    "wan21-i2v-14b-480p-q4ks": ("base",),
    "infinitetalk-single-q8": ("infinitetalk",),
    "wan21-vae-bf16": ("vae",),
    "umt5-xxl-enc-fp8": ("text_encoder",),
    "clip-vision-h": ("clip_vision",),
    "lightx2v-i2v-480p-rank64": ("lora",),
    "wav2vec2-chinese-base-fp16": ("wav2vec",),
    "wan22-s2v-14b-fp8": ("s2v",),
    "wan22-s2v-14b-q4ks": ("s2v",),
    "wan22-t2v-a14b-low-fp8": ("t2v",),
    "wan22-fun-vace-a14b-low-fp8": ("vace",),
    "lightx2v-t2v-rank64-v2": ("lora", "t2v_lora"),
    "wav2vec2-large-english-fp16": ("audio_encoder",),
    "sam2.1-hiera-small": ("sam2",),
    "yunet-face-detector": ("face_detector",),
    "rife49": ("rife",),
    "musetalk-v15-unet": ("musetalk_unet",),
    "musetalk-v15-config": ("musetalk_config",),
    "musetalk-whisper-tiny-model": ("musetalk_whisper_model",),
    "musetalk-whisper-tiny-config": ("musetalk_whisper_config",),
    "musetalk-whisper-tiny-preprocessor": ("musetalk_whisper_preprocessor",),
    "musetalk-sd-vae-model": ("musetalk_vae_model",),
    "musetalk-sd-vae-config": ("musetalk_vae_config",),
    "musetalk-dwpose": ("musetalk_dwpose",),
    "musetalk-face-parse": ("musetalk_face_parse",),
    "musetalk-face-parse-resnet18": ("musetalk_face_resnet",),
}

# Which roles each stage needs present.
# Stages that are an ffmpeg command rather than a ComfyUI workflow.
COMMAND_STAGES = ("retime",)
EXTERNAL_STAGES = ("musetalk",)

STAGE_ROLES = {
    "infinitetalk": ("base", "infinitetalk", "vae", "text_encoder", "clip_vision",
                     "lora", "wav2vec"),
    "s2v": ("s2v", "vae", "text_encoder", "lora", "audio_encoder"),
    "face-detailer": ("vae", "text_encoder", "t2v", "t2v_lora", "vace", "sam2",
                       "face_detector"),
    "rife": ("rife",),
    "retime": (),
    "musetalk": (
        "musetalk_unet", "musetalk_config", "musetalk_whisper_model",
        "musetalk_whisper_config", "musetalk_whisper_preprocessor",
        "musetalk_vae_model", "musetalk_vae_config", "musetalk_dwpose",
        "musetalk_face_parse", "musetalk_face_resnet"),
}


class RunnerError(RuntimeError):
    """A concise user-facing failure with optional technical detail."""

    def __init__(self, message, *, code="runner-error", detail=None, context=None):
        super().__init__(message)
        self.code = code
        self.detail = detail or message
        self.context = context or {}


def _execution_error(status):
    """Extract one useful error from ComfyUI's otherwise very noisy history."""
    messages = status.get("messages", [])
    detail = "\n".join(str(message) for message in messages)
    exception_message = ""
    for message in reversed(messages):
        if not isinstance(message, (list, tuple)) or len(message) < 2:
            continue
        if message[0] != "execution_error" or not isinstance(message[1], dict):
            continue
        exception_message = str(message[1].get("exception_message") or "").strip()
        break
    haystack = "%s\n%s" % (exception_message, detail)
    lowered = haystack.lower()

    # Host memory and GPU memory both surface as "out of memory", and they need
    # opposite remedies: one wants more block swapping, the other wants swap or
    # more RAM. Check the host case first, because a CUDA message never mentions
    # the kernel OOM killer while a host failure can quote a CUDA call.
    if ("cannot allocate memory" in lowered
            or "defaultcpuallocator: not enough memory" in lowered
            or "killed" == exception_message.strip().lower()
            or "oom-killer" in lowered):
        return RunnerError(
            "Host memory ran out while processing the video.",
            code="host-out-of-memory", detail=detail)

    if "out of memory" in lowered:
        context = {}
        allocation = re.search(r"Tried to allocate ([0-9.]+ [KMG]iB)", haystack)
        free = re.search(r"([0-9.]+ [KMG]iB) is free", haystack)
        if allocation:
            context["allocation_requested"] = allocation.group(1)
        if free:
            context["memory_free"] = free.group(1)
        return RunnerError(
            "GPU memory ran out while processing the video.",
            code="gpu-out-of-memory", detail=detail, context=context)
    return RunnerError(
        exception_message or "The generation stage failed in ComfyUI.",
        code="comfy-stage-failed", detail=detail)


# ---------------------------------------------------------------------------
# ComfyUI client
# ---------------------------------------------------------------------------

class ComfyClient:
    def __init__(self, server=DEFAULT_SERVER, timeout=60):
        self.server = server
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    def call(self, path, data=None):
        url = "http://%s%s" % (self.server, path)
        if data is None:
            request = urllib.request.Request(url)
        else:
            request = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"},
            )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = response.read()
            # Mutating ComfyUI endpoints such as /free reply with HTTP 200 and
            # an empty body.  Success must not be misclassified as malformed
            # JSON, while non-empty responses remain strictly decoded.
            return None if not payload else json.loads(payload.decode())

    def submit(self, workflow):
        try:
            result = self.call("/prompt", {"prompt": workflow, "client_id": self.client_id})
        except urllib.error.HTTPError as exc:
            raise RunnerError(
                "ComfyUI rejected the workflow (HTTP %d):\n%s" % (exc.code, exc.read().decode())
            ) from exc
        except urllib.error.URLError as exc:
            raise RunnerError(
                "cannot reach ComfyUI at %s -- is the container running? "
                "(scripts/up.sh up -d)\n%s" % (self.server, exc)
            ) from exc
        return result["prompt_id"]

    def wait_ready(self, timeout=300):
        """Allow cold imports and first-run database setup before declaring failure."""
        deadline = time.time() + timeout
        last_error = None
        while time.time() < deadline:
            try:
                self.call("/system_stats")
                return
            except (OSError, ValueError, urllib.error.URLError) as exc:
                last_error = exc
                time.sleep(2)
        raise RunnerError("ComfyUI did not become ready within %d seconds: %s"
                          % (timeout, last_error))

    def wait(self, prompt_id, poll_seconds=5, on_progress=None):
        """Block until ``prompt_id`` finishes, forwarding live progress when available.

        History remains the authority for completion and error detail. The local
        WebSocket merely makes sampler progress available while that history
        entry is still pending, and failure to attach it deliberately falls back
        to the old polling-only behaviour.
        """
        started = time.time()
        last_history_poll = 0.0
        try:
            progress_socket = _ProgressSocket(self.server, self.client_id)
        except OSError:
            progress_socket = None
        while True:
            if progress_socket is not None:
                try:
                    event = progress_socket.receive(timeout=1)
                except socket.timeout:
                    event = None
                except OSError:
                    progress_socket.close()
                    progress_socket = None
                    event = None
                if event and event.get("type") == "progress":
                    data = event.get("data") or {}
                    if data.get("prompt_id") == prompt_id and on_progress:
                        on_progress(data)
            else:
                time.sleep(min(1, poll_seconds))

            now = time.time()
            if now - last_history_poll < poll_seconds:
                continue
            last_history_poll = now
            try:
                history = self.call("/history/%s" % prompt_id)
            except (TimeoutError, socket.timeout):
                # A long CUDA kernel can keep ComfyUI's HTTP event loop from
                # answering even though the queued prompt is still healthy.
                # Treat a read timeout like an empty history poll and retry;
                # connection failures still propagate so a dead service is
                # not mistaken for a running generation.
                continue
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status", {})
                if status.get("status_str") == "error" or not status.get("completed", True):
                    raise _execution_error(status)
                if progress_socket is not None:
                    progress_socket.close()
                return entry


def outputs_of(history, node_id=None):
    """Collect produced file paths from a history entry, newest stage first."""
    produced = []
    node_outputs = history.get("outputs", {})
    candidates = [node_id] if node_id and node_id in node_outputs else list(node_outputs)
    for key in candidates:
        for kind in ("gifs", "videos", "images"):
            for item in node_outputs[key].get(kind, []):
                subfolder = item.get("subfolder", "")
                produced.append("%s/%s" % (subfolder, item["filename"]) if subfolder
                                else item["filename"])
    return produced


def video_and_audio_outputs(outputs):
    """Choose the exact-frame video and its audio-bearing companion.

    VideoHelperSuite saves both ``name.mp4`` and ``name-audio.mp4`` when a
    combine node receives audio, but its history may report only the latter.
    The audio-bearing file can be a few frames shorter while muxing. Post-stages
    must consume the untrimmed sibling; only the audio source uses the reported
    companion. The caller probes the derived path before submitting a stage.
    """
    video_suffixes = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
    videos = [item for item in outputs
              if Path(item).suffix.lower() in video_suffixes]
    exact = [item for item in videos
             if not Path(item).stem.endswith("-audio")]
    audio = [item for item in videos
             if Path(item).stem.endswith("-audio")]
    if not exact and len(audio) == 1:
        audio_path = Path(audio[0])
        exact = [str(audio_path.with_name(
            audio_path.stem.removesuffix("-audio") + audio_path.suffix,
        ))]
    if len(exact) != 1:
        raise RunnerError(
            "a video stage must report exactly one exact-frame video; got %d from %r"
            % (len(exact), outputs))
    if len(audio) > 1:
        raise RunnerError(
            "a video stage reported more than one audio-bearing companion: %r"
            % audio)
    if audio:
        exact_path, audio_path = Path(exact[0]), Path(audio[0])
        if (audio_path.parent != exact_path.parent
                or audio_path.stem != exact_path.stem + "-audio"):
            raise RunnerError(
                "audio-bearing output %s is not the companion of %s"
                % (audio[0], exact[0]))
    return exact[0], audio[0] if audio else exact[0]


def face_detail_frame_count(available_frames, requested_frames=None):
    """Return a VACE-safe frame count without inventing source frames.

    Wan video latents require ``4n+1`` frames.  When resuming from a muxed
    video, ffmpeg may have shortened the stream by a few frames.  An implicit
    count is therefore rounded *down* to the largest sequence the source can
    actually provide.  An explicit count is a reproducibility assertion and
    is rejected instead of silently changed.
    """
    try:
        available = int(available_frames) if available_frames is not None else None
        requested = int(requested_frames) if requested_frames is not None else None
    except (TypeError, ValueError) as exc:
        raise RunnerError("face detail frame counts must be integers") from exc
    if available is not None and available < 5:
        raise RunnerError(
            "face detail needs at least 5 source frames; got %d" % available)
    if requested is not None:
        if requested < 5 or (requested - 1) % 4:
            raise RunnerError(
                "face detail --frames must satisfy 4n+1 and be at least 5; got %d"
                % requested)
        if available is not None and requested > available:
            raise RunnerError(
                "face detail requested %d frames but the source contains only %d"
                % (requested, available))
        return requested
    if available is None:
        raise RunnerError("could not determine the source frame count")
    return available - ((available - 1) % 4)


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def plan_frames(audio_path, fps, seconds=None):
    """Frame count for this narration, using the pipeline's own rounding."""
    if seconds is None:
        seconds = wav_duration(audio_path)
    return seconds, frames_for_audio(seconds, fps)


def trim_wav(source, target, seconds):
    """Write the first ``seconds`` of a PCM WAV and return its exact duration."""
    import wave

    source = Path(source)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(source), "rb") as reader:
        params = reader.getparams()
        frame_count = min(reader.getnframes(), int(round(seconds * reader.getframerate())))
        payload = reader.readframes(frame_count)
        with wave.open(str(target), "wb") as writer:
            writer.setparams(params)
            writer.writeframes(payload)
        return frame_count / reader.getframerate()


def resolve_models(root, recipe, stage_ids):
    """Map the recipe's model ids to the filenames ComfyUI addresses them by."""
    from .plan import load_models_lock

    lock = load_models_lock(root)
    by_id = {entry["id"]: entry for entry in lock["models"]}
    resolved = {}
    for model_id in recipe.get("models", []):
        # ComfyUI addresses a model by its path under models/<category>/, so the
        # lock path minus its first component is exactly what a node wants.
        entry = by_id[model_id]
        filename = entry.get("node_name", entry["path"].split("/", 1)[1])
        for role in MODEL_ROLES.get(model_id, ()):
            resolved[role] = filename

    needed = set()
    for stage_id in stage_ids:
        needed.update(STAGE_ROLES.get(stage_id, ()))
    missing = sorted(needed - set(resolved))
    if missing:
        raise RunnerError(
            "recipe %s does not provide the model(s) needed by stage(s) %s: %s"
            % (recipe["id"], ", ".join(stage_ids), ", ".join(missing))
        )
    return resolved


def pipeline_for(recipe, requested=None):
    """Return the ordered stage list for this run."""
    declared = recipe.get("pipeline_stages") or [recipe["model_family"].split("-")[-1]]
    stages = requested if requested else declared
    return stages_mod.order_stages(stages)


# ---------------------------------------------------------------------------
# stage 1a: Wan2.1 InfiniteTalk
# ---------------------------------------------------------------------------

def build_infinitetalk(recipe, profile, models, image_name, audio_name, frames,
                       out_prefix):
    width, height = recipe["resolution"]
    sampler = recipe["sampler"]
    settings = profile.get("settings") or {}

    workflow = {
        "1": {"class_type": "WanVideoBlockSwap", "inputs": {
            "blocks_to_swap": settings["blocks_to_swap"],
            "offload_img_emb": False, "offload_txt_emb": False,
            "use_non_blocking": settings.get("use_non_blocking", False),
            "vace_blocks_to_swap": 0, "prefetch_blocks": 0,
            "block_swap_debug": False}},
        # merge_loras must stay false: the base model is GGUF and cannot merge.
        "2": {"class_type": "WanVideoLoraSelect", "inputs": {
            "lora": models["lora"], "strength": recipe["lora"]["strength"],
            "low_mem_load": False, "merge_loras": recipe["lora"].get("merge", False)}},
        "3": {"class_type": "MultiTalkModelLoader", "inputs": {
            "model": models["infinitetalk"]}},
        "4": {"class_type": "WanVideoModelLoader", "inputs": {
            "model": models["base"], "base_precision": sampler["base_precision"],
            "quantization": "disabled", "load_device": "offload_device",
            "attention_mode": sampler["attention"],
            "block_swap_args": ["1", 0], "lora": ["2", 0], "multitalk_model": ["3", 0]}},
        "5": {"class_type": "WanVideoVAELoader", "inputs":
            stages_mod.wan_vae_loader_inputs(models["vae"], settings)},
        "6": {"class_type": "CLIPVisionLoader", "inputs": {
            "clip_name": models["clip_vision"]}},
        "7": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "8": {"class_type": "ImageResizeKJv2", "inputs": {
            "image": ["7", 0], "width": width, "height": height,
            "upscale_method": "lanczos", "keep_proportion": "crop",
            "pad_color": "0, 0, 0", "crop_position": "center",
            "divisible_by": 16, "device": "cpu"}},
        "9": {"class_type": "WanVideoClipVisionEncode", "inputs": {
            "clip_vision": ["6", 0], "image_1": ["8", 0],
            "strength_1": 1.0, "strength_2": 1.0, "crop": "center",
            "combine_embeds": "average", "force_offload": True,
            "tiles": 0, "ratio": 0.5}},
        "10": {"class_type": "LoadAudio", "inputs": {"audio": audio_name}},
        "11": {"class_type": "Wav2VecModelLoader", "inputs": {
            "model": models["wav2vec"], "base_precision": "fp16",
            "load_device": "main_device"}},
        "12": {"class_type": "MultiTalkWav2VecEmbeds", "inputs": {
            "wav2vec_model": ["11", 0], "audio_1": ["10", 0],
            "normalize_loudness": True, "num_frames": frames,
            "fps": float(recipe["fps"]), "audio_scale": 1.0,
            "audio_cfg_scale": sampler["audio_cfg_scale"], "multi_audio_type": "para"}},
        "13": {"class_type": "WanVideoTextEncodeCached", "inputs": {
            "model_name": models["text_encoder"], "precision": "bf16",
            "positive_prompt": (recipe.get("prompt") or {}).get(
                "positive", POSITIVE_PROMPT),
            "negative_prompt": (recipe.get("prompt") or {}).get(
                "negative", NEGATIVE_PROMPT),
            "quantization": "disabled", "use_disk_cache": True, "device": "gpu"}},
        "14": {"class_type": "WanVideoImageToVideoMultiTalk", "inputs": {
            "vae": ["5", 0], "width": width, "height": height,
            "frame_window_size": recipe["window"],
            "motion_frame": recipe["motion_frames"],
            "force_offload": settings.get("offload_transformer_before_vae_decode", False),
            "colormatch": "disabled",
            "start_image": ["8", 0], "clip_embeds": ["9", 0],
            "mode": "infinitetalk",
            "continuation_encode_tiling": "inherit",
            "continuation_conditioning_encode_tiling": "inherit",
            "tiled_vae": settings.get("tiled_vae", False)}},
        "15": {"class_type": "WanVideoSampler", "inputs": {
            "model": ["4", 0], "image_embeds": ["14", 0],
            "steps": sampler["steps"], "cfg": sampler["cfg"], "shift": sampler["shift"],
            "seed": sampler["seed"], "force_offload": True,
            "scheduler": sampler["scheduler"], "riflex_freq_index": 0,
            "text_embeds": ["13", 0], "multitalk_embeds": ["12", 0],
            "rope_function": "comfy", "denoise_strength": 1.0, "batched_cfg": False}},
        "16": {"class_type": "WanVideoPassImagesFromSamples", "inputs": {
            "samples": ["15", 0]}},
        # Keep the raw video at the planned 4n+1 count as well as producing the
        # audio-muxed companion. InfiniteTalk decodes complete windows, which
        # otherwise leaves the raw file longer than the requested narration.
        "18": {"class_type": "ImageFromBatch", "inputs": {
            "image": ["16", 0], "batch_index": 0, "length": frames}},
        # InfiniteTalk generates in windows, so the frame count rounds up beyond
        # the audio. trim_to_audio cuts the video back; with it off, the audio
        # gets padded to the video length instead, which is the wrong direction.
        "17": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["18", 0], "frame_rate": float(recipe["fps"]), "loop_count": 0,
            "filename_prefix": out_prefix, "format": "video/h264-mp4",
            "pix_fmt": recipe["output"]["pix_fmt"], "crf": recipe["output"]["crf"],
            "save_metadata": True, "trim_to_audio": recipe["output"]["trim_to_audio"],
            "pingpong": False, "audio": ["10", 0], "save_output": True}},
    }
    return workflow, "17"


# ---------------------------------------------------------------------------
# stage dispatch
# ---------------------------------------------------------------------------

def build_stage(stage_id, *, recipe, profile, models, image_name=None,
                audio_name=None, frames=None, out_prefix, source_video=None,
                audio_source=None, mask_only=False, source_fps=None):
    """Build one stage.

    Workflow stages return ``(workflow, output_node_id)``. The ``retime`` stage
    is not a ComfyUI graph at all -- it is an ffmpeg invocation -- and returns
    ``(argv, None)`` instead.
    """
    postprocess = recipe.get("postprocess") or {}
    source_fps = source_fps or recipe["fps"]

    if stage_id == "infinitetalk":
        return build_infinitetalk(recipe, profile, models, image_name, audio_name,
                                  frames, out_prefix)
    if stage_id == "s2v":
        return stages_mod.build_s2v(recipe, profile, models, image_name, audio_name,
                                    frames, out_prefix)
    if stage_id == "musetalk":
        return dict(postprocess.get("musetalk") or {}), None
    if stage_id == "face-detailer":
        config = dict(postprocess.get("face_detailer") or {})
        settings = profile.get("settings") or {}
        config.setdefault(
            "blocks_to_swap",
            settings.get(
                "face_detailer_blocks_to_swap",
                settings.get("blocks_to_swap", 40)),
        )
        # A missing stage-specific size must fail toward lower activation
        # memory. 512x512 can exhaust a 16 GiB card on a full-length temporal
        # latent, while 320x320 is the verified baseline for 480p profiles.
        config.setdefault("size", settings.get("face_detailer_size", 320))
        config.setdefault("seed", (recipe.get("sampler") or {}).get("seed", 12345))
        config["vae_native_upsample"] = settings.get("vae_native_upsample", False)
        return stages_mod.build_face_detailer(
            config, models, source_video, frames, source_fps, out_prefix,
            audio_source=audio_source, mask_only=mask_only)
    if stage_id == "rife":
        config = dict(postprocess.get("rife") or {})
        return stages_mod.build_rife(
            config, source_video, source_fps, out_prefix,
            audio_source=audio_source)
    if stage_id == "retime":
        config = dict(postprocess.get("retime") or {})
        config.setdefault("target_fps", recipe.get("output_fps"))
        return stages_mod.build_retime(config, source_video, audio_source), None
    raise RunnerError("no builder for stage %r" % stage_id)


def container_path(output_name, outputs_root="/opt/ComfyUI/output"):
    """Translate a ComfyUI output name into the path the next stage loads from."""
    return str(Path(outputs_root) / output_name)


def musetalk_container_name(output_name):
    """Return a stable, run-scoped sidecar name suitable for cancellation."""
    import hashlib

    digest = hashlib.sha256(str(output_name).encode("utf-8")).hexdigest()[:16]
    return "nvg-musetalk-%s" % digest


def _stage_command_prefix(container):
    """Return how a post-processing command reaches the runtime.

    ``self`` is the one-off calibration container, where the harness and
    ComfyUI share one container.  Normal generation retains the Docker-exec path.
    """
    if container == "self":
        return []
    return ["docker", "exec", container]


def run_command_stage(argv, container, audio_source, output_path):
    """Execute a non-ComfyUI stage inside the running container or this image.

    Audio metadata decides both the measured duration and whether the track can
    be copied into MP4. Canonical narration is normally PCM WAV, which MP4
    cannot carry; encode that once as AAC while retaining an existing AAC track
    without another lossy generation.
    """
    import shutil
    import subprocess

    direct = container == "self"
    if not direct and shutil.which("docker") is None:
        raise RunnerError("docker is needed to run the %s stage" % argv[0])

    prefix = _stage_command_prefix(container)
    probe = subprocess.run(
        [*prefix, "ffprobe", "-v", "error",
         "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,duration",
         "-of", "json", audio_source],
        capture_output=True, text=True, check=False,
    )
    try:
        streams = json.loads(probe.stdout).get("streams") or []
        audio = streams[0] if streams else {}
        codec = audio.get("codec_name")
        duration = audio.get("duration")
    except (TypeError, ValueError):
        codec = duration = None
    if probe.returncode != 0 or not codec or not duration:
        raise RunnerError(
            "could not inspect the audio stream of %s%s:\n%s"
            % (audio_source, "" if direct else " inside %s" % container,
               probe.stderr.strip())
        )

    substitutions = {"{audio_duration}": duration, "{output}": output_path}
    audio_codec_args = (["-c:a", "copy"] if codec == "aac" else
                        ["-c:a", "aac", "-b:a", "192k"])
    resolved = []
    for part in argv:
        if part == "{audio_codec_args}":
            resolved.extend(audio_codec_args)
        else:
            resolved.append(substitutions.get(part, part))

    completed = subprocess.run([*prefix, *resolved],
                               capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RunnerError("%s failed:\n%s" % (argv[0], completed.stderr.strip()))
    return output_path


def rife_chunk_ranges(total_frames, chunk_frames, multiplier):
    """Plan overlapping RIFE chunks without losing an interpolation interval.

    Adjacent chunks share one source frame. The first interpolated frame of
    every chunk after the first is removed, so the joined result has exactly
    ``(total_frames - 1) * multiplier + 1`` frames.
    """
    total_frames = int(total_frames)
    chunk_frames = int(chunk_frames)
    multiplier = int(multiplier)
    if total_frames < 2:
        raise RunnerError("RIFE needs at least two source frames")
    if chunk_frames < 2:
        raise RunnerError("RIFE chunk size must be at least two frames")
    if multiplier not in (2, 4):
        raise RunnerError("RIFE chunking supports only a 2x or 4x multiplier")

    chunks = []
    start = 0
    while start < total_frames - 1:
        input_frames = min(chunk_frames, total_frames - start)
        generated_frames = (input_frames - 1) * multiplier + 1
        drop_first = start > 0
        chunks.append({
            "start_frame": start,
            "input_frames": input_frames,
            "generated_frames": generated_frames,
            "drop_first_output_frame": drop_first,
            "kept_frames": generated_frames - int(drop_first),
        })
        start += input_frames - 1
    expected = (total_frames - 1) * multiplier + 1
    if sum(item["kept_frames"] for item in chunks) != expected:
        raise RunnerError("internal RIFE chunk frame accounting error")
    return chunks


def build_rife_chunk_workflow(config, source_video, source_fps, out_prefix,
                              chunk, number):
    """Build the exact video-only workflow used for one planned RIFE chunk."""
    chunk_prefix = "%s-chunks/%04d" % (out_prefix, number)
    chunk_config = dict(config)
    chunk_config.update({
        "include_audio": False,
        "frame_load_cap": chunk["input_frames"],
        "skip_first_frames": chunk["start_frame"],
        "drop_first_output_frame": chunk["drop_first_output_frame"],
        "output_frame_count": chunk["generated_frames"],
    })
    return stages_mod.build_rife(
        chunk_config, source_video, source_fps, chunk_prefix,
        audio_source=None)


def _validate_rife_media(probe, *, expected_frames, expected_fps, label):
    if probe["video_frames"] != expected_frames:
        raise RunnerError(
            "%s produced %d frames; expected %d"
            % (label, probe["video_frames"], expected_frames))
    if probe["video_packets"] != expected_frames:
        raise RunnerError(
            "%s contains %d video packets; expected %d"
            % (label, probe["video_packets"], expected_frames))
    # MP4 concat stream-copy can retain a sub-millisecond boundary duration,
    # making avg_frame_rate differ slightly even though every packet still has
    # the intended cadence. The duration check below remains the authority.
    if abs(probe["video_fps"] - expected_fps) > 0.05:
        raise RunnerError(
            "%s is %.6g fps; expected %.6g"
            % (label, probe["video_fps"], expected_fps))
    expected_duration = expected_frames / expected_fps
    duration = probe.get("video_duration_seconds")
    tolerance = (1.0 / expected_fps) + 0.001
    if duration is None or abs(duration - expected_duration) > tolerance:
        raise RunnerError(
            "%s duration is %r seconds; expected %.6f (+/- %.6f)"
            % (label, duration, expected_duration, tolerance))


def run_chunked_rife_stage(client, *, config, source_video, source_fps,
                           source_frames, out_prefix, audio_source, container,
                           on_chunk=None):
    """Run RIFE in bounded overlapping chunks and losslessly concatenate them."""
    chunk_frames = int(config.get("chunk_frames", 0) or 0)
    multiplier = int(config.get("multiplier", 4))
    chunks = rife_chunk_ranges(source_frames, chunk_frames, multiplier)
    chunk_paths = []
    expected_fps = float(source_fps) * multiplier

    for number, chunk in enumerate(chunks, start=1):
        workflow, output_node = build_rife_chunk_workflow(
            config, source_video, source_fps, out_prefix, chunk, number)
        prompt_id = client.submit(workflow)
        history = client.wait(prompt_id)
        outputs = outputs_of(history, output_node)
        if not outputs:
            raise RunnerError("RIFE chunk %d/%d produced no video"
                              % (number, len(chunks)))
        video_output, _audio_output = video_and_audio_outputs(outputs)
        video_path = container_path(video_output)
        probe = probe_container_media(container, video_path)
        _validate_rife_media(
            probe, expected_frames=chunk["kept_frames"],
            expected_fps=expected_fps,
            label="RIFE chunk %d/%d" % (number, len(chunks)))
        chunk_paths.append(video_path)
        if on_chunk:
            on_chunk(number, len(chunks), chunk, probe)
        try:
            client.call("/free", {"unload_models": True, "free_memory": True})
        except (OSError, ValueError) as exc:
            raise RunnerError(
                "could not release memory after RIFE chunk %d/%d: %s"
                % (number, len(chunks), exc)) from exc

    output_path = container_path("%s_00001.mp4" % out_prefix)
    audio_output_path = container_path("%s_00001-audio.mp4" % out_prefix)
    manifest_path = container_path("%s-chunks/concat.txt" % out_prefix)
    # MP4 rounds each segment's format duration to milliseconds. Letting the
    # concat demuxer use those rounded values accumulates enough timestamp
    # drift for the later 64->60 fps pass to choose a neighbouring frame at a
    # few boundaries. The exact frame-derived durations keep presentation
    # timestamps continuous without re-encoding or rewriting H.264 B-frame
    # PTS/DTS order.
    manifest = "".join(
        "file %s\nduration %.9f\n"
        % (path, chunk["kept_frames"] / expected_fps)
        for path, chunk in zip(chunk_paths, chunks))
    written = _run_runtime_command(container, [
        "python3", "-c",
        "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')",
        manifest_path, manifest,
    ])
    if written["returncode"] != 0:
        raise RunnerError("could not write RIFE concat manifest:\n%s"
                          % written["stderr"])
    joined = _run_runtime_command(container, [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-f", "concat", "-safe", "0", "-i", manifest_path,
        "-map", "0:v:0", "-c:v", "copy", "-an", output_path,
    ])
    if joined["returncode"] != 0:
        raise RunnerError("could not join RIFE chunks:\n%s" % joined["stderr"])

    expected_frames = (int(source_frames) - 1) * multiplier + 1
    joined_probe = probe_container_media(container, output_path)
    _validate_rife_media(
        joined_probe, expected_frames=expected_frames,
        expected_fps=expected_fps, label="joined RIFE output")

    run_command_stage([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", output_path, "-i", audio_source,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
        "{audio_codec_args}", "-movflags", "use_metadata_tags",
        audio_output_path,
    ], container, audio_source, audio_output_path)
    return {
        "video": output_path,
        "audio": audio_output_path,
        "chunks": chunks,
        "chunk_paths": chunk_paths,
        "expected_frames": expected_frames,
    }


def run_musetalk_stage(root, config, source_video, audio_path, output_name):
    """Run the pinned MuseTalk sidecar against a completed video stage."""
    import os
    import shutil
    import subprocess

    from .plan import musetalk_build_identity

    if shutil.which("docker") is None:
        raise RunnerError("docker is needed to run the MuseTalk stage")
    root = Path(root).resolve()
    outputs = (root / "outputs").resolve()
    assets = (root / "assets").resolve()
    models = (root / "models" / "musetalk").resolve()

    prefix = "/opt/ComfyUI/output/"
    if not str(source_video).startswith(prefix):
        raise RunnerError("MuseTalk source is not a repository output: %s" % source_video)
    source_relative = str(source_video)[len(prefix):]
    source_host = (outputs / source_relative).resolve()
    try:
        source_host.relative_to(outputs)
    except ValueError as exc:
        raise RunnerError("MuseTalk source escapes outputs/: %s" % source_video) from exc
    if not source_host.is_file():
        raise RunnerError("MuseTalk source does not exist: %s" % source_host)

    audio_host = Path(audio_path).resolve()
    try:
        audio_relative = audio_host.relative_to(outputs)
        audio_container = "/data/outputs/%s" % audio_relative.as_posix()
    except ValueError:
        try:
            audio_relative = audio_host.relative_to(assets)
            audio_container = "/data/assets/%s" % audio_relative.as_posix()
        except ValueError as exc:
            raise RunnerError("MuseTalk audio must be under assets/ or outputs/") from exc

    output_relative = Path(output_name)
    output_host = (outputs / output_relative).resolve()
    try:
        output_host.relative_to(outputs)
    except ValueError as exc:
        raise RunnerError("MuseTalk output escapes outputs/: %s" % output_name) from exc
    image = "narration-video-gen-musetalk:%s" % musetalk_build_identity(root)[:12]
    container_name = musetalk_container_name(output_name)
    command = [
        "docker", "run", "--rm", "--name", container_name,
        "--gpus", "all", "--ipc=host",
        "--user", "%d:%d" % (os.getuid(), os.getgid()),
        "-e", "HOME=/tmp/nvg-home",
        "-e", "NVIDIA_VISIBLE_DEVICES=all",
        "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility,video",
        "-v", "%s:/opt/MuseTalk/models:ro" % models,
        "-v", "%s:/data/outputs" % outputs,
        "-v", "%s:/data/assets:ro" % assets,
        image,
        "/data/outputs/%s" % source_relative,
        audio_container,
        "/data/outputs/%s" % output_relative.as_posix(),
        str(int(config.get("bbox_shift", 0))),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RunnerError("MuseTalk failed:\n%s" % (
            completed.stderr.strip() or completed.stdout.strip()))
    if not output_host.is_file():
        raise RunnerError("MuseTalk completed without creating %s" % output_host)
    return container_path(output_relative.as_posix())


def probe_container_video(container, path):
    """Return the exact frame count and frame rate of a runtime video."""
    probe = probe_container_media(container, path)
    return probe["video_frames"], probe["video_fps"]


def probe_container_media(container, path):
    """Return packet-level media evidence from the runtime filesystem."""
    import subprocess

    prefix = _stage_command_prefix(container)
    completed = subprocess.run([
        *prefix, "ffprobe", "-v", "error",
        "-count_frames", "-count_packets",
        "-show_entries",
        "stream=index,codec_type,nb_read_frames,nb_read_packets,avg_frame_rate,duration:format=duration,size",
        "-of", "json", path,
    ], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RunnerError("could not inspect media %s:\n%s"
                          % (path, completed.stderr.strip()))
    try:
        payload = json.loads(completed.stdout)
        video = next(item for item in payload["streams"]
                     if item.get("codec_type") == "video")
        numerator, denominator = video["avg_frame_rate"].split("/", 1)
        audio = next((item for item in payload["streams"]
                      if item.get("codec_type") == "audio"), None)

        def optional_number(value, converter):
            return (converter(value) if value not in (None, "", "N/A") else None)

        return {
            "path": path,
            "video_frames": int(video["nb_read_frames"]),
            "video_packets": int(video["nb_read_packets"]),
            "video_fps": float(numerator) / float(denominator),
            "video_duration_seconds": optional_number(video.get("duration"), float),
            "audio_frames": optional_number(
                audio.get("nb_read_frames") if audio else None, int),
            "audio_packets": optional_number(
                audio.get("nb_read_packets") if audio else None, int),
            "audio_duration_seconds": optional_number(
                audio.get("duration") if audio else None, float),
            "format_duration_seconds": optional_number(
                payload["format"].get("duration"), float),
            "bytes": int(payload["format"]["size"]),
        }
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RunnerError("invalid ffprobe result for media %s" % path) from exc
    except StopIteration as exc:
        raise RunnerError("media %s has no video stream" % path) from exc


def _run_runtime_command(container, command):
    import subprocess

    completed = subprocess.run(
        [*_stage_command_prefix(container), *command],
        capture_output=True, text=True, check=False)
    return {
        "command": [*_stage_command_prefix(container), *command],
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _flush_runtime_file(container, path):
    """Ask the backing filesystem to commit a just-closed encoder output."""
    script = (
        "import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); "
        "os.fsync(fd); os.close(fd)"
    )
    return _run_runtime_command(container, ["python3", "-c", script, path])


def stable_media_probe(container, path, attempts=3, interval=0.25):
    """Require two identical re-opens before another process consumes a file."""
    observations = []
    previous = None
    for _index in range(attempts):
        current = probe_container_media(container, path)
        observations.append(current)
        signature = (current["bytes"], current["video_frames"],
                     current["video_packets"], current["video_duration_seconds"])
        if previous == signature:
            return current, observations
        previous = signature
        time.sleep(interval)
    raise RunnerError(
        "media file did not become stable after %d probes: %s" % (attempts, path),
        code="media-unstable", context={"observations": observations})


def ensure_audio_companion(container, video_path, audio_path, audio_source,
                           expected_frames=None):
    """Validate VHS's second-pass mux and repair a partially copied video.

    VideoHelperSuite writes the exact-frame raw video and then opens that file
    in a second ffmpeg process to attach audio.  On a FUSE-backed workspace the
    second open can very rarely observe a shorter file.  Flush and re-open the
    raw file, compare packet counts, and atomically replace only a mismatched
    audio companion.  The returned record is persisted by the CLI.
    """
    evidence = {
        "schema_version": 1,
        "video": video_path,
        "audio_companion": audio_path,
        "audio_source": audio_source,
        "expected_video_frames": expected_frames,
        "flush": _flush_runtime_file(container, video_path),
    }
    try:
        exact, observations = stable_media_probe(container, video_path)
    except RunnerError as exc:
        evidence.update({
            "status": "raw-unstable", "error": str(exc),
            "video_stability_probes": exc.context.get("observations", []),
        })
        raise RunnerError(
            str(exc), code="media-contract-failed",
            context={"media_contract": evidence}) from exc
    evidence["video_stability_probes"] = observations
    if (expected_frames is not None
            and exact["video_frames"] != int(expected_frames)):
        evidence.update({
            "status": "unexpected-raw-frame-count",
            "error": "raw video has %d frames; expected %d"
                     % (exact["video_frames"], int(expected_frames)),
        })
        if audio_path != video_path:
            try:
                evidence["audio_companion_before"] = probe_container_media(
                    container, audio_path)
            except RunnerError as exc:
                evidence["audio_companion_probe_error"] = str(exc)
        # Do not use an overlong FramePack raw file to overwrite a correctly
        # audio-trimmed companion.  The workflow must cap decoded images before
        # VHS; a mismatch here is an output-contract failure, not mux damage.
        raise RunnerError(
            evidence["error"], code="media-contract-failed",
            context={"media_contract": evidence})
    if audio_path == video_path:
        evidence.update({"status": "no-companion", "video_after": exact})
        return evidence

    companion = probe_container_media(container, audio_path)
    evidence.update({"video_before": exact, "audio_companion_before": companion})
    if (companion["video_frames"] == exact["video_frames"]
            and companion["video_packets"] == exact["video_packets"]):
        evidence["status"] = "matched"
        return evidence

    audio = Path(audio_path)
    temporary = str(audio.with_name(
        "%s.mux-repair-%s%s" % (audio.stem, uuid.uuid4().hex, audio.suffix)))
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-i", video_path, "-i", audio_source,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "1",
        # The exact raw was planned from this narration.  Do not repeat VHS's
        # -shortest cut here: AAC encoder delay can otherwise remove valid
        # audio packets while repairing the video stream.
        "-movflags", "use_metadata_tags", temporary,
    ]
    remux = _run_runtime_command(container, command)
    evidence["repair_command"] = remux
    if remux["returncode"] != 0:
        _run_runtime_command(container, ["rm", "-f", "--", temporary])
        evidence.update({"status": "repair-failed", "error": remux["stderr"]})
        raise RunnerError(
            "audio companion repair failed for %s:\n%s"
            % (audio_path, remux["stderr"]), code="media-contract-failed",
            context={"media_contract": evidence})
    replace = _run_runtime_command(
        container, ["mv", "-f", "--", temporary, audio_path])
    evidence["replace_command"] = replace
    if replace["returncode"] != 0:
        _run_runtime_command(container, ["rm", "-f", "--", temporary])
        evidence.update({"status": "replace-failed", "error": replace["stderr"]})
        raise RunnerError(
            "could not install repaired audio companion %s:\n%s"
            % (audio_path, replace["stderr"]), code="media-contract-failed",
            context={"media_contract": evidence})

    repaired, repaired_observations = stable_media_probe(container, audio_path)
    evidence["audio_companion_after"] = repaired
    evidence["repair_stability_probes"] = repaired_observations
    if (repaired["video_frames"] != exact["video_frames"]
            or repaired["video_packets"] != exact["video_packets"]):
        evidence["status"] = "repair-mismatch"
        raise RunnerError(
            "repaired audio companion still differs from raw video: %d/%d frames"
            % (exact["video_frames"], repaired["video_frames"]),
            code="media-contract-failed", context={"media_contract": evidence})
    evidence["status"] = "repaired"
    return evidence
