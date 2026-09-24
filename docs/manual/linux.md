# Linuxで実行する

**日本語** | [English](linux_en.md)

セットアップ後は同じ画面のメニューから、音声作成・動画の準備・生成・進捗確認へ進めます。以下の個別コマンドも引き続き使えます。

## 1. セットアップする

動画生成には、amd64版Ubuntu 24.04または26.04 LTSと、VRAM 8 GiB以上のNVIDIA GPUが必要です。
RAM・swapの要件は構成ごとに異なります。モデル取得前の空き容量はWan 2.1で55 GiB、Wan 2.2で80 GiBが目安です。利用できる構成は`plan`で確認できます。

```bash
sudo apt update
sudo apt install -y git python3
git clone https://github.com/vonvonhero/narration-video-gen.git
cd narration-video-gen
scripts/setup-linux.sh
```

画面の案内に沿って、ドライバー、Docker、GPUへのアクセスを確認します。必要なインストールや設定変更は、その都度確認してから進みます。
再ログイン・再起動を案内されたら、済ませた後に同じ`scripts/setup-linux.sh`を実行してください。

swapが不足している場合は合計32 GiBを提案します。空き容量が足りない場合や、自動設定に対応しないファイルシステムでは手動設定が必要です。

音声作成だけならNVIDIA GPUは不要です。DockerとDocker Composeを用意し、`./bin/narration-video-gen tts`で音声作成ページを開いてください。`setup-linux.sh`は動画生成用です。

セットアップ完了後のメニューは、1が音声作成、2が構成選択と準備、3が動画生成、4が進捗確認、5が生成中止、0が終了です。各操作が終わるとメニューに戻ります。準備や生成の内容は、それぞれの画面で確認してから開始します。次回も`scripts/setup-linux.sh`から開けます。

## 2. 動画の構成を選んで準備する

```bash
./bin/narration-video-gen plan
```

モデルと解像度を画面で選びます。必要なモデル、空き容量、所要時間を確認して進むと、モデル取得と実行イメージの準備が始まります。モデルの利用条件は[サードパーティの利用条件](../../THIRD_PARTY_NOTICES.md)を参照してください。

準備を途中で止めた場合も、同じ`plan`から再開できます。状態だけを見る場合は`plan --check`を使います。

## 3. 素材を選んで生成する

```bash
./bin/narration-video-gen run
```

画像とナレーションを選び、長さと内容を確認して生成を開始します。初回は短尺テストで仕上がりを確認してください。
生成はバックグラウンドで続くため、ターミナルを閉じても構いません。

自分の素材を使う場合は、`inputs/<名前>/`に画像1件とモノラルWAV音声1件を置くと、素材一覧に表示されます。同梱キャラクターも一覧から選べます。
音声は`./bin/narration-video-gen tts`から作成できます。

ローカルGPUのTTSエンジンが起動中なら、動画生成時に停止してGPUメモリを空けます。音声作成ページはそのまま利用でき、次の音声生成でエンジンが再起動します。

## 4. 結果を確認する

```bash
./bin/narration-video-gen status
```

進捗と完成動画の保存先を表示します。動画を再生し、口の動き、顔や手の崩れ、音声とのずれを確認してください。

生成を中止する場合:

```bash
./bin/narration-video-gen cancel
```

失敗した場合は`status`に原因が表示されます。詳しいログは`outputs/<実行ID>/run.log`にあります。
対処方法は[トラブルシューティング](../troubleshooting.md)を参照してください。

## 表示言語を変える

アプリに保存された言語設定を優先し、未設定ならOSの言語を使います。一度だけ日本語で起動する場合:

```bash
NVG_UI_LANGUAGE=ja ./bin/narration-video-gen plan
```

英語にする場合は`ja`を`en`に変えてください。同じ指定を`scripts/setup-linux.sh`にも使えます。

## セットアップの確認と取り消し

状態確認だけなら、設定変更をしない`--check`を使います。

```bash
scripts/setup-linux.sh --check
```

セットアップが追加したswapを削除する場合:

```bash
scripts/setup-linux.sh --remove-swap --yes
```

対象は`/swapfile-narration-video-gen`だけです。使用中のデータをRAMへ戻す空きが必要です。

個別の操作には`--setup`、`--install-driver`、`--test-gpu`を使えます。操作内容は`--help`で確認してください。

<details>
<summary>sudoのパスワード入力を一時的に省略する</summary>

個人用の開発マシンで、必要な間だけ利用してください。有効にするとパスワードなしでroot権限を使えるようになり、自動では失効しません。

```bash
scripts/passwordless-sudo.sh status
scripts/passwordless-sudo.sh enable
# 必要な作業が終わったら戻す
scripts/passwordless-sudo.sh disable
```

</details>

## 詳細設定と診断

仕上げ処理を変えたい場合は`plan --advanced`を使います。
通常は`plan`がハードウェアに合うプロファイルを選びます。選択内容の詳細は`show`、個別指定は`select`で確認できます。

```bash
./bin/narration-video-gen show
./bin/narration-video-gen select --model wan21 --resolution 480p
```

独自設定を使う場合は`examples/profiles/`のひな形をコピーし、`--profile-dir /path/to/my-profiles`で指定します。
生成設定はレシピ、GPU・メモリの設定はプロファイルで管理します。

GPUのメモリ不足が続く場合は、構成を選んだ後にローカル調整を実行できます。複数回のテスト生成が必要です。

```bash
./bin/narration-video-gen calibrate
./bin/narration-video-gen calibrations list
```

調整結果は`~/.config/narration-video-gen/calibrations/`へ保存され、同じGPUと構成で使われます。
`calibrations disable <ファイル>`で一時的に無効化し、`calibrations enable <ファイル>`で戻せます。

GPUやメモリの使用量を記録する場合は、生成中に別のターミナルで`scripts/monitor.sh <実行ID>`を起動し、終了後にCtrl+Cで止めてください。記録先は`results/<実行ID>/metrics.csv`です。

動画の自動検査には`verify <動画ファイル> --audio <使用した音声ファイル>`を使います。
短尺テストでは、切り出した`outputs/<実行ID>/input-short-test.wav`を指定してください。

プロファイルを共有するために実行結果を記録する場合は、動画を確認した後に`report --run-id <実行ID> --visual-review passed`を使います。
検証ラベルの意味は[検証結果の読み方](../evidence.md)、同梱素材の利用条件は[素材ライセンス](../../ASSET_LICENSES.md)を参照してください。
