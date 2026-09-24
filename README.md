# narration-video-gen

**日本語** | [English](README_en.md)

画像とナレーション音声から、話している人物の動画を作成します。
Wan 2.1 + InfiniteTalkとWan 2.2-S2Vに対応し、台本から音声を作る画面も付属しています。

動画生成にはNVIDIA GPUとDockerが必要です。利用できるモデル・解像度は、準備画面で確認できます。
音声作成はCPUでも利用できます。

## はじめる

### Windows

1. GitHubの **Code > Download ZIP** からダウンロードして展開します。
2. `setup.cmd`をダブルクリックし、「音声作成だけ」または「動画生成も使う」を選びます。
3. 画面の案内に沿ってセットアップします。Ubuntu導入前にショートカットを作成すると、再起動後はデスクトップの **Narration Video Gen** から続けられます。

セットアップ後も同じ入口を使えます。動画はメニューから構成の準備・生成・進捗確認を選び、音声はブラウザで作成します。
ショートカットを作成しなかった場合は、再起動後に同じ`setup.cmd`を開いてください。詳しくは[Windows手順書](docs/manual/windows-wsl2.md)を参照してください。

### Linux

セットアップウィザードはUbuntu 24.04／26.04 LTSのamd64版に対応しています。

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/vonvonhero/narration-video-gen.git
cd narration-video-gen
scripts/setup-linux.sh
```

セットアップ完了後は、そのままメニューから音声作成・動画の準備・生成・進捗確認へ進めます。再起動や再ログインが必要な場合は、終了後に同じ`scripts/setup-linux.sh`を実行してください。セットアップ済みの場合も同じ入口を使えます。

コマンドで操作したい場合は、次の2つを順に実行します。

```bash
./bin/narration-video-gen plan   # モデルと解像度を選び、必要なものを準備
./bin/narration-video-gen run    # 素材を選び、動画を生成
```

`plan`にはモデルの取得量と推定生成時間が表示されます。内容を確認して進むと、モデル取得と実行イメージの準備が始まります。
途中で止めた場合も同じコマンドから再開できます。詳しくは[Linux手順書](docs/manual/linux.md)を参照してください。

## 動画を作る

`run`で入力素材と長さを選び、確認して生成を開始します。
同梱の葵・さくらの画像と音声を使えるので、最初から素材を用意する必要はありません。
初回は約5.6秒の「短尺(テスト)」で仕上がりを確認してください。

生成はバックグラウンドで続きます。

```bash
./bin/narration-video-gen status  # 進捗・出力先を確認
./bin/narration-video-gen cancel  # 実行中の生成を中止
```

完成した動画は`outputs/<実行ID>/`に保存されます。`status`に表示された動画を開いて確認してください。
失敗した場合も`status`で原因と対処を確認できます。

自分の素材を使う場合は、画像1件とWAV音声1件を同じフォルダに入れます。

```text
inputs/
  my-video/
    image.png
    audio.wav
```

次の`run`で入力候補に表示されます。詳細は[入力素材の用意](inputs/README.md)を参照してください。
選択した構成は再起動まで保存されます。再起動後は`plan`で構成を選んでください。

## 台本から音声を作る

```bash
./bin/narration-video-gen tts
```

ブラウザでキャラクターを選び、台本を入力して音声を作成します。
初回は画面の「モデルをダウンロードする」から必要なモデルを取得します。
試聴して間合いを調整した後、「この音声を動画の入力にする」を押すと、`run`で選べる入力セットを作成できます。
操作の詳細は[音声作成の手順](docs/tts.md)を参照してください。

## 設定を変える・問題を調べる

通常は`plan`の標準設定で利用できます。顔補正やリップシンクを変えたい場合は`plan --advanced`を使います。
60 fps化を省略する場合は`plan --frame-interpolation off`、戻す場合は`plan --frame-interpolation on`を指定します。

| 目的 | コマンド・資料 |
|---|---|
| 準備状況だけ確認する | `./bin/narration-video-gen plan --check` |
| PCのGPU・メモリを確認する | `./bin/narration-video-gen detect` |
| コマンドの使い方を見る | `./bin/narration-video-gen --help`、`run --help`など |
| エラーや生成の遅さを調べる | [トラブルシューティング](docs/troubleshooting.md) |
| メモリ不足が続く場合の調整 | [Linux手順書の詳細設定](docs/manual/linux.md#詳細設定と診断) |
| 対応構成と確認実績を見る | [ハードウェア一覧](docs/hardware-matrix.md)、[検証結果](docs/evidence.md) |
| 生成・仕上げ処理を調べる | [パイプライン](docs/pipeline.md) |

自動化では`--model`、`--resolution`、`--image`、`--audio`、`--length short|full`で条件を指定できます。
`--json`で結果をJSON形式にできます。`plan --json`は準備状況の確認だけを行います。
独自のハードウェア設定は[カスタムプロファイル](examples/profiles/README.md)を参照してください。

## ライセンス

コードは[Apache-2.0](LICENSE)です。
モデルは[サードパーティの利用条件](THIRD_PARTY_NOTICES.md)、同梱の画像・音声は[素材ライセンス](ASSET_LICENSES.md)を参照してください。
