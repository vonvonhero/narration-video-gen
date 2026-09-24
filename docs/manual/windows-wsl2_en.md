# Running on Windows + WSL2

[日本語](windows-wsl2.md) | **English**

Use `setup.cmd` to set up your machine and start video or speech creation.

## 1. Start setup

Download and extract the repository using **Code > Download ZIP** on GitHub, then double-click `setup.cmd`.

Choose how you want to use the application:

- **音声作成だけ (Speech only)**: Works on a CPU too.
- **動画生成も使う (Video generation)**: Requires an NVIDIA GPU. The preparation screen checks memory and disk requirements for each configuration.

Follow the wizard to prepare Ubuntu, WSL2, and Docker Desktop. It asks before installing software or changing settings.
If you need a Windows NVIDIA driver, install it from the official page shown by the wizard.

On Ubuntu's first-run screen, create a Linux username and password. Password characters are not displayed while you type.
When you see a prompt such as `user@computer:~$`, type `exit` and press Enter.

When Docker Desktop opens for the first time, review its terms and continue to the Dashboard. Return to the setup window and press Enter.
The wizard can enable WSL integration if needed.

Video setup gives WSL 20 GiB of memory and 32 GiB of swap (larger existing values are kept). 720p needs 48 GiB of swap. Applying WSL settings stops all WSL distributions and Docker containers, so save your work before continuing.

Before Ubuntu and WSL2 are installed, the wizard offers to create a desktop shortcut. If prompted to restart, open **Narration Video Gen** on the desktop to continue.
If you chose not to create the shortcut, open the same `setup.cmd`. The wizard offers the shortcut again when setup completes.

## 2. Create video or speech

For video use, setup opens the video menu:

1. Choose **キャラクターと音声を作る (Create a character and narration)** to open the Web UI. Set the character's image, voice, speaking style, and script. After reviewing the narration, choose **Use this narration as video input**.
2. Choose **動画の構成選択と準備 (Prepare video configuration)** to select the model and resolution, then prepare models and the runtime image.
3. Choose **動画を生成 (Generate video)** and select the input set created in the Web UI. Start with a short test.
4. Choose **生成状況を確認 (Check generation status)** to view progress and the completed video's location.

Generation continues in the background. After closing the window, reopen the shortcut to check progress or cancel.
See the [Linux generation steps](linux_en.md#3-choose-your-input-and-generate) for input folders and detailed usage.

The video menu's **Create a character and narration** option and the speech-only setup both open the narration page in your browser. If it does not open automatically, enter the displayed `http://127.0.0.1:7861` address in your Windows browser.

To switch purposes, open PowerShell in the folder containing `setup.cmd` and run one of these commands:

```powershell
.\setup.cmd -Purpose Tts
.\setup.cmd -Purpose Video
```

Your choice is saved for future launches. If you created the desktop shortcut, a copy of `setup.cmd` is also available under `%LOCALAPPDATA%\NarrationVideoGen`.

## Memory and file locations

| Configuration | Physical RAM | WSL RAM | Swap |
|---|---:|---:|---:|
| 480p | 32 GiB | 20 GiB | 32 GiB |
| 720p Wan 2.2 | 24 GiB | 16 GiB | 48 GiB |
| 720p Wan 2.1 | 32 GiB | 20 GiB | 48 GiB |

Increasing WSL memory or swap does not change the physical RAM requirement. The preparation screen shows the configurations you can use.

The runtime repository is stored at `~/narration-video-gen` inside WSL. Models and generated output are stored there too.
When video generation completes successfully, the final video is also copied to
`Videos\Narration Video Gen\<run-id>\video.mp4` on Windows. Intermediate files and
the original remain under `~/narration-video-gen/outputs`, so a Windows copy failure
does not lose the generated result.
To browse these files from Windows, open **Linux > your Ubuntu distribution > home > your username > narration-video-gen** in File Explorer.

## Reclaim disk space

Choose **Reclaim disk space (cleanup)** from the video menu, or open `cleanup.cmd`,
to remove downloaded models, Docker images built by Narration
Video Gen, compact WSL virtual disks, or perform all three. Run
`.\cleanup.cmd -Check` in PowerShell to preview the targets and sizes without
deleting anything.

Inputs, generated media, settings, characters, Docker volumes and images from
other projects are never removed. Compacting the WSL virtual disk stops Docker
Desktop and every WSL distribution, so it asks separately and needs
administrator rights. Compaction also removes the `memory=20GB` and
`swap=32GB` lines that setup added to `.wslconfig`; run video setup again to
restore them.

Before downloading models, allow about 55 GiB of free space for Wan 2.1 or 80 GiB for Wan 2.2, plus space for WSL swap.
Keep the runtime repository inside WSL; Windows-mounted paths such as `/mnt/c` slow model loading.

## Troubleshooting

Rerun `setup.cmd` and check the incomplete items it reports. To inspect setup without changing settings:

```powershell
.\setup.cmd -Check -Purpose Video
# Use -Purpose Tts for speech only
```

If Docker is unavailable, start Docker Desktop and enable your Ubuntu distribution under **Settings > Resources > WSL Integration**.
Run `wsl --list --verbose` in Windows PowerShell to check distribution names and WSL versions.

To choose a specific Ubuntu distribution, pass its listed name:

```powershell
.\setup.cmd -Distribution Ubuntu-24.04
```

During normal setup, the WSL CLI language is saved to match the Windows display language.
See [changing the display language](linux_en.md#change-the-display-language) to override it for one command.

See [troubleshooting](../troubleshooting_en.md) for further help.
