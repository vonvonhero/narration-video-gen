**日本語** | [English](troubleshooting_en.md)

# トラブルシューティング

失敗した生成は、まず`./bin/narration-video-gen status`で原因と対処を確認してください。
詳しいログは`outputs/<実行ID>/run.log`にあります。

## WindowsでDocker Desktopが起動しない（socketのremove / rename）

### 予防：エージェントもsetup.cmdの順序を守る

手動での`setup.cmd`では発生せず、エージェント操作時に発生したという利用者の観測が
あります。ただし、最初の障害を起こした操作は確定していません。正常に使えている
ウィザードの自動WSL連携は維持し、エージェントが別の操作列に置き換えないようにします。

- セットアップは1セッションだけで進めます。入力待ちもロックの対象です。
  エージェントのツールが実行セッションIDを返したり時間切れで制御を返したりしても、
  元の処理は続いている場合があります。同じ端末を再開・監視し、別のセットアップや
  Docker終了・設定変更・WSL停止を並行して実行しません。
- Dashboardは初回操作の完了確認です。ウィザードは同じバックエンドのPID・起動時刻と、
  Windows側の`desktop-linux`コンテキストのServer応答・Linux Engine情報を
  60秒以上・3回以上確認してからWSL連携へ進みます。初回確認の上限は300秒です。
  連携後は対象UbuntuのDocker / Composeも含めて確認します（上限180秒）。
  CLIには個別の時間制限もあり、全体の上限には実行中のプローブの時間が加わる場合があります。
- 継続確認中の応答消失やバックエンドの終了・交代は成功扱いにしません。
  最後の1回だけ応答しても進めません。確認時間は本プロジェクトの基準であり、
  Docker公式の保証値や長時間/GPU稼働の保証ではありません。
- 連携設定が必要ならウィザードが承認、通常終了、完全停止確認、設定のバックアップ・変更、
  対象WSLの終了、Docker起動を順に担当します。エージェントは各関数を抜き出して実行したり、
  同じ操作を別シェルで補ったりしません。対話端末を維持できない場合はユーザーに
  `setup.cmd`の実行を任せます。

### 起動失敗時：証拠を残してから、段階を分けて復旧する

`sailor-ingest.sock`、`dockerInference`、`docker-secrets-engine\engine.sock` の
`remove` / `rename` が `The file cannot be accessed by the system` で失敗する場合に
次を使います。単なるEngine未応答だけで、ソケットの退避へ進まないでください。

1. **読み取り専用診断。** 操作時刻、Dockerバージョン、プロセスのPID・起動時刻、
   `wsl -l -v`、`docker desktop status`、Windowsと対象UbuntuのDocker応答を記録します。
   各CLIには外側のタイムアウトも設けます。失敗は「コンテナなし」を意味しません。
   `%LOCALAPPDATA%\Docker\log\host`と`log\vm`の関連ログ、対象ソケットと`.stale`の
   有無・時刻・属性、既存退避先、直前の操作履歴をローカルに保存します。
   新しいログと過去のエラーを区別し、秘密情報をチャットへ貼りません。
   診断バンドルやログのアップロードは別途承認が必要です。
2. **通常終了を1回。** 他の作業への影響を伝えて承認を得てから、GUIのQuit、または
   `docker desktop stop --timeout 45`による通常終了を試します。終了要求自体が固まる
   場合があるため、CLIのオプションだけを時間制限として信用しません。
   コマンド終了だけで成功とせず、Docker Desktop、backend、終了要求プロセスが消え、
   `docker-desktop` WSLと`com.docker.service`が停止したことを再確認します。
   停止できた場合は通常起動を1回試し、後述の継続確認を行えます。
3. **終了不能なら自動操作を停止。** GUIがなくてもプロセスが残る場合があります。
   `--force`、`Stop-Process`、`taskkill /F`、`wsl --shutdown`へ自動で切り替えません。
   状況を報告し、作業を保存したうえでのWindows再起動をユーザーに提案します。
   再起動後はDockerの自動起動と新しいログを先に確認します。健康なら追加起動や
   退避はせず、その状態を継続確認します。再起動だけで直る保証はありません。
4. **同じソケットエラーが続く場合の限定的な回避策。**
   [公開イシュー #554](https://github.com/docker/desktop-feedback/issues/554)の投稿者は、
   完全停止後、次の2つの親ディレクトリを退避してから起動する方法を報告しています。
   Docker公式の復旧手順ではありません。明示的な承認を得て、完全停止を確認できた場合だけ
   1回試す候補です。停止できなければ、ここで止めます。

   | 対象 | 操作 |
   |---|---|
   | `%LOCALAPPDATA%\Docker\run` | 存在すれば固有名へ退避 |
   | `%LOCALAPPDATA%\docker-secrets-engine` | 存在すれば固有名へ退避。なければ何もしない |

   **両方の元の場所を確認・退避し終えるまでDockerを起動しません。**
   片方のエラーしか出ていなくても、両方を確認します。最初の失敗で次のエラーが
   隠れている可能性があるためです。既存の退避先は保持し、上書き・削除しません。
   移動失敗やDockerの予期しない起動を検出したら中止します。
   `Docker`全体、`wsl`、VHDX、設定ファイル、モデル・イメージ・コンテナ・ボリュームは
   対象外です。全ソケットを検索して無差別に移動する手順ではありません。
5. **起動は1回、判定は継続応答。** 退避完了後に一度だけ起動し、同じバックエンド、
   WindowsのLinux Engine、対象UbuntuのDocker / Composeが60秒以上・3回以上応答することと、
   起動時刻以降のログに同じソケットエラー、backend crash、PauseError等がないことを確認します。
   別のエラーでも、再発したら証拠を保存して停止します。追加退避・再起動を繰り返しません。
   成功後も生成は別の作業です。新しいセットアップは、待機中の古いセッションが終了したことを
   確認してから再開します。

更新は対象症状の修正根拠を確認して提案します。新しいバージョンなら直るとは断定しません。
再インストールやリセットが必要なら、先に[公式バックアップ手順](https://docs.docker.com/desktop/settings-and-maintenance/backup-and-restore/)
に沿ってデータ保全を計画します。Factory Reset、Clean up data、退避先の削除は自動復旧の対象外です。
公式情報：[停止CLI](https://docs.docker.com/reference/cli/docker/desktop/stop/)、
[診断・ログ](https://docs.docker.com/desktop/troubleshoot-and-support/troubleshoot/)、
[WSL連携](https://docs.docker.com/desktop/features/wsl/)。

### 確認済みの範囲（2026-09-27の利用者レポート）

Docker Desktop 4.91.0 (239619)で、初回Engine応答後にソケットエラーが発生しました。
その後、`run`退避→起動→`docker-secrets-engine`退避→起動の順で再発しました。
通常終了はCLIの45秒指定を超えて待機し、最終的に失敗しました。Windows再起動後は
自動起動し、17:21〜17:23 JSTの3回の確認でEngineとUbuntuのDocker / Composeが応答しました。
先行する退避と再起動の効果は切り分けられていません。一括退避・長時間稼働・GPU生成は未検証です。
利用者からは、別の過去の事例で再起動では直らず、一括退避で復旧したとの報告もありますが、
そのバージョンは未確認です。いずれも万能な復旧方法の証明ではありません。

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
