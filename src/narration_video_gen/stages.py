"""Pipeline stages.

A run is a sequence of stages. The first one generates video from an image and
an audio track; every later stage takes a **video file path** and produces
another one.

That boundary is the whole point. Because the post-stages consume a video rather
than an in-memory batch, they are not tied to the model that produced it and can
also run on their own against a video that already exists.

Each builder returns a ComfyUI workflow plus the id of the node whose output is
the stage's result.
"""

from __future__ import annotations

# Stage ids, in the order they may appear in a pipeline.
GENERATE_STAGES = ("infinitetalk", "s2v")
POST_STAGES = ("musetalk", "face-detailer", "rife", "retime")
ALL_STAGES = GENERATE_STAGES + POST_STAGES


class StageError(ValueError):
    pass


def _loader_media_name(path):
    """Translate a runtime media path for ComfyUI's LoadAudio node."""
    value = str(path or "")
    input_prefix = "/opt/ComfyUI/input/"
    output_prefix = "/opt/ComfyUI/output/"
    if value.startswith(input_prefix):
        return value[len(input_prefix):]
    if value.startswith(output_prefix):
        return "%s [output]" % value[len(output_prefix):]
    raise StageError("audio source is outside the ComfyUI input/output roots: %s"
                     % value)


def order_stages(stages):
    """Return ``stages`` in execution order, rejecting unknown or bad orderings."""
    unknown = [s for s in stages if s not in ALL_STAGES]
    if unknown:
        raise StageError("unknown stage(s): %s (known: %s)"
                         % (", ".join(unknown), ", ".join(ALL_STAGES)))
    generators = [s for s in stages if s in GENERATE_STAGES]
    if len(generators) > 1:
        raise StageError("a pipeline has at most one generating stage, got %s"
                         % ", ".join(generators))
    ordered = sorted(stages, key=ALL_STAGES.index)
    if generators and ordered[0] not in GENERATE_STAGES:
        raise StageError("the generating stage must come first")
    return ordered


def needs_source_video(stages):
    """True when the pipeline starts from an existing video rather than an image."""
    return not any(s in GENERATE_STAGES for s in stages)


def wan_vae_loader_inputs(model_name, settings):
    inputs = {"model_name": model_name, "precision": "bf16"}
    if settings.get("vae_native_upsample", False):
        inputs["native_upsample"] = True
    return inputs


# ---------------------------------------------------------------------------
# stage 1b: Wan2.2 S2V generation
# ---------------------------------------------------------------------------

S2V_POSITIVE = (
    "The video begins from the exact reference image. The same subject speaks directly to "
    "the camera with lively, natural facial expressions, natural head movement, and "
    "expressive hand gestures. Preserve identity, clothing, background, lighting, and "
    "framing. Photorealistic live-action footage, natural skin texture, fine facial detail, "
    "realistic lips and teeth, stable camera, single continuous shot."
)

S2V_NEGATIVE = (
    "overexposed, static, blurred details, subtitles, worst quality, low quality, "
    "JPEG compression residue, ugly, incomplete, extra fingers, poorly drawn hands, "
    "poorly drawn faces, deformed, disfigured, misshapen limbs, fused fingers, "
    "still picture, messy background, frozen body, motionless"
)


