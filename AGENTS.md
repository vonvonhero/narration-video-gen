# AGENTS.md — helping a user run narration-video-gen

This file is for an AI coding agent that a user has pointed at this folder to
set up the tool, make a video or narration, or find out why something failed.
The user is usually not a developer of this project. Your job is to operate the
tool for them, not to change it.

Reply in the user's language. Every command below accepts `NVG_UI_LANGUAGE=ja`
or `NVG_UI_LANGUAGE=en` if you need its messages in a particular language.

## What this tool does

It turns one portrait image and one narration WAV into a talking-head video
(Wan 2.1 + InfiniteTalk or Wan 2.2 S2V, then face refinement, RIFE and a 60 fps
finish). It can also synthesise the narration from a script (Irodori-TTS).
Video generation needs an NVIDIA GPU and Docker; narration also works on CPU.
Everything heavy runs in containers. Nothing is installed into the host Python.

## Ground rules

Stop and ask the user before you:

1. Install or change anything on the host: NVIDIA driver, Docker, NVIDIA
   Container Toolkit, apt packages, swap, `/etc/fstab`, `sysctl`, `.wslconfig`,
   or `wsl --shutdown` (it stops every WSL distribution and container).
2. Download models (tens of GiB; each has its own licence in
   `THIRD_PARTY_NOTICES.md`) or build the runtime image (large and slow).
3. Start a GPU generation or `calibrate`. A full-length video takes from about
   20 minutes to several hours. Show the `plan` estimate first and suggest a
   `--length short` test (about 5.6 seconds) before the first full run.
4. Delete models, outputs or characters, or run the cleanup scripts.
5. Expose anything on the network (`tts web --lan`). Never publish ComfyUI's
   port 8188: it has no authentication.
6. Use `--force`, or a profile that `select` reports as not fully verified
   (short, capacity-simulated, not reviewed, `experimental` or
   `not-recommended`). Tell the user what the limitation is and let them decide.

Never:

* Edit files under `profiles/`, `recipes/`, `manifests/`, `calibration/`,
  `docker/` or `src/` to work around a failure. Settings are data: a GPU/memory
  setting belongs in a profile and a generation setting in a recipe. If a
  different setting is needed, create a custom profile outside the checkout
  (see `examples/profiles/README.md`) and pass `--profile-dir`, or run
  `calibrate`.
* Report a video as reviewed. Only the user can watch it. `verify` checks
  resolution, frame count and audio, not whether the face looks right. Do not
  pass `report --visual-review passed` unless the user said they watched it.
* Commit or upload images, audio or generated videos, or anything under
  `models/`, `outputs/`, `inputs/`, `voices/`, `cache/` or `results/`.
* Print tokens, passwords or the contents of `.env` files.

## Where the working copy is

* **Linux (Ubuntu 24.04 / 26.04, amd64):** this folder is the working copy. Run
  every command from its root.
