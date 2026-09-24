# Create narration

[日本語](tts.md) | **English**

Create narration for a video from a script with Irodori-TTS v4.1 Small.

## Use the browser

```bash
./bin/narration-video-gen tts
```

This opens the narration page. The Web UI labels are in Japanese.

1. Choose Aoi, Sakura, or a character you created, and enter your script.
2. Press **音声を作る** (Create audio). First use downloads the models, about 3.7 GB. The page shows the image build step, the amount downloaded, the speed and an estimated time left, and returns to that display if you reload it.
3. Listen to the finished audio and each part, then tick the confirmation.
4. Press **この音声を動画の入力にする** (Use as video input). Run `./bin/narration-video-gen run` and choose the new input set.

Line breaks separate parts; long lines also split at punctuation. A blank line adds a longer pause. The preview below the script shows the actual parts. Generation on CPU can take several minutes.

### Adjust the audio

- **Pauses:** Change **次の文まで** (Until the next sentence), then press **間の変更を反映する** (Apply pause changes).
- **Delivery or length:** Open **このパートだけ作り直す** (Regenerate this part). A different seed changes delivery; length uses 1.0 as the default. **最初のseedに戻す** uses the initial seed again.
- **Pronunciation:** Edit the script and press **音声を作る**. Writing difficult kanji or names in hiragana can help.
- **Speaking style or ending pause:** Open **話し方などの詳細設定** (Advanced settings). The default ending pause is 1.5 seconds.

Transcript checks appear beside each part. Listen to parts marked **要確認** for skipped words or extra speech; spelling differences may also trigger this label.

**前に作ったもの** (Previous narrations) lets you reopen a job. Closing the tab during generation does not stop it while the narration service remains running.

## Create a character

Use **＋ キャラクターを作る** to set a name, voice, speaking style, and portrait.

- **声を文章で作る** (Describe the voice): Enter a description such as “a calm female voice, clear speech, natural pace.” **別の声にする** changes the voice variation for another attempt.
- **音声を指定する** (Choose audio): Select a recording in mp3, m4a, wav, or another supported format. Use your own voice or one you have the speaker's permission to use.
- **Inherit an existing character:** Keep its voice, portrait, and seed while changing the speaking style. Omitting the portrait keeps the original picture.

Reference audio must be at least 2 seconds; 10 seconds or more is a useful starting point. Only the first 120 seconds are used. If the page flags a dull voice, try another variation for a described voice, or another recording for an imported voice.

The portrait becomes the video's first frame. It is **centre-cropped to 16:9**; check the preview for a cut-off face.

After the audition, press **このキャラクターを使う** (Use this character) to write a script. Use **別の声にする** to try another voice, or **やめる（削除）** to discard the character.

### Change a character

Selecting a character shows actions to redesign the voice, replace the picture, or delete it. Voice redesign is available only for voices made from a description.

A character's voice cannot be redesigned and the character cannot be deleted while narrations using it remain. Create another character to use a different voice. Bundled Aoi and Sakura cannot be changed or deleted.

User characters are saved in `voices/`; generated audio is saved in `outputs/tts/<job-id>/`.

## Use the CLI

```bash
# Preview script segmentation
./bin/narration-video-gen tts plan --script inputs/my-video/script.txt

# Prepare the models on first use and start the speech engine
./bin/narration-video-gen tts prepare
./bin/narration-video-gen tts backend start

# Generate audio
./bin/narration-video-gen tts generate \
  --script inputs/my-video/script.txt \
  --character aoi \
  --output inputs/my-video/audio.wav
```

Listen using the URL printed after generation. Transcript checking is enabled by default; add `--skip-asr` to skip that check.

After listening, use the printed job ID to create a video input set:

```bash
./bin/narration-video-gen tts adopt <job-id> --name my-video --confirm-listened
```

## Start, stop, and access from another device

```bash
./bin/narration-video-gen tts web status
./bin/narration-video-gen tts web stop
```

To use another device on the same LAN:

```bash
./bin/narration-video-gen tts web reset-password
./bin/narration-video-gen tts web start --lan --no-open
```

LAN access requires a password of at least 10 characters. The username is fixed
to `tts`; the login page only asks for the password. Run `reset-password` again
from a local terminal or SSH session if you forget it. The password is never
stored in plain text. An existing localhost-only service restarts with the new
address setting. The connection uses HTTP, so only enable LAN access on a
trusted local network.

Starting video generation stops the speech engine to release GPU memory. The Web UI stays open and restarts the engine for the next narration.

## Models and audio format

First use downloads the TTS, codec, Whisper Base, and SilentCipher models under the MIT licence. Bundled character references use synthetic speech.

Audio is saved as 48 kHz mono 16-bit PCM WAV. A video input set contains the audio, character portrait, and script.