def build_s2v(recipe, profile, models, image_name, audio_name, frames, out_prefix):
    """Wan2.2-S2V: image + audio -> video at the recipe's resolution and fps."""
    width, height = recipe["resolution"]
    sampler = recipe["sampler"]
    context = recipe["context"]
    settings = profile.get("settings") or {}
    vae = recipe["vae"]
    s2v = recipe.get("s2v") or {}
    start_from_reference = bool(s2v.get("start_from_reference", False))
    drop = 0 if start_from_reference else recipe.get("drop_first_frames", 0)

    # S2V decodes a few motion/warm-up frames that are not part of the output.
    # Generate in whole groups of four so that dropping them still leaves the
    # requested 4n+1 count: for the default 3-frame drop, generate 133, save 129.
    extra = 4 * ((drop + 3) // 4) if drop else 0
    generation_frames = frames + extra

    workflow = {
        "1": {"class_type": "WanVideoBlockSwap", "inputs": {
            "blocks_to_swap": settings["blocks_to_swap"],
            "offload_img_emb": False, "offload_txt_emb": False,
            "use_non_blocking": settings.get("use_non_blocking", False),
            "vace_blocks_to_swap": 0, "prefetch_blocks": 0,
            "block_swap_debug": False}},
        "2": {"class_type": "WanVideoModelLoader", "inputs": {
            "model": models["s2v"], "base_precision": "fp16_fast",
            "quantization": s2v.get("quantization", "fp8_e4m3fn_scaled"),
            "load_device": "offload_device",
            "attention_mode": sampler["attention"], "block_swap_args": ["1", 0]}},
        "3": {"class_type": "WanVideoVAELoader", "inputs":
            wan_vae_loader_inputs(models["vae"], settings)},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "ImageResizeKJv2", "inputs": {
            "image": ["4", 0], "width": width, "height": height,
            "upscale_method": "lanczos", "keep_proportion": "crop",
            "pad_color": "0, 0, 0", "crop_position": "center",
            "divisible_by": 16, "device": "cpu"}},
        "6": {"class_type": "WanVideoEncode", "inputs": {
            "vae": ["3", 0], "image": ["5", 0],
            "enable_vae_tiling": settings.get("encode_tiled", vae["encode_tiled"]),
            "tile_x": 272, "tile_y": 272, "tile_stride_x": 144, "tile_stride_y": 128,
            "noise_aug_strength": 0.0, "latent_strength": 1.0}},
        "7": {"class_type": "LoadAudio", "inputs": {"audio": audio_name}},
        "8": {"class_type": "AudioEncoderLoader", "inputs": {
            "audio_encoder_name": models["audio_encoder"]}},
        "9": {"class_type": "AudioEncoderEncode", "inputs": {
            "audio_encoder": ["8", 0], "audio": ["7", 0]}},
        "10": {"class_type": "WanVideoTextEncodeCached", "inputs": {
            "model_name": models["text_encoder"], "precision": "bf16",
            "positive_prompt": (recipe.get("prompt") or {}).get(
                "positive", S2V_POSITIVE),
            "negative_prompt": (recipe.get("prompt") or {}).get(
                "negative", S2V_NEGATIVE),
            "quantization": "disabled", "use_disk_cache": True, "device": "gpu"}},
        "11": {"class_type": "WanVideoEmptyEmbeds", "inputs": {
            "width": width, "height": height, "num_frames": generation_frames}},
        "12": {"class_type": "WanVideoAddS2VEmbeds", "inputs": {
            "embeds": ["11", 0], "audio_encoder_output": ["9", 0],
            "ref_latent": ["6", 0], "frame_window_size": context["frames"],
            "audio_scale": sampler["audio_scale"],
            "pose_start_percent": 0.0, "pose_end_percent": 1.0,
            "enable_framepack": False}},
        "13": {"class_type": "WanVideoContextOptions", "inputs": {
            "context_schedule": context["schedule"],
            "context_frames": context["frames"],
            "context_stride": context["stride"],
            "context_overlap": context["overlap"],
            "freenoise": True, "verbose": False, "fuse_method": "linear"}},
        "14": {"class_type": "WanVideoSampler", "inputs": {
            "model": ["2", 0], "image_embeds": ["12", 0], "text_embeds": ["10", 0],
            "context_options": ["13", 0],
            "steps": sampler["steps"], "cfg": sampler["cfg"], "shift": sampler["shift"],
            "seed": sampler["seed"], "force_offload": True, "scheduler": "dpm++_sde",
            "riflex_freq_index": 0, "denoise_strength": 1.0,
            "batched_cfg": False, "rope_function": "comfy"}},
        "15": {"class_type": "WanVideoDecode", "inputs": {
            "vae": ["3", 0], "samples": ["14", 0],
            "enable_vae_tiling": settings.get("decode_tiled", vae["decode_tiled"]),
            "tile_x": 272, "tile_y": 272, "tile_stride_x": 144, "tile_stride_y": 128,
            "normalization": "default"}},
        "16": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["15", 0], "audio": ["7", 0],
            "frame_rate": float(recipe["fps"]), "loop_count": 0,
            "filename_prefix": out_prefix, "format": "video/h264-mp4",
            "pix_fmt": "yuv420p", "crf": 10, "save_metadata": True,
            "trim_to_audio": True, "pingpong": False, "save_output": True}},
        "17": {"class_type": "WanVideoLoraSelectMulti", "inputs": {
            "lora_0": models["lora"], "strength_0": recipe["lora"]["strength"],
            "lora_1": "none", "strength_1": 1.0,
            "lora_2": "none", "strength_2": 1.0,
            "lora_3": "none", "strength_3": 1.0,
            "lora_4": "none", "strength_4": 1.0,
            "low_mem_load": False, "merge_loras": False}},
        "18": {"class_type": "WanVideoSetLoRAs", "inputs": {
            "model": ["2", 0], "lora": ["17", 0]}},
    }
    workflow["14"]["inputs"]["model"] = ["18", 0]

    if start_from_reference:
        workflow["12"]["inputs"].update({
            "frame_window_size": int(s2v.get("framepack_frames", 80)),
            "ref_image": ["5", 0],
            "vae": ["3", 0],
            "enable_framepack": True,
            "start_from_ref": True,
            "ref_hold_frames": int(s2v.get("reference_hold_frames", 6)),
            "offload_transformer_before_vae_decode": bool(
                settings.get("offload_transformer_before_vae_decode", False)),
        })
        workflow["14"]["inputs"].pop("context_options")
        workflow.pop("13")

    # FramePack samples complete windows.  For example, an 89-frame short run
    # with an 80-frame window decodes 160 frames even though only the first 89
    # belong to the planned narration.  Cap every S2V output before VHS writes
    # either its raw file or its audio-bearing companion; trim_to_audio alone
    # only shortens the latter and leaves the raw sibling at the window size.
    workflow["19"] = {"class_type": "ImageFromBatch", "inputs": {
        "image": ["15", 0], "batch_index": drop, "length": frames}}
    workflow["16"]["inputs"]["images"] = ["19", 0]

    return workflow, "16"


