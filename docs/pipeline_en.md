[日本語](pipeline.md) | **English**

# Pipeline

A run is a sequence of stages. The first stage generates a video from the image
and audio; each later stage takes a video file and produces a new one.

```
infinitetalk ─▶ [musetalk] ─┐
                            ├─▶ face-detailer ─▶ rife ─▶ retime
s2v ────────────────────────┘
```

| Stage | Input | Output | What it does |
|---|---|---|---|
| `infinitetalk` | image + audio | video | Wan 2.1 + InfiniteTalk generation |
| `s2v` | image + audio | video | Wan 2.2-S2V generation |
| `musetalk` | video + audio | video | Experimental MuseTalk 1.5 lip regeneration |
| `face-detailer` | video | video | Detects and tracks the face, then refines it with VACE |
| `rife` | video | video | RIFE frame interpolation (16 → 64 fps) |
| `retime` | video | video | Converts to 60 fps and restores the original audio |

Stage outputs are saved as `outputs/<run-id>/<NN>-<stage>.mp4`.

## Prompts

The person, clothing, background and lighting come from the reference image.
The standard prompts describe only motion and image quality, so the same recipe
works for any portrait. For specific direction, set `prompt` in a custom recipe.

## Running some stages only

`--stages` runs a subset of the pipeline. If the first stage is a post-stage,
pass the input video with `--source-video`.

```bash
# The full pipeline
./bin/narration-video-gen run --profile linux-wan22-480p-vram16 \
  --image p.png --audio n.wav --length short

# Interpolation only, on an existing video
./bin/narration-video-gen run --profile linux-wan21-480p-vram16 --stages rife,retime \
  --source-video outputs/existing.mp4

# Check face tracking before the face refinement pass
./bin/narration-video-gen run --profile linux-wan22-480p-vram16 --stages face-detailer \
  --source-video outputs/existing.mp4 --mask-only
```

Stages always run in pipeline order.

## Resuming after a failure

When a stage fails, the outputs of the completed stages are listed. Continue
from the failed stage:

```bash
./bin/narration-video-gen run --profile <profile-id> --stages face-detailer,rife,retime \
  --source-video outputs/<run-id>/01-s2v.mp4
```

## Face refinement

The largest face in the first frame is detected and tracked through the video.
If no face is found, the stage stops. For videos with several people or unusual
framing, set coordinates in a custom recipe under
`postprocess.face_detailer.points`. Use `--mask-only` to check the tracking.

Face refinement can be turned on or off in `plan --advanced`, or with
`plan --face-detailer on|off`. When it is off, its models are not downloaded.

The following settings can be set in a
[custom profile](../examples/profiles/README.md):

| Setting | Meaning | Default |
|---|---|---|
| `face_detailer_enabled` | Turn face refinement on or off | `true` |
| `face_detailer_size` | Size of the square region processed around the face; a multiple of 16 | `320` |
| `face_detailer_blocks_to_swap` | Block swap for this stage (0–40) | same as `blocks_to_swap` |

If this stage runs out of GPU memory, raise `face_detailer_blocks_to_swap` or
lower `face_detailer_size`. A smaller size uses less memory but gives less
facial detail.

## 60 fps finish

The 60 fps finish is on by default. To keep the source frame rate (16 fps):

```bash
./bin/narration-video-gen plan --frame-interpolation off
```

The setting is saved for later `run` commands. Use
`plan --frame-interpolation on` to turn it back on.

## Wan 2.1 and MuseTalk

The standard Wan 2.1 pipeline is `infinitetalk → face-detailer → rife → retime`.
`plan --lip-sync-enhancement musetalk` switches to
`infinitetalk → musetalk → face-detailer → rife → retime`. MuseTalk is
experimental and verified only for short 480p clips.
