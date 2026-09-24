# 生成入力 / Generation inputs

1回の生成に使う素材を、名前を付けた1つのフォルダへまとめてください。

```text
inputs/
  sakura-blog/
    image.png
    audio.wav
    script.txt  # 任意。Web UI/CLIで音声を作る場合の台本
```

`./bin/narration-video-gen run`は、`inputs/`のカスタム入力と`assets/characters/`の同梱
キャラクターを同じ一覧に表示します。最新の完成済みカスタム入力が先頭かつ既定になり、各候補には
実測した音声尺を表示します。各セットには画像1件とモノラルWAV音声1件が必要です。

生成開始前に、選択した画像と音声のパスを表示します。メディアファイルはGit管理外です。
エージェントや自動化では`run --image <path> --audio <path> --length short|full`で
直接指定できます。

WAVがまだない場合は、`./bin/narration-video-gen tts`でローカルの音声作成ページを開けます。
生成後に「この音声を動画の入力にする」を押すと、画像・完成WAV・台本を含む入力セットを作ります。
詳しくは[`docs/tts.md`](../docs/tts.md)を参照してください。

---

Keep all media for one generation in one named folder:

```text
inputs/
  sakura-blog/
    image.png
    audio.wav
    script.txt  # optional source text for Web UI/CLI narration generation
```

`./bin/narration-video-gen run` presents custom sets under `inputs/` and bundled
characters under `assets/characters/` in the same list. The newest complete
custom set is first and is the default, and every candidate shows its measured
audio duration. Each set currently needs exactly one image and one mono WAV file.

These media files are ignored by Git. The selected image and audio paths are
shown before generation starts.

Agents and automation can bypass discovery and pass explicit paths with
`run --image <path> --audio <path> --length short|full`.

If the WAV does not exist yet, open the local narration page with
`./bin/narration-video-gen tts`. After generation, **Use as video input** creates
an input set containing the portrait, completed WAV and script. See
[`docs/tts_en.md`](../docs/tts_en.md).