# ---------------------------------------------------------------------------
# stage 2: face detailer
# ---------------------------------------------------------------------------

FACE_POSITIVE = (
    "The same subject's face. Preserve exact identity, facial proportions, skin tone, original "
    "expression, and speaking mouth shape. Natural skin texture, precise stable eyes, eyebrows, "
    "lips and teeth, photorealistic, sharp but natural facial detail."
)

FACE_NEGATIVE = (
    "identity change, different person, changed expression, lip shape drift, lip sync error, "
    "plastic skin, oversharpening, flicker, shimmer, deformed face, asymmetrical eyes, blur, "
    "low quality, jpeg artifacts"
)


def build_face_detailer(config, models, source_video, frames, fps, out_prefix,
                        audio_source=None, mask_only=False):
    """SAM2-tracked face crop plus a low-noise VACE detail pass, then uncrop.

    Consumes a video path, so it does not care which model produced it.
    """
    points = config.get("points") or {}
    positive = points.get("positive")
    negative = points.get("negative")

    workflow = {
        "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": source_video, "force_rate": int(fps),
            "custom_width": 0, "custom_height": 0,
            "frame_load_cap": frames, "skip_first_frames": 0,
            "select_every_nth": 1, "format": "None"}},
        "2": {"class_type": "DownloadAndLoadSAM2Model", "inputs": {
            "model": models["sam2"], "segmentor": "video",
            "device": config.get("sam_device", "cuda"),
            "precision": config.get("sam_precision", "fp16")}},
        "5": {"class_type": "Sam2Segmentation", "inputs": {
            "sam2_model": ["2", 0], "image": ["1", 0], "keep_model_loaded": False,
            "individual_objects": False}},
        "6": {"class_type": "GrowMaskWithBlur", "inputs": {
            "mask": ["5", 0], "expand": config.get("mask_expand", 12),
            "incremental_expandrate": 0.0, "tapered_corners": True,
            "flip_input": False, "blur_radius": config.get("mask_blur", 24.0),
            "lerp_alpha": 1.0, "decay_factor": 1.0, "fill_holes": True}},
    }

    if positive:
        # A custom recipe can record explicit points for inputs on which the
        # detector cannot operate. Standard recipes detect the face instead.
        workflow["3"] = {"class_type": "StringConstant", "inputs": {
            "string": _points(positive)}}
        workflow["5"]["inputs"]["coordinates_positive"] = ["3", 0]
        if negative:
            workflow["4"] = {"class_type": "StringConstant", "inputs": {
                "string": _points(negative)}}
            workflow["5"]["inputs"]["coordinates_negative"] = ["4", 0]
    else:
        workflow["3"] = {"class_type": "NVGDetectPrimaryFace", "inputs": {
            "image": ["1", 0], "model_name": models["face_detector"],
            "score_threshold": config.get("face_detection_score", 0.7)}}
        workflow["5"]["inputs"]["bboxes"] = ["3", 0]

    if mask_only:
        # Rendering just the mask is how you check the tracking points before
        # committing to a pass that takes the better part of an hour.
        workflow["7"] = {"class_type": "MaskToImage", "inputs": {"mask": ["6", 0]}}
        workflow["8"] = {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["7", 0], "frame_rate": float(fps), "loop_count": 0,
            "filename_prefix": "%s_mask" % out_prefix, "format": "video/h264-mp4",
            "pix_fmt": "yuv420p", "crf": 10, "save_metadata": True,
            "trim_to_audio": False, "pingpong": False, "save_output": True}}
        return workflow, "8"

    if not audio_source:
        raise StageError("face detail needs an explicit audio source")

    crop = config.get("crop_size_mult", 0.75)
    detail_size = int(config.get("size", 512))
    if detail_size < 16 or detail_size % 16:
        raise StageError("face detailer size must be a positive multiple of 16")
    workflow.update({
        "9": {"class_type": "BatchCropFromMaskAdvanced", "inputs": {
            "original_images": ["1", 0], "masks": ["6", 0],
            "crop_size_mult": crop, "bbox_smooth_alpha": 0.5}},
        # The detail pass uses a profile-scoped square activation plane on the
        # cropped face, independent of the source video's resolution.
        "10": {"class_type": "ImageResizeKJv2", "inputs": {
            "image": ["9", 1], "width": detail_size, "height": detail_size,
            "upscale_method": "lanczos", "keep_proportion": "stretch",
            "pad_color": "0, 0, 0", "crop_position": "center",
            "divisible_by": 16, "device": "cpu"}},
        "11": {"class_type": "WanVideoVAELoader", "inputs":
            wan_vae_loader_inputs(models["vae"], config)},
        "12": {"class_type": "WanVideoEncode", "inputs": {
            "vae": ["11", 0], "image": ["10", 0], "enable_vae_tiling": False,
            "tile_x": 512, "tile_y": 512, "tile_stride_x": 256, "tile_stride_y": 256,
            "noise_aug_strength": 0.0, "latent_strength": 1.0}},
        "13": {"class_type": "VHS_SelectImages", "inputs": {
            "image": ["9", 1], "indexes": "0",
            "err_if_missing": True, "err_if_empty": True}},
        "14": {"class_type": "VHS_SelectImages", "inputs": {
            "image": ["9", 1], "indexes": "-1",
            "err_if_missing": True, "err_if_empty": True}},
        "15": {"class_type": "WanVideoVACEStartToEndFrame", "inputs": {
            "start_image": ["13", 0], "end_image": ["14", 0], "num_frames": frames,
            "empty_frame_level": 0.5, "start_index": 0, "end_index": -1}},
        "16": {"class_type": "WanVideoVACEEncode", "inputs": {
            "vae": ["11", 0], "width": detail_size, "height": detail_size,
            "num_frames": frames,
            "strength": config.get("vace_strength", 0.6),
            "vace_start_percent": 0.0, "vace_end_percent": 1.0,
            "input_frames": ["15", 0], "input_masks": ["15", 1], "tiled_vae": False}},
        "17": {"class_type": "WanVideoVACEModelSelect", "inputs": {
            "vace_model": models["vace"]}},
        "18": {"class_type": "WanVideoBlockSwap", "inputs": {
            "blocks_to_swap": config.get("blocks_to_swap", 40),
            "offload_img_emb": False, "offload_txt_emb": False,
            "use_non_blocking": False, "vace_blocks_to_swap": 15,
            "prefetch_blocks": 1, "block_swap_debug": False}},
        "19": {"class_type": "WanVideoLoraSelect", "inputs": {
            "lora": models["t2v_lora"], "strength": 1.0,
            "low_mem_load": False, "merge_loras": False}},
        "20": {"class_type": "WanVideoModelLoader", "inputs": {
            "model": models["t2v"], "base_precision": "fp16_fast",
            "quantization": "disabled", "load_device": "offload_device",
            "attention_mode": "sdpa", "block_swap_args": ["18", 0],
            "lora": ["19", 0], "extra_model": ["17", 0],
            "rms_norm_function": "default"}},
        "21": {"class_type": "LoadWanVideoT5TextEncoder", "inputs": {
            "model_name": models["text_encoder"], "precision": "bf16",
            "load_device": "offload_device", "quantization": "fp8_e4m3fn"}},
        "22": {"class_type": "WanVideoTextEncode", "inputs": {
            "positive_prompt": config.get("positive_prompt", FACE_POSITIVE),
            "negative_prompt": config.get("negative_prompt", FACE_NEGATIVE),
            "t5": ["21", 0], "force_offload": True,
            "use_disk_cache": True, "device": "gpu"}},
        # start_step 4 of 6 is what makes this a detail pass rather than a
        # regeneration: only the last two steps run, so the existing face is
        # refined instead of replaced.
        "23": {"class_type": "WanVideoSampler", "inputs": {
            "model": ["20", 0], "image_embeds": ["16", 0], "text_embeds": ["22", 0],
            "samples": ["12", 0], "steps": 6, "cfg": 1.0, "shift": 5.0,
            "seed": config.get("seed", 12345), "force_offload": True,
            "scheduler": "dpm++_sde", "riflex_freq_index": 0,
            "denoise_strength": 1.0, "batched_cfg": False, "rope_function": "comfy",
            "start_step": 4, "end_step": -1, "add_noise_to_samples": True}},
        "24": {"class_type": "WanVideoDecode", "inputs": {
            "vae": ["11", 0], "samples": ["23", 0], "enable_vae_tiling": False,
            "tile_x": 512, "tile_y": 512, "tile_stride_x": 256, "tile_stride_y": 256,
            "normalization": "default"}},
        "25": {"class_type": "BatchUncropAdvanced", "inputs": {
            "original_images": ["9", 0], "cropped_images": ["24", 0],
            "cropped_masks": ["9", 2], "combined_crop_mask": ["9", 4],
            "bboxes": ["9", 5], "combined_bounding_box": ["9", 6],
            "border_blending": 1.0, "crop_rescale": 1.0,
            "use_combined_mask": False, "use_square_mask": True}},
        "27": {"class_type": "LoadAudio", "inputs": {
            "audio": _loader_media_name(audio_source)}},
        "26": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["25", 0], "audio": ["27", 0],
            "frame_rate": float(fps), "loop_count": 0,
            "filename_prefix": out_prefix, "format": "video/h264-mp4",
            "pix_fmt": "yuv420p", "crf": 10, "save_metadata": True,
            # The source video was already trimmed to its audio; trimming again
            # here would compound the tail slack.
            "trim_to_audio": False, "pingpong": False, "save_output": True}},
    })
    return workflow, "26"


