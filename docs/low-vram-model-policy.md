# 低VRAM向けモデル選択

**日本語** | [English](low-vram-model-policy_en.md)

VRAM 12 GiB以下のGPU向けの構成は、ベースモデルに小さいQ4_K_S版を使います。

| モデル | 12 GiB以下 | 16 GiB以上 |
|---|---|---|
| Wan 2.1 InfiniteTalk | `wan21-i2v-14b-480p-q4ks` | Q6 |
| Wan 2.2 S2V | `wan22-s2v-14b-q4ks` | FP8 |

- Q4_K_S版は`*-q4ks`レシピで指定します。実行時にモデルが自動で置き換わることはありません。
- Wan 2.1の720pも480p用のベースモデルを使い、720pで出力します。
- 音声アダプター、テキストエンコーダー、顔補正（VACE）などのモデルはQ4ではありません。

## 検証状況

| 構成 | 結果 |
|---|---|
| Windows、Wan 2.2、480p、8 GiB相当（物理RAM 24 GiB、WSL 16 GiB、swap 48 GiB） | フル尺を完走、目視合格。途中でWSLが停止し、再開して完走 |
| Linux、Wan 2.2、480p、12 GiB相当（RAM 16 GiB、swap 32 GiB） | フル尺を完走、目視合格。生成と後処理を分けて実行 |
| Wan 2.2、720p、8 GiB・12 GiB相当 | 短尺でメモリ不足 |
| Wan 2.1の12 GiB以下の構成 | 未検証 |

8 GiB・12 GiB相当の結果は、大きいGPUの一部を確保して再現したものです。
物理8 GiB・12 GiBのGPUでは確認していません。
