**日本語** | [English](pipeline_en.md)

# パイプライン

1回の実行は、いくつかの工程（ステージ）で構成されます。最初の工程で画像と音声から動画を生成し、
以降の工程は動画ファイルを受け取って新しい動画ファイルを作ります。

```
infinitetalk ─▶ [musetalk] ─┐
                            ├─▶ face-detailer ─▶ rife ─▶ retime
s2v ────────────────────────┘
```

| 工程 | 入力 | 出力 | 内容 |
|---|---|---|---|
| `infinitetalk` | 画像 + 音声 | 動画 | Wan 2.1 + InfiniteTalkで生成 |
| `s2v` | 画像 + 音声 | 動画 | Wan 2.2-S2Vで生成 |
| `musetalk` | 動画 + 音声 | 動画 | MuseTalk 1.5で口の動きを作り直す（実験的） |
| `face-detailer` | 動画 | 動画 | 顔を検出・追跡し、VACEで補正 |
| `rife` | 動画 | 動画 | RIFEでフレーム補間（16 → 64 fps） |
| `retime` | 動画 | 動画 | 60 fpsへ変換し、元の音声を付け直す |

各工程の出力は`outputs/<実行ID>/<番号>-<工程>.mp4`に保存されます。

## プロンプト

人物、服装、背景、照明は参照画像のものを使います。標準のプロンプトは動きと画質だけを
指定しているため、どの画像にも同じレシピを使えます。特定の演出が必要な場合は、
カスタムレシピの`prompt`を指定してください。

## 一部の工程だけを実行する

`--stages`で実行する工程を指定できます。最初の工程が後処理の場合は、`--source-video`で
入力動画を指定します。

```bash
# パイプライン全体
./bin/narration-video-gen run --profile linux-wan22-480p-vram16 \
  --image p.png --audio n.wav --length short

# 既存の動画にフレーム補間だけを実行
./bin/narration-video-gen run --profile linux-wan21-480p-vram16 --stages rife,retime \
  --source-video outputs/existing.mp4

# 顔補正の前に顔の追跡結果を確認
./bin/narration-video-gen run --profile linux-wan22-480p-vram16 --stages face-detailer \
  --source-video outputs/existing.mp4 --mask-only
```

工程は常にパイプラインの順番で実行されます。

## 失敗した工程から再開する

工程が失敗すると、完了した工程の出力が表示されます。失敗した工程から再開してください。

```bash
./bin/narration-video-gen run --profile <プロファイル> --stages face-detailer,rife,retime \
  --source-video outputs/<実行ID>/01-s2v.mp4
```

## 顔補正

最初のフレームで最も大きい顔を検出し、動画全体で追跡します。顔が見つからない場合は停止します。
複数人が映る動画や特殊な構図では、カスタムレシピの`postprocess.face_detailer.points`で
座標を指定します。追跡結果は`--mask-only`で確認できます。

顔補正は`plan --advanced`または`plan --face-detailer on|off`で切り替えられます。
OFFにすると、顔補正用のモデルはダウンロードしません。

[カスタムプロファイル](../examples/profiles/README.md)では次の設定を指定できます。

| 設定 | 内容 | 既定値 |
|---|---|---|
| `face_detailer_enabled` | 顔補正のON/OFF | `true` |
| `face_detailer_size` | 顔の周囲を処理する正方形のサイズ。16の倍数 | `320` |
| `face_detailer_blocks_to_swap` | この工程のblock swap（0〜40） | `blocks_to_swap`と同じ |

この工程でGPUメモリが不足する場合は、`face_detailer_blocks_to_swap`を増やすか、
`face_detailer_size`を小さくします。サイズを小さくするとメモリ使用量は減りますが、
顔の精細さも下がります。

## 60 fps化

60 fps化は標準でONです。元のフレームレート（16 fps）のままにする場合:

```bash
./bin/narration-video-gen plan --frame-interpolation off
```

この設定は保存され、以降の`run`に使われます。元に戻すには`plan --frame-interpolation on`を
使います。

## Wan 2.1とMuseTalk

Wan 2.1の標準パイプラインは`infinitetalk → face-detailer → rife → retime`です。
`plan --lip-sync-enhancement musetalk`を指定すると、
`infinitetalk → musetalk → face-detailer → rife → retime`になります。
MuseTalkは実験的な機能で、480pの短尺でのみ確認しています。