def _points(points):
    """Serialise ``[{'x': .., 'y': ..}, ...]`` the way the SAM2 node expects."""
    import json
    return json.dumps(points or [], separators=(",", ":"))


# ---------------------------------------------------------------------------
# stage 3: RIFE interpolation
# ---------------------------------------------------------------------------

def build_rife(config, source_video, source_fps, out_prefix, audio_source=None):
    """Interpolate an existing video by an integer factor.

    Nothing here is model-specific, so this stage can be run on its own against
    any finished video -- including a Wan2.1 render.

    RIFE only doubles or quadruples. Landing on a frame rate that is not an
    integer multiple of the source -- 60 fps from 16 fps, say -- is the job of
    the separate ``retime`` stage, which is what the published runs did.
    """
    multiplier = config.get("multiplier", 4)
    if multiplier not in (2, 4):
        raise StageError(
            "RIFE interpolates by a factor of 2 or 4, not %r. To reach a frame "
            "rate that is not an integer multiple, add the `retime` stage."
            % (multiplier,)
        )
    include_audio = config.get("include_audio", True)
    if include_audio and not audio_source:
        raise StageError("RIFE needs an explicit audio source")

    frame_load_cap = int(config.get("frame_load_cap", 0) or 0)
    skip_first_frames = int(config.get("skip_first_frames", 0) or 0)
    drop_first_output_frame = bool(config.get("drop_first_output_frame", False))
    output_frame_count = config.get("output_frame_count")
    if frame_load_cap < 0 or skip_first_frames < 0:
        raise StageError("RIFE frame ranges cannot be negative")
    if drop_first_output_frame:
        if output_frame_count is None or int(output_frame_count) < 2:
            raise StageError(
                "chunked RIFE needs output_frame_count to drop its overlap frame")
        output_frame_count = int(output_frame_count)

    workflow = {
        "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": source_video, "force_rate": 0,
            "custom_width": 0, "custom_height": 0,
            "frame_load_cap": frame_load_cap,
            "skip_first_frames": skip_first_frames,
            "select_every_nth": 1, "format": "None"}},
        "2": {"class_type": "RIFE VFI", "inputs": {
            "ckpt_name": config.get("model", "rife49") + ".pth",
            "frames": ["1", 0],
            # Interpolation holds decoded frames in memory; clearing the cache
            # keeps a full-length run from growing without bound.
            "clear_cache_after_n_frames": 10,
            "multiplier": multiplier, "fast_mode": False, "ensemble": True,
            "scale_factor": 1.0, "dtype": "float16",
            "torch_compile": False, "batch_size": 1}},
        "3": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["2", 0], "audio": ["4", 0],
            "frame_rate": float(source_fps) * multiplier, "loop_count": 0,
            "filename_prefix": out_prefix, "format": "video/h264-mp4",
            "pix_fmt": "yuv420p", "crf": 10, "save_metadata": True,
            "trim_to_audio": include_audio, "pingpong": False,
            "save_output": True}},
    }
    if drop_first_output_frame:
        workflow["5"] = {"class_type": "ImageFromBatch", "inputs": {
            "image": ["2", 0], "batch_index": 1,
            "length": output_frame_count - 1}}
        workflow["3"]["inputs"]["images"] = ["5", 0]
    if include_audio:
        workflow["4"] = {"class_type": "LoadAudio", "inputs": {
            "audio": _loader_media_name(audio_source)}}
    else:
        workflow["3"]["inputs"].pop("audio")
    return workflow, "3"


