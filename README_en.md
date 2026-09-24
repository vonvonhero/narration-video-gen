# narration-video-gen

[日本語](README.md) | **English**

Create a talking video from a portrait and narration audio using Wan 2.1 + InfiniteTalk or Wan 2.2-S2V.
A browser interface for creating narration from a script is included.

Video generation requires an NVIDIA GPU and Docker. The preparation screen shows which models and resolutions your machine can use.
Audio creation also works on a CPU.

## Get started

### Windows

1. Download and extract the repository using **Code > Download ZIP** on GitHub.
2. Double-click `setup.cmd` and choose audio creation only or video generation too.
3. Follow the setup prompts. If you create the shortcut offered before Ubuntu installation, continue after a reboot from **Narration Video Gen** on the desktop.

Keep using this launcher after setup. Its video menu offers preparation, generation and progress checks; audio creation opens in a browser.
If you choose not to create the shortcut, open the same `setup.cmd` after rebooting. See the [Windows guide](docs/manual/windows-wsl2_en.md).

### Linux

The setup wizard supports Ubuntu 24.04 and 26.04 LTS on amd64.

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/vonvonhero/narration-video-gen.git
cd narration-video-gen
scripts/setup-linux.sh
```

After setup, a menu lets you create speech, prepare and generate videos, and check progress. If a reboot or fresh login is required, run the same `scripts/setup-linux.sh` afterward. Keep using this entry point once setup is complete.

If you prefer individual commands, run these in order:

```bash
./bin/narration-video-gen plan   # choose a model and resolution, then prepare
./bin/narration-video-gen run    # choose inputs and generate a video
```

`plan` shows the download size and estimated generation time. Confirm to download models and prepare the runtime image.
Run the same command to continue after an interruption. See the [Linux guide](docs/manual/linux_en.md).

## Create a video

`run` asks for inputs and a length, then confirms before starting.
The bundled Aoi and Sakura portraits and narration let you try it without preparing your own inputs.
Start with the roughly 5.6-second **Short test** to check the result.

Generation continues in the background.

```bash
./bin/narration-video-gen status  # show progress and the output path
./bin/narration-video-gen cancel  # stop the running generation
```

Completed videos are saved under `outputs/<run-id>/`. Open the video shown by `status` to check the result.
If generation fails, `status` also shows the cause and suggested next steps.

For your own inputs, put one image and one WAV file in the same folder:

```text
inputs/
  my-video/
    image.png
    audio.wav
```

The next `run` includes it in the input list. See [preparing inputs](inputs/README.md).
Your configuration is saved until reboot. After rebooting, use `plan` to choose it again.

## Create audio from a script

```bash
./bin/narration-video-gen tts
```

Choose a character and enter your script in the browser.
On first use, prepare the models using the button on the page.
Listen to the result and adjust pauses, then choose **Use as video input** to create an input set for `run`.
See the [narration guide](docs/tts_en.md) for details.

## Settings and troubleshooting

Use the defaults in `plan` to get started. To change face refinement or lip sync, use `plan --advanced`.
Use `plan --frame-interpolation off` to skip the 60 fps finish, or `plan --frame-interpolation on` to restore it.

| Task | Command or guide |
|---|---|
| Check preparation without making changes | `./bin/narration-video-gen plan --check` |
| Check your GPU and memory | `./bin/narration-video-gen detect` |
| Read command help | `./bin/narration-video-gen --help`, `run --help`, etc. |
| Investigate errors or slow generation | [Troubleshooting](docs/troubleshooting_en.md) |
| Adjust persistent memory problems | [Advanced settings in the Linux guide](docs/manual/linux_en.md#advanced-configuration-and-diagnostics) |
| Check supported configurations and results | [Hardware matrix](docs/hardware-matrix_en.md), [evidence](docs/evidence_en.md) |
| Understand generation and finishing stages | [Pipeline](docs/pipeline_en.md) |

Automation can supply `--model`, `--resolution`, `--image`, `--audio` and `--length short|full`.
Use `--json` for JSON results. `plan --json` only checks preparation status.
See [custom profiles](examples/profiles/README.md) for your own hardware settings.

## Licence

The code is licensed under [Apache-2.0](LICENSE).
See [third-party terms](THIRD_PARTY_NOTICES.md) for models and [asset licences](ASSET_LICENSES.md) for bundled images and audio.
