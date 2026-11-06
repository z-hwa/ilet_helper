#!/usr/bin/env bash
# 下載本地語音模型到 models/（約 600MB，已存在的檔案會略過；中斷後重跑會續傳）。
#
#   bash scripts/download_models.sh            全部（英文 Kokoro ＋ 德文 4 個 Piper 聲音）
#   bash scripts/download_models.sh en         只下載英文聽力/口說用的 Kokoro
#   bash scripts/download_models.sh de         德文 4 個聲音
#   bash scripts/download_models.sh de-min     德文只要預設聲音 Thorsten（約 63MB）
#
# 口說「對話追問」用的 Whisper 語音辨識模型不在這裡：第一次用到時會自動下載到 models/whisper。
# 授權：Thorsten、Kerstin 為 CC0；Ramona、Karlsson 來自 M-AILABS 資料集；Kokoro 為 Apache-2.0。
#   使用前請看各模型的 model card（https://huggingface.co/rhasspy/piper-voices）確認符合你的用途。
set -euo pipefail
cd "$(dirname "$0")/.."

KOKORO=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
PIPER=https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE

fetch() {  # fetch <url> <dest>
  local url=$1 dest=$2
  if [ -s "$dest" ]; then echo "已有 $dest，略過"; return; fi
  mkdir -p "$(dirname "$dest")"
  echo "下載 $dest"
  curl -fL --retry 3 -C - -o "$dest.part" "$url"
  mv "$dest.part" "$dest"
}

voice() {  # voice <name> <quality>：同時下載 .onnx 與 .onnx.json
  for ext in onnx onnx.json; do
    fetch "$PIPER/$1/$2/de_DE-$1-$2.$ext" "models/de/de_DE-$1-$2.$ext"
  done
}

what=${1:-all}
case "$what" in all | en)
  fetch "$KOKORO/kokoro-v1.0.onnx" models/kokoro-v1.0.onnx
  fetch "$KOKORO/voices-v1.0.bin" models/voices-v1.0.bin ;;
esac
case "$what" in all | de | de-min) voice thorsten medium ;; esac
case "$what" in all | de)
  voice kerstin low
  voice ramona low
  voice karlsson low ;;
esac
echo "完成。models/ 已被 .gitignore 擋住，不會被上傳。"