# ---------------------------------------------------------------------------
# stage 4: retime
# ---------------------------------------------------------------------------

def build_retime(config, source_video, audio_source):
    """Convert to a frame rate RIFE cannot reach, and restore the exact audio.

    RIFE gets from 16 fps to 64; 60 fps is a resample away from that, done in
    ffmpeg rather than by interpolating again.

    Two details here are not cosmetic. The audio is the pipeline's earliest
    canonical narration track and the output is cut to its measured duration,
    because every visual post-stage mux can add or remove AAC padding. And a
    single cloned frame is padded onto the end first: resampling 64 fps to 60
    leaves the video a few milliseconds shorter than the audio, and without the
    pad the last frame is dropped instead of held.

    Returns an ffmpeg argument list to run inside the container, not a workflow.
    ``{audio_codec_args}``, ``{audio_duration}``, and ``{output}`` are
    placeholders the runner fills in after probing the canonical audio track.
    """
    target_fps = config.get("target_fps")
    if not target_fps:
        raise StageError("the retime stage needs postprocess.retime.target_fps")

    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", source_video,
        "-i", audio_source,
        "-filter_complex",
        "[0:v]fps=%s,tpad=stop_mode=clone:stop_duration=0.05[v]" % target_fps,
        "-map", "[v]", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "10", "-pix_fmt", "yuv420p",
        "{audio_codec_args}",
        # -t comes from the measured audio duration; a hard-coded length
        # silently truncates a narration of a different size.
        "-t", "{audio_duration}",
        "{output}",
    ]