* **Windows:** the downloaded folder only contains the launcher. `setup.cmd`
  creates the real working copy inside WSL at `~/narration-video-gen`, and
  models and outputs live there. Run the CLI inside WSL, for example
  `wsl -- bash -lc 'cd ~/narration-video-gen && ./bin/narration-video-gen status'`.
  Finished videos are also copied to `Videos\Narration Video Gen\<run-id>\`.

## Setup

Check first; both of these are read-only:

```bash
scripts/setup-linux.sh --check            # Linux
./bin/narration-video-gen --json detect   # GPU, RAM, swap, disk, Docker
```

```powershell
.\setup.cmd -Check                        # Windows, from the downloaded folder
```

Setup itself is an interactive wizard that asks for confirmation at each host
change and may require a reboot or re-login. Prefer asking the user to run it
in their own terminal (`scripts/setup-linux.sh`, or double-click `setup.cmd`),
then re-run the check. On Linux, the individual steps can be run explicitly
after the user agrees: `--setup` (Docker and NVIDIA Container Toolkit),
`--install-driver`, `--test-gpu`, each with `--yes`. For narration only, Docker
is enough; no GPU setup is needed.

## Make a video

Commands that ask questions only do so on a terminal. Without a TTY, and with
`--json`, they never prompt, so pass every choice as an option.

1. **Choose a configuration.**

   ```bash
   ./bin/narration-video-gen --json select --explain
   ./bin/narration-video-gen --json plan --model wan22 --resolution 480p --seconds <audio seconds>
   ```

   `plan` reports eligibility, blockers, missing models, whether the runtime
   image is built, free disk and the estimated time. Without a terminal it only
   checks. Note the profile id it chose.

2. **Prepare (after the user agrees to the download and build shown by plan).**
   Either ask the user to run `./bin/narration-video-gen plan` in a terminal and
   confirm, or run the same steps plan would run:

   ```bash
   scripts/download-models.sh --profile <profile-id>
   scripts/build-image.sh          # add --with-musetalk only for the MuseTalk option
   ./bin/narration-video-gen --json plan --profile <profile-id>   # confirm nothing is missing
   ```

   Both are resumable; re-run them after an interruption. `./bin/narration-video-gen hashes`
   verifies downloaded weights.

3. **Pick inputs.** Bundled characters are under `assets/characters/`. A user's
   own set is a folder `inputs/<name>/` with one image and one mono WAV (see
   `inputs/README.md`).

4. **Run.** Without a terminal, generation runs in the foreground of that
   command and does not start the ComfyUI container itself, so start it first
   and run the generation detached:

   ```bash
   scripts/up.sh up -d
   mkdir -p outputs/<run-id>
   nohup ./bin/narration-video-gen run --profile <profile-id> \
     --image <image> --audio <wav> --length short --run-id <run-id> \
     >> outputs/<run-id>/run.log 2>&1 &
   ./bin/narration-video-gen --json status --run-id <run-id>
   ```

   Use `--dry-run` first if you want to show the workflow without generating.
   Poll `status` at long intervals (minutes, not seconds); `cancel --run-id <id>`
   stops it. Results are in `outputs/<run-id>/`.

5. **Check.** `./bin/narration-video-gen verify <video> --profile <profile-id> --audio <wav>`,
   then ask the user to watch the video.

## Make a narration

```bash
./bin/narration-video-gen tts plan --script <file>        # how the script is split
./bin/narration-video-gen tts prepare                     # first time: builds and downloads (~3.7 GB)
./bin/narration-video-gen tts backend start
./bin/narration-video-gen tts generate --script <file> --character aoi --output inputs/<name>/audio.wav
./bin/narration-video-gen tts adopt <job-id> --name <name> --confirm-listened
```

`tts prepare` downloads models, so ask first. Use `--confirm-listened` only
after the user has listened to every part. For most users the browser page is
easier: `./bin/narration-video-gen tts` (see `docs/tts_en.md`). Video generation
stops the TTS engine to free GPU memory; the page keeps working and restarts
it on the next synthesis.

## When something fails

1. `./bin/narration-video-gen --json status --run-id <run-id>` gives the error
   code, a short explanation and suggested remedies. Start there.
2. The full log is `outputs/<run-id>/run.log`; the state is
   `outputs/<run-id>/run-state.json`. ComfyUI's own log:
   `scripts/up.sh logs comfy`.
3. Match the symptom in `docs/troubleshooting_en.md` (Japanese:
   `docs/troubleshooting.md`). It covers unusable configurations, disk space,
   CUDA out of memory, GPU hangs (Xid 119), degrading output, voice changes
   between sentences, length mismatches, WSL allocator failures, host RAM for
   720p, ComfyUI not reachable and hash mismatches.
4. Common fixes, in order of preference:
   * **GPU out of memory:** close other GPU programs (browsers, games, other
     CUDA processes) and retry. If it persists, suggest `calibrate` (a
     multi-run GPU search; ask first, preview with `--dry-run`). For the face
     refinement stage, `status` suggests the specific setting to change in a
     custom profile.
   * **Host out of memory / killed:** check RAM and swap against the profile's
     requirements (`show <profile-id>`); do not change swap without asking.
   * **Not prepared / image missing:** repeat the preparation step.
   * **Resume instead of restarting:** completed stages are kept, so a failed
     post-stage can be resumed with `--stages <stage>,... --source-video
     outputs/<run-id>/<NN>-<stage>.mp4` (see `docs/pipeline_en.md`).
5. When you report back, state what you ran, the error code and the relevant
   log lines, and what you did not verify.

## Reference

| Topic | File |
|---|---|
| Overview and quick start | `README_en.md` / `README.md` |
| Linux and Windows manuals | `docs/manual/linux_en.md`, `docs/manual/windows-wsl2_en.md` |
| Which GPU runs what | `docs/hardware-matrix_en.md` |
| What the verification labels mean | `docs/evidence_en.md` |
| Stages, partial runs and resuming | `docs/pipeline_en.md` |
| Narration | `docs/tts_en.md` |
| Custom hardware profiles | `examples/profiles/README.md` |

Exit codes: `0` success, `1` error, `2` no usable profile or not ready
(read the blockers), `130` cancelled.
