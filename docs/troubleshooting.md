**日本語** | [English](troubleshooting_en.md)

# トラブルシューティング

失敗した生成は、まず`./bin/narration-video-gen status`で原因と対処を確認してください。
詳しいログは`outputs/<実行ID>/run.log`にあります。

## 選んだ構成を実行できない

`plan`や`select`は、足りない要件と代わりに使える構成を表示します。除外された構成も
確認する場合は`./bin/narration-video-gen select --explain`を使います。

よくある原因はswap不足です。各構成は16 GiB以上、Linuxの720pは32 GiBのswapを必要とします。
`scripts/setup-linux.sh`は、既存のswapを残したまま合計32 GiBまでの追加を提案します。

**Physical Windows RAM could not be measured**と表示された場合は、WSLから
`powershell.exe`を実行できていません。この値が必要な構成は選択できません。

## ディスク容量が足りない

モデルの保存には、Wan 2.1で55 GiB、Wan 2.2で80 GiBの空きが必要です。
空き容量は`./bin/narration-video-gen detect`で確認できます。

## CUDA out of memory

**生成中（sampler）**: 他のプログラムがGPUメモリを使っている可能性があります。16 GiB向けの
構成は余裕が数百MiBしかないため、ブラウザや別のCUDAプロセスを閉じてから再実行してください。
それでも失敗する場合は`calibrate`でこのマシンに合う設定を探せます。

**顔補正の段階**: `status`に表示される`face_detailer_blocks_to_swap`の増加、または
`face_detailer_size`の縮小を[カスタムプロファイル](../examples/profiles/README.md)で指定します。

**24 GiBで720pのVAE処理中**: 24 GiBでは720pのVAE処理が収まりません。
`linux-wan21-720p-vram16`など、block swapを使う構成を選んでください。

`blocks_to_swap`は最大40です。40でも収まらない場合は、解像度を下げるか、より大きなGPUが必要です。

## GPUがハングする（Xid 119、GSP RPC timeout）

VRAMに余裕があるGPUで大きな`blocks_to_swap`を使うと発生することがあります。
GPUのVRAM容量に合った構成を使ってください。24 GiBでは`blocks_to_swap=0`が最速です。
発生したかどうかは`journalctl -k | grep -i xid`で確認できます。

## 動画が途中から劣化する

格子状のノイズが後半ほど強くなる場合は、tiled VAEが原因です。tiled VAEを有効にした
カスタムプロファイルを使っている場合は無効にしてください。

## 動画が途中から灰色になる

Windowsの720p Wan 2.2で一度発生しています。同じ構成で再実行してください。

## ナレーションの声が文ごとに変わる

seedが固定されていません。音声作成画面と`tts generate`はキャラクターごとにseedを固定します。
自分で合成する場合も、seedを固定し、参照音声を指定してください。

## 動画と音声の長さが数フレーム合わない

4フレームまでの差は正常です。`verify`はこの範囲を許容します。それ以上の差がある場合は、
`--audio`に生成で実際に使った音声ファイルを指定しているか確認してください。
短尺テストでは、切り出した`outputs/<実行ID>/input-short-test.wav`を指定します。

## `verify`は通るが動画がおかしい

`verify`は解像度、フレーム数、音声の有無、同期だけを確認します。口の動きや顔の崩れは
確認できないため、動画を再生して確認してください。

## Windows: expandable segmentsでallocator failureが発生する

古いWindows NVIDIAドライバーで発生します（[PyTorch #192330](https://github.com/pytorch/pytorch/issues/192330)）。
WSL2では回避策が標準で有効です。ドライバー616.92以降で回避策を無効にする場合は、
WSL内で次を実行します。

```bash
export NVG_WSL2_ALLOCATOR_WORKAROUND=0
scripts/up.sh up -d comfy
```

## Windows: 720pに必要なメモリ

| モデル | 物理RAM | WSL RAM | swap |
|---|---:|---:|---:|
| Wan 2.2 720p | 24 GiB | 16 GiB | 48 GiB |
| Wan 2.1 720p | 32 GiB | 20 GiB | 48 GiB |

WSLのメモリやswapを増やしても、物理RAMの要件は変わりません。

## ComfyUIに接続できない

```bash
scripts/up.sh ps
scripts/up.sh logs comfy
```

ComfyUIには認証がないため、ポート8188は127.0.0.1だけで待ち受けます。別のPCから使う場合は
設定を変えずにSSHトンネルを使ってください。

## モデルのハッシュが一致しない

```bash
./bin/narration-video-gen hashes
```

`size-mismatch`はダウンロードが途中で切れています。ファイルを削除して再取得してください。
`hash-mismatch`は別のファイルです。`manifests/models.lock.yaml`のURLから取得し直してください。
