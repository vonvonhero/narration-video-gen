# Running on Linux

[日本語](linux.md) | **English**

After setup, continue in the menu to create speech, prepare and generate videos, and check progress. The individual commands below remain available.

## 1. Set up your machine

Video generation requires amd64 Ubuntu 24.04 or 26.04 LTS and an NVIDIA GPU with at least 8 GiB of VRAM.
RAM and swap requirements vary by configuration. Before downloading models, allow about 55 GiB of free space for Wan 2.1 or 80 GiB for Wan 2.2. Use `plan` to check which configurations your machine supports.

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/vonvonhero/narration-video-gen.git
cd narration-video-gen
scripts/setup-linux.sh
```

Follow the prompts to check the driver, Docker, and GPU access. Setup asks before installing software or changing settings.
If prompted to log in again or reboot, do that and run `scripts/setup-linux.sh` again.

If swap is insufficient, setup offers to bring the total to 32 GiB. You will need to configure it manually if disk space is insufficient or your filesystem does not support automatic setup.

For speech creation alone, an NVIDIA GPU is optional. Install Docker and Docker Compose, then open the speech page with `./bin/narration-video-gen tts`. `setup-linux.sh` is intended for video generation.

The menu offers 1 for speech creation, 2 for preparation, 3 for generation, 4 for status, 5 for cancellation, and 0 to exit. Each operation returns to the menu. Preparation and generation retain their own review prompts before starting. Open `scripts/setup-linux.sh` again for your next session.

## 2. Choose and prepare a video configuration

```bash
./bin/narration-video-gen plan
```

Choose the model and resolution. Review the required models, disk space, and estimated time, then continue to download models and prepare the runtime image. See [third-party terms](../../THIRD_PARTY_NOTICES.md) for model licences.

Run `plan` again to resume interrupted preparation. Use `plan --check` to check its status without making changes.

## 3. Choose your input and generate

```bash
./bin/narration-video-gen run
```

Choose an image and narration, then review the duration and inputs before starting. Use a short test for your first video to check the result.
Generation continues in the background, so you can close the terminal.

To use your own material, put one image and one mono WAV file in `inputs/<name>/`. The folder then appears in the input list alongside the bundled characters.
You can create narration with `./bin/narration-video-gen tts`.

If a local TTS engine is using the GPU, video generation stops that engine to free GPU memory. The speech page stays available, and the engine restarts when you next generate speech.

## 4. Check the result

```bash
./bin/narration-video-gen status
```

This shows progress and the completed video's location. Play the video and check lip movement, faces, hands, and audio synchronization.

To cancel generation:

```bash
./bin/narration-video-gen cancel
```

On failure, `status` shows the cause. Detailed logs are in `outputs/<run-id>/run.log`.
See [troubleshooting](../troubleshooting.md) for common fixes.

## Change the display language

The application uses its saved language preference, or the OS language when no preference is saved. To use English for one command:

```bash
NVG_UI_LANGUAGE=en ./bin/narration-video-gen plan
```

Use `ja` instead of `en` for Japanese. The same setting works with `scripts/setup-linux.sh`.

## Check or undo setup

Use `--check` to inspect the current setup without changing settings.

```bash
scripts/setup-linux.sh --check
```

To remove swap added by setup:

```bash
scripts/setup-linux.sh --remove-swap --yes
```

This removes only `/swapfile-narration-video-gen`. Enough free RAM is required to move any data currently stored there.

Individual actions are available through `--setup`, `--install-driver`, and `--test-gpu`. See `--help` for details.

<details>
<summary>Temporarily skip sudo password prompts</summary>

This lets an AI agent run the setup without stopping at sudo password prompts. Use it only on a personal development machine and only while needed. While it is enabled, anything running as your user has root access without a password.

Enable it yourself in a terminal (you enter your password once). A systemd timer removes the grant when the period ends or at the next boot, whichever comes first. The default period is 2 hours; `--for` accepts up to 12 hours. Running `enable` again while enabled starts a new period.

```bash
scripts/passwordless-sudo.sh status
scripts/passwordless-sudo.sh enable --for 2h
# Restore password prompts if you finish early
scripts/passwordless-sudo.sh disable
```

The reboot after installing the driver ends the grant; run `enable` again if the agent should continue afterwards. It cannot be enabled where systemd is not running. With the standard sudo of Ubuntu 24.04 the rule itself also carries the deadline (`NOTAFTER`), so it stops working at the deadline even if the timer does not run. A grant without expiry enabled by an earlier version shows up in `status`; `enable` replaces it with a timed one and `disable` removes it.

</details>

## Advanced configuration and diagnostics

Use `plan --advanced` to change the finishing options.
Normally, `plan` selects a profile that fits your hardware. Use `show` for details or `select` to choose directly.

```bash
./bin/narration-video-gen show
./bin/narration-video-gen select --model wan21 --resolution 480p
```

For custom settings, copy a template from `examples/profiles/` and pass `--profile-dir /path/to/my-profiles`.
Recipes hold generation settings; profiles hold GPU and memory settings.

If GPU memory errors persist, choose a configuration and run local calibration. This requires several test generations.

```bash
./bin/narration-video-gen calibrate
./bin/narration-video-gen calibrations list
```

Results are saved under `~/.config/narration-video-gen/calibrations/` and apply to the same GPU and configuration.
Use `calibrations disable <file>` to disable a result temporarily and `calibrations enable <file>` to restore it.

To record GPU and memory use, run `scripts/monitor.sh <run-id>` in a second terminal during generation and stop it with Ctrl+C when finished. Records go to `results/<run-id>/metrics.csv`.

Use `verify <video-file> --audio <used-audio-file>` for automated video checks.
For a short test, pass the trimmed `outputs/<run-id>/input-short-test.wav`.

To record a result for a shared profile, review the video, then use `report --run-id <run-id> --visual-review passed`.
See [evidence labels](../evidence_en.md) for verification details and [asset licenses](../../ASSET_LICENSES.md) for bundled material.
