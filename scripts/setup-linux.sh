#!/usr/bin/env bash
# Prepare a supported Ubuntu host for narration-video-gen.
#
# With no arguments this is an interactive setup wizard. --check is always
# read-only. Explicit action modes remain available for automation.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="wizard"
mode_count=0
test_gpu=false
yes=false
ubuntu_codename=""
is_wsl=false
driver_marker="/var/lib/narration-video-gen/driver-install-boot-id"
apt_preflight_marker="/var/lib/narration-video-gen/apt-preflight"
apt_preflight_max_age=86400
swap_target_gib=32
swap_os_reserve_gib=20
managed_swapfile="/swapfile-narration-video-gen"
fstab_path="/etc/fstab"
meminfo_path="/proc/meminfo"
swap_fstab_begin="# BEGIN narration-video-gen managed swap"
swap_fstab_end="# END narration-video-gen managed swap"
if [ -n "${NVG_CONFIG_HOME:-}" ]; then
  ui_config_dir="$NVG_CONFIG_HOME"
elif [ -n "${XDG_CONFIG_HOME:-}" ]; then
  ui_config_dir="$XDG_CONFIG_HOME/narration-video-gen"
else
  ui_config_dir="$HOME/.config/narration-video-gen"
fi
ui_language="${NVG_UI_LANGUAGE:-}"
if [ "$ui_language" != "ja" ] && [ "$ui_language" != "en" ]; then
  ui_language=""
  if [ -f "$ui_config_dir/ui-language" ]; then
    IFS= read -r ui_language <"$ui_config_dir/ui-language" || ui_language=""
  fi
fi
if [ "$ui_language" != "ja" ] && [ "$ui_language" != "en" ]; then
  ui_locale="${LC_ALL:-${LC_MESSAGES:-${LANG:-C}}}"
  case "$ui_locale" in
    ja*|JA*) ui_language="ja" ;;
    *)       ui_language="en" ;;
  esac
fi

usage() {
  cat <<'EOF'
Usage: scripts/setup-linux.sh [--check | --setup | --install-driver | --test-gpu | --remove-swap] [--yes]

  (no arguments)     Interactive setup
  --check            Status only
  --setup            Set up Docker and NVIDIA Container Toolkit
  --install-driver   Install the recommended NVIDIA driver
  --test-gpu         Test Docker GPU access
  --remove-swap      Remove swap managed by this script
  --yes              Approve an explicit action
EOF
}

die() { echo "error: $*" >&2; exit 1; }
note() { echo "$*"; }
say() {
  if [ "$ui_language" = "ja" ]; then
    note "$1"
  else
    note "$2"
  fi
}
die_ui() {
  if [ "$ui_language" = "ja" ]; then
    die "$1"
  else
    die "$2"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check) mode="check"; mode_count=$((mode_count + 1)) ;;
    --setup) mode="setup"; mode_count=$((mode_count + 1)) ;;
    --install-driver) mode="install-driver"; mode_count=$((mode_count + 1)) ;;
    --remove-swap) mode="remove-swap"; mode_count=$((mode_count + 1)) ;;
    --test-gpu) test_gpu=true ;;
    --yes) yes=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die_ui "不明なオプションです: $1" "unknown option: $1" ;;
  esac
  shift
done

[ "$mode_count" -le 1 ] \
  || die_ui "--check、--setup、--install-driver、--remove-swapは1つだけ指定してください" \
    "choose only one of --check, --setup, --install-driver, or --remove-swap"
if "$test_gpu"; then
  [ "$mode_count" -eq 0 ] \
    || die_ui "--test-gpuは単独で指定してください。--checkは常に読み取り専用です" \
      "--test-gpu is a standalone action; --check is always read-only"
  mode="test-gpu"
fi
[ "$mode" != "wizard" ] || ! "$yes" \
  || die_ui "--yesには明示的な操作モードが必要です" \
    "--yes requires an explicit action mode"

require_supported_host() {
  [ "$(uname -s)" = "Linux" ] \
    || die_ui "このスクリプトはLinuxで実行してください" "this script must run on Linux"
  [ "$(uname -m)" = "x86_64" ] \
    || die_ui "amd64/x86_64のみ対応しています" "only amd64/x86_64 is supported"
  [ -r /etc/os-release ] \
    || die_ui "/etc/os-releaseを読み取れません" "/etc/os-release is unavailable"
  # shellcheck disable=SC1091
  . /etc/os-release
  [ "${ID:-}" = "ubuntu" ] \
    || die_ui "対応ホストはUbuntu 24.04または26.04 LTSです。検出: ${PRETTY_NAME:-unknown}" \
      "supported host is Ubuntu 24.04 or 26.04 LTS; found ${PRETTY_NAME:-unknown}"
  case "${VERSION_ID:-}:${VERSION_CODENAME:-}" in
    24.04:noble|26.04:resolute) ubuntu_codename="$VERSION_CODENAME" ;;
    *) die_ui "対応ホストはUbuntu 24.04 (noble)または26.04 (resolute) LTSです。検出: ${PRETTY_NAME:-unknown}" \
         "supported host is Ubuntu 24.04 (noble) or 26.04 (resolute) LTS; found ${PRETTY_NAME:-unknown}" ;;
  esac
}

need_confirmation() {
  if ! "$yes"; then
    die_ui "$1 はホストを変更します。内容を確認後、--yesを付けて再実行してください" \
      "$1 changes the host; re-run with --yes after reviewing the action"
  fi
}

sudo_run() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    command -v sudo >/dev/null \
      || die_ui "この操作にはsudoが必要です" "sudo is required for this action"
    sudo "$@"
  fi
}

confirm() {
  local answer
  [ -r /dev/tty ] \
    || die_ui "対話セットアップには端末が必要です。--checkまたは明示的な操作モードを使用してください" \
      "interactive setup requires a terminal; use --check or an explicit action mode"
  if [ "$ui_language" = "ja" ]; then
    printf '\n続行しますか？ [y/N]: ' >/dev/tty
  else
    printf '\nContinue? [y/N]: ' >/dev/tty
  fi
  IFS= read -r answer </dev/tty || return 1
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

confirm_apt_refresh() {
  local answer
  [ -r /dev/tty ] \
    || die_ui "対話セットアップには端末が必要です。--checkまたは明示的な操作モードを使用してください" \
      "interactive setup requires a terminal; use --check or an explicit action mode"
  if [ "$ui_language" = "ja" ]; then
    printf '\n今すぐ更新しますか？ [y/N]: ' >/dev/tty
  else
    printf '\nRefresh now? [y/N]: ' >/dev/tty
  fi
  IFS= read -r answer </dev/tty || return 1
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

confirm_reboot() {
  local answer
  [ -r /dev/tty ] || return 1
  if [ "$ui_language" = "ja" ]; then
    printf '\n今すぐ再起動しますか？ [y/N]: ' >/dev/tty
  else
    printf '\nReboot now? [y/N]: ' >/dev/tty
  fi
  IFS= read -r answer </dev/tty || return 1
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

confirm_optional_reboot() {
  local answer
  [ -r /dev/tty ] || return 1
  if [ "$ui_language" = "ja" ]; then
    printf '\n代わりに再起動しますか？ [y/N]: ' >/dev/tty
  else
    printf '\nReboot instead? [y/N]: ' >/dev/tty
  fi
  IFS= read -r answer </dev/tty || return 1
  case "$answer" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

activate_docker_group_or_reboot() {
  printf '\nNext:\n'
  say "dockerグループの反映には再ログインが必要です。" \
    "Log in again to activate the docker group."
  if [ -n "${SSH_CONNECTION:-}" ] || [ -n "${SSH_TTY:-}" ]; then
    say "SSH接続を exit で切断し、再接続後に scripts/setup-linux.sh を実行してください。" \
      "Exit this SSH connection, reconnect, then run scripts/setup-linux.sh."
  else
    say "ログアウトして再ログインした後、scripts/setup-linux.sh を実行してください。" \
      "Log out and back in, then run scripts/setup-linux.sh."
  fi
  if confirm_optional_reboot; then
    say "再起動します。ログイン後、scripts/setup-linux.sh を再実行してください。" \
      "Rebooting now. Run scripts/setup-linux.sh again after login."
    sudo_run systemctl reboot
  fi
}

invoking_user() {
  if [ -n "${SUDO_USER:-}" ]; then
    printf '%s\n' "$SUDO_USER"
  else
    id -un
  fi
}

has_nvidia_driver() {
  command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1
}

has_nvidia_pci_device() {
  local device vendor class class_code
  has_nvidia_driver && return 0
  for vendor in /sys/bus/pci/devices/*/vendor; do
    device="$(dirname "$vendor")"
    class="$device/class"
    [ -r "$vendor" ] && [ -r "$class" ] || continue
    [ "$(tr -d '\n' < "$vendor")" = "0x10de" ] || continue
    class_code="$(tr -d '\n' < "$class")"
    class_code="${class_code#0x}"
    case "${class_code:0:4}" in
      0300|0302) return 0 ;;
    esac
  done
  return 1
}

nvidia_gpu_name() {
  local bdf device device_id name vendor_id
  if has_nvidia_driver; then
    name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null \
      | awk 'NF { if (seen++) printf ", "; printf "%s", $0 } END { if (seen) print "" }')" \
      || true
    [ -z "$name" ] || { printf '%s\n' "$name"; return; }
  fi
  for device in /sys/bus/pci/devices/*; do
    [ -r "$device/vendor" ] && [ -r "$device/class" ] || continue
    [ "$(tr -d '\n' < "$device/vendor")" = "0x10de" ] || continue
    case "$(tr -d '\n' < "$device/class")" in
      0x0300*|0x0302*) ;;
      *) continue ;;
    esac
    bdf="$(basename "$device")"
    if command -v lspci >/dev/null; then
      name="$(lspci -Dnn -s "$bdf" 2>/dev/null \
        | sed -E 's/^.*(VGA compatible controller|3D controller|Display controller)( \[[^]]+\])?: //' \
        | sed -E 's/^NVIDIA Corporation [^[]*\[([^]]+)\] \[10de:[^]]+\]( \(rev [^)]+\))?$/NVIDIA \1/')" \
        || true
      [ -z "$name" ] || { printf '%s\n' "$name"; return; }
    fi
    vendor_id="$(tr -d '\n' < "$device/vendor")"
    device_id="$(tr -d '\n' < "$device/device")"
    printf 'NVIDIA GPU (PCI ID %s:%s)\n' "${vendor_id#0x}" "${device_id#0x}"
    return
  done
  printf '%s\n' 'NVIDIA GPU (model unavailable)'
}

docker_works() {
  command -v docker >/dev/null \
    && { docker_works_directly \
      || { command -v sudo >/dev/null \
        && sudo -n docker version --format '{{.Server.Version}}' >/dev/null 2>&1; }; }
}

docker_works_directly() {
  command -v docker >/dev/null \
    && docker version --format '{{.Server.Version}}' >/dev/null 2>&1
}

docker_read() {
  docker "$@" 2>/dev/null || sudo -n docker "$@" 2>/dev/null
}

nvidia_runtime_registered() {
  docker_works && docker_read info --format '{{json .Runtimes}}' | grep -q '"nvidia"'
}

nvidia_runtime_registered_privileged() {
  if docker_works_directly; then
    nvidia_runtime_registered
  else
    command -v docker >/dev/null \
      && sudo_run docker info --format '{{json .Runtimes}}' 2>/dev/null \
        | grep -q '"nvidia"'
  fi
}

account_in_docker_group() {
  local gid user
  user="$(invoking_user)"
  [ "$user" = "root" ] && return 0
  gid="$(getent group docker 2>/dev/null | awk -F: '$1 == "docker" { print $3 }')"
  [ -n "$gid" ] && id -G "$user" | tr ' ' '\n' | grep -qx "$gid"
}

session_has_docker_group() {
  local gid
  [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ] && return 0
  [ "$(id -un)" = "$(invoking_user)" ] || return 1
  gid="$(getent group docker 2>/dev/null | awk -F: '$1 == "docker" { print $3 }')"
  [ -n "$gid" ] && id -G | tr ' ' '\n' | grep -qx "$gid"
}

current_boot_id() {
  tr -d '\n' </proc/sys/kernel/random/boot_id
}

driver_marker_exists() {
  [ -r "$driver_marker" ]
}

driver_reboot_pending() {
  driver_marker_exists && [ "$(tr -d '\n' <"$driver_marker")" = "$(current_boot_id)" ]
}

system_reboot_pending() {
  [ -f /var/run/reboot-required ]
}

swap_total_kib() {
  awk '$1 == "SwapTotal:" { print $2 + 0; found=1 } END { if (!found) print 0 }' \
    "$meminfo_path"
}

swap_total_display() {
  awk '$1 == "SwapTotal:" { printf "%.1f", $2 / 1048576; found=1 }
       END { if (!found) printf "0.0" }' "$meminfo_path"
}

swap_target_satisfied() {
  [ "$(swap_total_kib)" -ge $((swap_target_gib * 1048576)) ]
}

swap_missing_gib() {
  local missing_kib target_kib total_kib
  target_kib=$((swap_target_gib * 1048576))
  total_kib="$(swap_total_kib)"
  missing_kib=$((target_kib - total_kib))
  if [ "$missing_kib" -le 0 ]; then
    printf '0\n'
  else
    printf '%s\n' $(((missing_kib + 1048575) / 1048576))
  fi
}

managed_swap_active() {
  swapon --show=NAME --noheadings --raw 2>/dev/null \
    | awk -v path="$managed_swapfile" '$1 == path { found=1 } END { exit !found }'
}

fstab_has_managed_swap() {
  [ -r "$fstab_path" ] || return 1
  awk -v begin="$swap_fstab_begin" -v end="$swap_fstab_end" \
      -v path="$managed_swapfile" '
    $0 == begin {
      if (managed || begin_found) invalid=1
      managed=1; begin_found=1; next
    }
    $0 == end {
      if (!managed || end_found) invalid=1
      managed=0; end_found=1; next
    }
    managed && $1 == path && $3 == "swap" { entry_found=1 }
    END { exit (invalid || managed || !(begin_found && end_found && entry_found)) }
  ' "$fstab_path"
}

fstab_has_any_managed_marker() {
  [ -r "$fstab_path" ] || return 1
  awk -v begin="$swap_fstab_begin" -v end="$swap_fstab_end" '
    $0 == begin || $0 == end { found=1 }
    END { exit !found }
  ' "$fstab_path"
}

fstab_has_swap_path() {
  [ -r "$fstab_path" ] || return 1
  awk -v path="$managed_swapfile" '
    $0 !~ /^[[:space:]]*#/ && $1 == path && $3 == "swap" { found=1 }
    END { exit !found }
  ' "$fstab_path"
}

managed_swap_needs_persistence() {
  managed_swap_active && ! fstab_has_managed_swap
}

catalog_model_disk_reserve_gib() {
  python3 - "$root" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
from narration_video_gen.catalog import Catalog

values = [
    int(profile.get("requires", {}).get("free_disk_gib_min", 0))
    for profile in Catalog(root).profiles.values()
    if profile.get("platform") == "linux"
]
print(max(values, default=0))
PY
}

catalog_model_disk_bounds_gib() {
  python3 - "$root" <<'PYEOF'
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root / "src"))
from narration_video_gen.catalog import Catalog

values = [
    int(profile.get("requires", {}).get("free_disk_gib_min", 0))
    for profile in Catalog(root).profiles.values()
    if profile.get("platform") == "linux"
]
values = [value for value in values if value > 0]
print(min(values, default=0), max(values, default=0))
PYEOF
}

models_free_gib() {
  local target="$root/models"
  [ -d "$target" ] || target="$root"
  df -B1 --output=avail "$target" 2>/dev/null \
    | awk 'NR == 2 { print int($1 / 1073741824) }'
}

swap_root_filesystem() {
  findmnt -n -o FSTYPE --target /
}

swap_disk_reserve_gib() {
  local reserve
  reserve="$swap_os_reserve_gib"
  if [ "$(stat -c '%d' /)" = "$(stat -c '%d' "$root")" ]; then
    reserve=$((reserve + $(catalog_model_disk_reserve_gib)))
  fi
  printf '%s\n' "$reserve"
}

swap_root_free_gib() {
  local free_bytes
  free_bytes="$(df -B1 --output=avail / | awk 'NR == 2 { print $1 }')"
  printf '%s\n' $((free_bytes / 1073741824))
}

managed_swap_file_is_usable() {
  local minimum_bytes size_bytes type
  [ -f "$managed_swapfile" ] && [ ! -L "$managed_swapfile" ] || return 1
  [ "$(stat -c '%u' "$managed_swapfile" 2>/dev/null)" = "0" ] || return 1
  [ "$(stat -c '%a' "$managed_swapfile" 2>/dev/null)" = "600" ] || return 1
  minimum_bytes=$(( $(swap_missing_gib) * 1073741824 ))
  size_bytes="$(stat -c '%s' "$managed_swapfile" 2>/dev/null)" || return 1
  [ "$size_bytes" -ge "$minimum_bytes" ] || return 1
  type="$(sudo_run blkid -p -s TYPE -o value "$managed_swapfile" 2>/dev/null)" \
    || return 1
  [ "$type" = "swap" ]
}

write_managed_swap_fstab() {
  local backup fstab_tmp
  fstab_has_managed_swap && return 0
  fstab_has_any_managed_marker \
    && die_ui "$fstab_path に不完全または重複した管理対象swapブロックがあります" \
      "$fstab_path contains an incomplete or duplicate managed swap block"
  fstab_has_swap_path \
    && die_ui "$managed_swapfile のfstab設定が既に存在しますが、このスクリプトの管理ブロックではありません" \
      "an fstab entry for $managed_swapfile already exists outside this script's managed block"
  fstab_tmp="${fstab_path}.narration-video-gen.tmp.$$"
  backup="${fstab_path}.narration-video-gen.$(date +%Y%m%d%H%M%S%N).bak"
  sudo_run cp --preserve=mode,ownership "$fstab_path" "$fstab_tmp" || return 1
  if ! printf '\n%s\n%s none swap sw 0 0\n%s\n' \
      "$swap_fstab_begin" "$managed_swapfile" "$swap_fstab_end" \
      | sudo_run tee -a "$fstab_tmp" >/dev/null; then
    sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true
    return 1
  fi
  if ! sudo_run findmnt --verify --tab-file "$fstab_tmp" >/dev/null; then
    sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true
    return 1
  fi
  sudo_run cp --preserve=mode,ownership "$fstab_path" "$backup" \
    || { sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true; return 1; }
  sudo_run mv "$fstab_tmp" "$fstab_path" || return 1
  say "変更前のfstabを $backup へ保存しました。" \
    "Saved the previous fstab to $backup."
}

remove_managed_swap_fstab() {
  local backup fstab_tmp
  fstab_has_managed_swap || return 0
  fstab_tmp="${fstab_path}.narration-video-gen.tmp.$$"
  backup="${fstab_path}.narration-video-gen.$(date +%Y%m%d%H%M%S%N).bak"
  if ! awk -v begin="$swap_fstab_begin" -v end="$swap_fstab_end" \
      -v path="$managed_swapfile" '
        $0 == begin { managed=1; next }
        $0 == end { managed=0; next }
        managed { next }
        { print }
      ' "$fstab_path" | sudo_run tee "$fstab_tmp" >/dev/null; then
    sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true
    return 1
  fi
  sudo_run chown --reference="$fstab_path" "$fstab_tmp" \
    || { sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true; return 1; }
  sudo_run chmod --reference="$fstab_path" "$fstab_tmp" \
    || { sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true; return 1; }
  if ! sudo_run findmnt --verify --tab-file "$fstab_tmp" >/dev/null; then
    sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true
    return 1
  fi
  sudo_run cp --preserve=mode,ownership "$fstab_path" "$backup" \
    || { sudo_run rm -f "$fstab_tmp" >/dev/null 2>&1 || true; return 1; }
  sudo_run mv "$fstab_tmp" "$fstab_path" || return 1
  say "変更前のfstabを $backup へ保存しました。" \
    "Saved the previous fstab to $backup."
}

managed_swap_used_bytes() {
  swapon --show=NAME,USED --bytes --noheadings --raw 2>/dev/null \
    | awk -v path="$managed_swapfile" '$1 == path { printf "%.0f\n", $2; found=1 }
      END { if (!found) print 0 }'
}

remove_managed_swap() {
  local available_bytes used_bytes
  if [ -e "$managed_swapfile" ] || [ -L "$managed_swapfile" ]; then
    [ -f "$managed_swapfile" ] && [ ! -L "$managed_swapfile" ] \
      && [ "$(stat -c '%u' "$managed_swapfile" 2>/dev/null)" = "0" ] \
      && [ "$(stat -c '%a' "$managed_swapfile" 2>/dev/null)" = "600" ] \
      || die_ui "$managed_swapfile を安全な管理対象ファイルとして確認できないため削除しません" \
        "refusing to remove $managed_swapfile because it is not a safe managed file"
  fi

  if ! fstab_has_managed_swap; then
    if managed_swap_active || [ -e "$managed_swapfile" ] || [ -L "$managed_swapfile" ]; then
      die_ui "$managed_swapfile のfstab管理ブロックを確認できないため削除しません" \
        "refusing to remove $managed_swapfile because its managed fstab block is missing"
    fi
    say "このスクリプトが管理するswapはありません。変更しません。" \
      "No swap managed by this script exists; nothing changed."
    return 0
  fi

  if managed_swap_active; then
    used_bytes="$(managed_swap_used_bytes)"
    available_bytes="$(awk '$1 == "MemAvailable:" { printf "%.0f\n", $2 * 1024 }' "$meminfo_path")"
    if [ "$used_bytes" -gt 0 ]; then
      [ "$available_bytes" -ge $((used_bytes + 1073741824)) ] \
        || die_ui "管理対象swapの使用ページをRAMへ戻す空きが不足しています。削除せず停止します" \
          "not enough available RAM to move used managed-swap pages back; nothing was removed"
    fi
    sudo_run swapoff "$managed_swapfile" || return 1
  fi

  remove_managed_swap_fstab
  if [ -e "$managed_swapfile" ]; then
    sudo_run rm -f "$managed_swapfile" || return 1
  fi
  say "管理対象swapを削除しました。既存の他のswapは変更していません。" \
    "Removed the managed swap. Existing swap was left unchanged."
}

create_or_activate_managed_swap() {
  local add_gib
  add_gib="$(swap_missing_gib)"
  [ "$add_gib" -gt 0 ] || return 0

  if managed_swap_active; then
    die_ui "$managed_swapfile は有効ですが、合計swapが目標未満です。自動拡張せず停止します" \
      "$managed_swapfile is active but total swap is below target; refusing to resize it automatically"
  fi

  if [ -e "$managed_swapfile" ] || [ -L "$managed_swapfile" ]; then
    managed_swap_file_is_usable \
      || die_ui "$managed_swapfile が存在しますが、安全な管理対象swapファイルとして確認できません" \
        "$managed_swapfile exists but is not a safe, usable managed swap file"
    sudo_run swapon "$managed_swapfile" || return 1
  else
    sudo_run fallocate -l "${add_gib}G" "$managed_swapfile" || return 1
    sudo_run chmod 0600 "$managed_swapfile" \
      || { sudo_run rm -f "$managed_swapfile" >/dev/null 2>&1 || true; return 1; }
    sudo_run mkswap "$managed_swapfile" \
      || { sudo_run rm -f "$managed_swapfile" >/dev/null 2>&1 || true; return 1; }
    sudo_run swapon "$managed_swapfile" \
      || { sudo_run rm -f "$managed_swapfile" >/dev/null 2>&1 || true; return 1; }
  fi

  swap_target_satisfied \
    || die_ui "swapを有効化しましたが、合計が ${swap_target_gib} GiBに届きません" \
      "swap was activated but total swap is still below ${swap_target_gib} GiB"
}

setup_swap_if_needed() {
  local add_gib free_after free_before fs reserve
  if swap_target_satisfied && ! managed_swap_needs_persistence; then
    return 0
  fi

  if managed_swap_needs_persistence; then
    printf '\nNext:\n'
    say "$managed_swapfile を再起動後も有効にします。" \
      "Make $managed_swapfile persistent across reboots."
    if ! confirm; then
      say "swapの永続化をスキップし、セットアップを続けます。" \
        "Swap persistence skipped; continuing setup."
      return 0
    fi
    write_managed_swap_fstab
    say "管理対象swapを再起動後も有効にしました。" \
      "Managed swap will now activate after reboot."
    return 0
  fi

  fs="$(swap_root_filesystem)"
  case "$fs" in
    ext4|xfs) ;;
    *)
      printf '\nNext:\n'
      say "$fsではswapを自動追加できません。手動で設定してください。" \
        "Automatic swap setup is unavailable on $fs; configure it manually."
      return 0
      ;;
  esac

  add_gib="$(swap_missing_gib)"
  free_before="$(swap_root_free_gib)"
  free_after=$((free_before - add_gib))
  reserve="$(swap_disk_reserve_gib)"
  if [ "$free_after" -lt "$reserve" ]; then
    printf '\nNext:\n'
    say "swap追加には空き容量が不足しています（必要: ${reserve} GiB）。" \
      "Not enough disk space for swap (required reserve: ${reserve} GiB)."
    return 0
  fi

  printf '\nNext:\n'
  say "既存swapは変更せず、${add_gib} GiBを追加し、" \
    "Keep the existing swap and add ${add_gib} GiB"
  say "合計${swap_target_gib} GiBへ永続化します。" \
    "for a persistent total of ${swap_target_gib} GiB."
  if ! confirm; then
    say "swapの追加をスキップし、セットアップを続けます。" \
      "Swap creation skipped; continuing setup."
    return 0
  fi
  create_or_activate_managed_swap
  write_managed_swap_fstab
  say "swapを合計 $(swap_total_display) GiBへ増やしました。" \
    "Total swap is now $(swap_total_display) GiB."
}

apt_preflight_fresh() {
  local recorded_at recorded_codename recorded_version now
  [ -r "$apt_preflight_marker" ] || return 1
  read -r recorded_at recorded_version recorded_codename <"$apt_preflight_marker" || return 1
  case "$recorded_at" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ "$recorded_version" = "$VERSION_ID" ] || return 1
  [ "$recorded_codename" = "$VERSION_CODENAME" ] || return 1
  now="$(date +%s)"
  [ "$now" -ge "$recorded_at" ] || return 1
  [ $((now - recorded_at)) -le "$apt_preflight_max_age" ]
}

record_apt_preflight() {
  local marker_tmp
  marker_tmp="${apt_preflight_marker}.tmp.$$"
  sudo_run install -d -m 0755 "$(dirname "$apt_preflight_marker")" || return 1
  printf '%s %s %s\n' "$(date +%s)" "$VERSION_ID" "$VERSION_CODENAME" \
    | sudo_run tee "$marker_tmp" >/dev/null \
    || { sudo_run rm -f "$marker_tmp" >/dev/null 2>&1 || true; return 1; }
  sudo_run chmod 0644 "$marker_tmp" \
    || { sudo_run rm -f "$marker_tmp" >/dev/null 2>&1 || true; return 1; }
  sudo_run mv "$marker_tmp" "$apt_preflight_marker" \
    || { sudo_run rm -f "$marker_tmp" >/dev/null 2>&1 || true; return 1; }
}

apt_upgrade_count() {
  LC_ALL=C apt-get -s -o Debug::NoLocking=1 upgrade \
    | awk '/^Inst / { count++ } END { print count + 0 }'
}

run_apt_preflight() {
  local running upgrade_count
  apt_preflight_fresh && return 0

  printf '\nNext:\n'
  say "Ubuntuのパッケージ情報を更新します。" \
    "Refresh Ubuntu package information."
  if ! confirm_apt_refresh; then
    say "スキップして続けます。" "Skipped; continuing setup."
    return 0
  fi

  sudo_run apt-get update || return 1
  upgrade_count="$(apt_upgrade_count)" || return 1
  if [ "$upgrade_count" -gt 0 ]; then
    printf '\nNext:\n'
    say "Ubuntuパッケージを ${upgrade_count} 件アップグレードします。" \
      "Upgrade $upgrade_count Ubuntu package(s)."
    if confirm; then
      if command -v docker >/dev/null; then
        running="$(sudo_run docker ps -q 2>/dev/null || true)"
        [ -z "$running" ] \
          || die_ui "システムパッケージを更新する前に、実行中のDockerコンテナを停止してください" \
            "stop running Docker containers before upgrading system packages"
      fi
      sudo_run apt-get upgrade -y || return 1
    else
      say "スキップして続けます。" "Skipped; continuing setup."
    fi
  fi
  record_apt_preflight || return 1
}

clear_driver_marker_if_satisfied() {
  if has_nvidia_driver && driver_marker_exists && ! driver_reboot_pending; then
    sudo_run rm -f "$driver_marker"
  fi
}

print_status() {
  local docker_version driver_version gpu_detected=false gpu_name
  say "Linuxセットアップ状況" "Linux setup status"
  printf '\n'
  say "[OK] Ubuntu $VERSION_ID amd64" "[OK] Ubuntu $VERSION_ID amd64"

  if "$is_wsl"; then
    say "[--] Swap: Windows/WSL側で管理" "[--] Swap: managed by Windows/WSL"
  elif swap_target_satisfied; then
    say "[OK] Swap: $(swap_total_display) GiB / 推奨 ${swap_target_gib} GiB" \
      "[OK] Swap: $(swap_total_display) GiB / recommended ${swap_target_gib} GiB"
    if managed_swap_needs_persistence; then
      say "[--] 管理対象swap: 再起動後の自動有効化が未設定" \
        "[--] Managed swap: not configured to activate after reboot"
    fi
  else
    say "[NG] Swap: $(swap_total_display) GiB / 推奨 ${swap_target_gib} GiB" \
      "[NG] Swap: $(swap_total_display) GiB / recommended ${swap_target_gib} GiB"
  fi

  local disk_free disk_min disk_max
  disk_free="$(models_free_gib)"
  read -r disk_min disk_max <<<"$(catalog_model_disk_bounds_gib)"
  if [ -n "$disk_free" ] && [ -n "$disk_max" ] && [ "$disk_max" != "0" ]; then
    if [ "$disk_free" -ge "$disk_max" ]; then
      say "[OK] モデル用ディスク: ${disk_free} GiB空き" \
        "[OK] Disk for models: ${disk_free} GiB free"
    elif [ "$disk_free" -ge "$disk_min" ]; then
      say "[--] モデル用ディスク: ${disk_free} GiB空き（Wan 2.2には ${disk_max} GiB必要）" \
        "[--] Disk for models: ${disk_free} GiB free (Wan 2.2 needs ${disk_max} GiB)"
    else
      say "[NG] モデル用ディスク: ${disk_free} GiB空き（最低 ${disk_min} GiB必要）" \
        "[NG] Disk for models: ${disk_free} GiB free (at least ${disk_min} GiB needed)"
    fi
  fi

  if has_nvidia_pci_device; then
    gpu_detected=true
    gpu_name="$(nvidia_gpu_name)"
    say "[OK] NVIDIA GPU検出: $gpu_name" "[OK] NVIDIA GPU detected: $gpu_name"
  else
    say "[NG] NVIDIA GPUを検出できません" "[NG] NVIDIA GPU is not detected"
  fi

  if "$is_wsl"; then
    say "[--] NVIDIAドライバー: Windows側で管理" "[--] NVIDIA driver: managed by Windows"
  elif ! "$gpu_detected"; then
    say "[--] NVIDIAドライバー: GPU検出後に確認" "[--] NVIDIA driver: check after GPU detection"
    say "[--] Docker Engine: GPU検出後に確認" "[--] Docker Engine: check after GPU detection"
    say "[--] NVIDIA Container Toolkit: Dockerセットアップ後に確認" "[--] NVIDIA Container Toolkit: check after Docker setup"
    return
  elif driver_reboot_pending; then
    say "[--] NVIDIAドライバー: インストール済み。再起動後に確認" "[--] NVIDIA driver: installed; check after reboot"
    say "[--] Docker Engine: 再起動後に確認" "[--] Docker Engine: check after reboot"
    say "[--] NVIDIA Container Toolkit: Dockerセットアップ後に確認" "[--] NVIDIA Container Toolkit: check after Docker setup"
    return
  elif ! has_nvidia_driver; then
    say "[NG] NVIDIAドライバーを利用できません" "[NG] NVIDIA driver is not usable"
    say "[--] Docker Engine: ドライバーセットアップ後に確認" "[--] Docker Engine: check after driver setup"
    say "[--] NVIDIA Container Toolkit: Dockerセットアップ後に確認" "[--] NVIDIA Container Toolkit: check after Docker setup"
    return
  else
    driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n 1)" \
      || true
    [ -n "$driver_version" ] || driver_version="unknown"
    say "[OK] NVIDIAドライバー: $driver_version" "[OK] NVIDIA driver: $driver_version"
  fi

  if docker_works; then
    docker_version="$(docker_read version --format '{{.Server.Version}}')" || true
    [ -n "$docker_version" ] || docker_version="unknown"
    say "[OK] Docker Engine: $docker_version" "[OK] Docker Engine: $docker_version"
  elif command -v docker >/dev/null \
    && account_in_docker_group && ! session_has_docker_group; then
    say "[--] Docker Engine: dockerグループ反映後に確認" "[--] Docker Engine: check after docker group activation"
  elif command -v docker >/dev/null && ! account_in_docker_group; then
    say "[--] Docker Engine: dockerグループ設定後に確認" "[--] Docker Engine: check after docker group setup"
  else
    say "[NG] Docker Engineを利用できません" "[NG] Docker Engine is not usable"
  fi

  if docker_works; then
    if nvidia_runtime_registered; then
      say "[OK] NVIDIA Container Toolkit" "[OK] NVIDIA Container Toolkit"
    else
      say "[NG] NVIDIA Container Toolkitが未設定です" "[NG] NVIDIA Container Toolkit is not configured"
    fi
  else
    say "[--] NVIDIA Container Toolkit: Dockerセットアップ後に確認" "[--] NVIDIA Container Toolkit: check after Docker setup"
  fi

  if session_has_docker_group; then
    say "[OK] dockerグループが有効です" "[OK] docker group is active"
  elif account_in_docker_group; then
    say "[--] dockerグループ: ログアウト・再ログイン後に反映" "[--] docker group: log out and back in to activate"
  else
    say "[NG] $(invoking_user) のdockerグループが未設定です" \
      "[NG] docker group is not configured for $(invoking_user)"
  fi

  if has_nvidia_driver && docker_works_directly \
    && nvidia_runtime_registered && session_has_docker_group; then
    if gpu_probe_verified; then
      say "[OK] DockerからのGPUアクセスをこの起動中に確認済みです" \
        "[OK] Docker GPU access verified for this boot"
    else
      say "[--] DockerからのGPUアクセスは未確認です" \
        "[--] Docker GPU access has not been verified"
    fi
  fi
}

print_next_for_check() {
  printf '\nNext:\n'
  if "$is_wsl"; then
    say "docs/manual/windows-wsl2.md に従ってDocker DesktopとWSL統合を確認してください。" \
      "Follow docs/manual/windows-wsl2_en.md to check Docker Desktop and WSL integration."
  elif ! has_nvidia_pci_device; then
    say "NVIDIA GPUまたはGPUパススルーを確認してください。" \
      "Check the NVIDIA GPU or GPU passthrough."
  elif system_reboot_pending; then
    say "保留中のシステム更新を反映するため、再起動してください。" \
      "Reboot to apply pending system updates."
  elif driver_reboot_pending; then
    say "再起動後、scripts/setup-linux.sh を再実行してください。" \
      "Reboot, then run scripts/setup-linux.sh again."
  elif ! has_nvidia_driver; then
    if driver_marker_exists; then
      say "Secure Boot/MOKとドライバーログを確認してください。再試行は --install-driver --yes で明示的に行います。" \
        "Check Secure Boot/MOK and the driver logs. Retry explicitly with --install-driver --yes."
    else
      say "scripts/setup-linux.sh を実行し、NVIDIAドライバーのセットアップを進めてください。" \
        "Run scripts/setup-linux.sh to continue NVIDIA driver setup."
    fi
  elif account_in_docker_group && ! session_has_docker_group; then
    say "ログアウト・再ログイン後、scripts/setup-linux.sh を再実行してください。" \
      "Log out and back in, then run scripts/setup-linux.sh again."
  elif command -v docker >/dev/null && session_has_docker_group && ! docker_works; then
    say "Dockerデーモンを起動または修復してください。" \
      "Start or repair the Docker daemon."
  elif ! docker_works || ! nvidia_runtime_registered || ! account_in_docker_group; then
    say "scripts/setup-linux.sh を実行し、Dockerのセットアップを進めてください。" \
      "Run scripts/setup-linux.sh to continue Docker setup."
  elif ! swap_target_satisfied || managed_swap_needs_persistence; then
    say "scripts/setup-linux.sh を実行し、swapのセットアップを確認してください。" \
      "Run scripts/setup-linux.sh to review swap setup."
  elif gpu_probe_verified; then
    say "セットアップ完了: ./bin/narration-video-gen plan" \
      "Setup complete: ./bin/narration-video-gen plan"
    say "GPUの実測（任意）: ./bin/narration-video-gen detect --measure" \
      "Measure the GPU (optional): ./bin/narration-video-gen detect --measure"
  else
    say "scripts/setup-linux.sh を実行してGPUコンテナテストへ進んでください。" \
      "Run scripts/setup-linux.sh to continue to the GPU container test."
  fi
}

install_docker() {
  if docker_works; then
    say "Docker Engine: OK" "Docker Engine: OK"
    return
  fi
  if command -v docker >/dev/null; then
    if sudo_run docker version --format '{{.Server.Version}}' >/dev/null 2>&1; then
      say "Docker Engine: OK" "Docker Engine: OK"
      return
    fi
    die_ui "Dockerはインストール済みですがデーモンを利用できません。置き換えず、既存環境を起動または修復してください" \
      "Docker is installed but its daemon is not usable; repair or start the existing installation instead of replacing it"
  fi
  say "Docker公式UbuntuリポジトリからDocker Engineをインストールします。" \
    "Installing Docker Engine from Docker's official Ubuntu repository."
  sudo_run apt-get update
  sudo_run apt-get install -y ca-certificates curl
  sudo_run install -m 0755 -d /etc/apt/keyrings
  sudo_run curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo_run chmod a+r /etc/apt/keyrings/docker.asc
  sudo_run tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $ubuntu_codename
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  sudo_run apt-get update
  sudo_run apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo_run systemctl enable --now docker
}

install_toolkit() {
  local backup running
  if nvidia_runtime_registered_privileged; then
    say "NVIDIA Container Toolkit: OK" "NVIDIA Container Toolkit: OK"
    return
  fi
  running="$(sudo_run docker ps -q)"
  [ -z "$running" ] \
    || die_ui "--setupの前に実行中のDockerコンテナを停止してください。NVIDIA runtime設定ではDockerを再起動します" \
      "stop existing Docker containers before --setup; configuring the NVIDIA runtime restarts Docker"
  say "NVIDIA Container Toolkitをインストールし、Dockerを設定します。" \
    "Installing NVIDIA Container Toolkit and configuring Docker."
  sudo_run apt-get update
  sudo_run apt-get install -y ca-certificates curl gpg
  sudo_run install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo_run gpg --dearmor --yes -o /etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo_run tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo_run apt-get update
  sudo_run apt-get install -y nvidia-container-toolkit
  if sudo_run test -f /etc/docker/daemon.json; then
    backup="/etc/docker/daemon.json.narration-video-gen.$(date +%Y%m%d%H%M%S).bak"
    sudo_run cp /etc/docker/daemon.json "$backup"
    say "既存のDocker daemon設定を $backup へバックアップしました。" \
      "Backed up the existing Docker daemon configuration to $backup"
  fi
  sudo_run nvidia-ctk runtime configure --runtime=docker --dry-run >/dev/null 2>&1 \
    || die_ui "Docker設定を作成できません" "could not prepare the Docker configuration"
  sudo_run nvidia-ctk runtime configure --runtime=docker >/dev/null 2>&1 \
    || die_ui "Docker設定を更新できません" "could not update the Docker configuration"
  sudo_run systemctl restart docker
}

install_driver() {
  if has_nvidia_driver; then
    say "NVIDIAドライバー: OK" "NVIDIA driver: OK"
    return
  fi
  has_nvidia_pci_device \
    || die_ui "NVIDIA GPUが見つかりません。GPUパススルーまたはハードウェアを確認してください" \
      "no NVIDIA GPU is visible; check GPU passthrough or hardware"
  say "Ubuntu推奨のNVIDIAドライバーをインストールします。" \
    "Installing Ubuntu's recommended NVIDIA driver."
  say "完了後に再起動が必要です。Secure BootではMOK登録が必要な場合があります。" \
    "Reboot required; Secure Boot may require MOK enrollment."
  sudo_run apt-get update
  sudo_run apt-get install -y ubuntu-drivers-common
  sudo_run ubuntu-drivers install
  sudo_run install -d -m 0755 "$(dirname "$driver_marker")"
  current_boot_id | sudo_run tee "$driver_marker" >/dev/null
  sudo_run chmod 0644 "$driver_marker"
  say "NVIDIAドライバーをインストールしました。" \
    "NVIDIA driver installed."
}

gpu_probe_image() {
  python3 - "$root" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))
from narration_video_gen.compat import load_yaml_file
lock = load_yaml_file(Path(sys.argv[1]) / "manifests" / "containers.lock.yaml")
probe = lock["gpu_probe_image"]
print("%s@%s" % (probe["reference"], probe["digest"]))
PY
}

gpu_probe_state_dir() {
  printf '/tmp/narration-video-gen-%s\n' "$(id -u)"
}

gpu_probe_fingerprint() {
  local docker_version driver_version image
  driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null \
    | head -n 1)" || return 1
  docker_version="$(docker_read version --format '{{.Server.Version}}')" || return 1
  image="$(gpu_probe_image)" || return 1
  printf '%s %s %s %s\n' "$(current_boot_id)" "$driver_version" "$docker_version" "$image"
}

gpu_probe_state_dir_is_safe() {
  local dir mode owner
  dir="$(gpu_probe_state_dir)"
  [ -d "$dir" ] && [ ! -L "$dir" ] || return 1
  owner="$(stat -c '%u' "$dir" 2>/dev/null)" || return 1
  mode="$(stat -c '%a' "$dir" 2>/dev/null)" || return 1
  [ "$owner" = "$(id -u)" ] && [ "$mode" = "700" ]
}

gpu_probe_verified() {
  local dir expected recorded
  docker_works_directly && has_nvidia_driver && nvidia_runtime_registered \
    && session_has_docker_group || return 1
  dir="$(gpu_probe_state_dir)"
  gpu_probe_state_dir_is_safe || return 1
  [ -r "$dir/gpu-probe" ] && [ ! -L "$dir/gpu-probe" ] || return 1
  expected="$(gpu_probe_fingerprint)" || return 1
  recorded="$(tr -d '\n' <"$dir/gpu-probe")" || return 1
  [ "$recorded" = "$expected" ]
}

record_gpu_probe() {
  local dir fingerprint marker_tmp
  dir="$(gpu_probe_state_dir)"
  if ! mkdir -m 0700 "$dir" 2>/dev/null; then
    gpu_probe_state_dir_is_safe || return 1
  fi
  chmod 0700 "$dir" || return 1
  fingerprint="$(gpu_probe_fingerprint)" || return 1
  marker_tmp="$(mktemp "$dir/gpu-probe.tmp.XXXXXX")" || return 1
  if ! printf '%s\n' "$fingerprint" >"$marker_tmp" \
    || ! chmod 0600 "$marker_tmp" \
    || ! mv -f "$marker_tmp" "$dir/gpu-probe"; then
    rm -f "$marker_tmp" 2>/dev/null || true
    return 1
  fi
}

test_gpu_container() {
  local image
  image="$(gpu_probe_image)"
  say "GPUテスト用イメージを取得し、DockerからGPUを確認します。" \
    "Downloading a test image and checking Docker GPU access."
  if docker_works_directly; then
    docker run --rm --gpus all "$image" nvidia-smi
  else
    sudo_run docker run --rm --gpus all "$image" nvidia-smi
  fi
  if ! record_gpu_probe; then
    say "GPUテストは成功しましたが、一時的な検証記録を保存できませんでした。次回もう一度確認します。" \
      "The GPU test passed, but its temporary verification record could not be saved; it will be checked again next time."
  fi
}

run_video_menu_command() {
  # Keep user-owned settings and outputs when setup was launched with sudo.
  if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    (cd "$root" && sudo -H -u "$SUDO_USER" env NVG_UI_LANGUAGE="$ui_language" ./bin/narration-video-gen "$@")
  else
    (cd "$root" && ./bin/narration-video-gen "$@")
  fi
}

tts_web_password_configured() {
  run_video_menu_command tts web password-status >/dev/null 2>&1
}

open_tts_web_ui() {
  local access_choice
  say "TTS Web UIの公開範囲を選んでください。" "Choose who can access the TTS Web UI."
  say "  1. このPCからのみ（パスワード不要）" "  1. This PC only (no password)"
  say "  2. ローカルネットワークからも許可（パスワード必須）" \
    "  2. Also allow the local network (password required)"
  say "  0. キャンセル" "  0. Cancel"
  say "番号を入力してください: " "Enter a number: "
  IFS= read -r access_choice || return 0
  case "$access_choice" in
    1)
      run_video_menu_command tts web start
      ;;
    2)
      if ! tts_web_password_configured; then
        say "ローカルネットワークへ公開するには、先にパスワードを設定してください。" \
          "Set a password before allowing access from the local network."
        if ! run_video_menu_command tts web reset-password; then
          return 1
        fi
      fi
      run_video_menu_command tts web start --lan
      ;;
    0)
      return 0
      ;;
    *)
      say "0から2の番号を入力してください。" "Enter a number from 0 to 2."
      return 1
      ;;
  esac
}

show_video_menu() {
  local choice action answer
  say "セットアップ完了。次の操作を選んでください。" "Setup complete. Choose what to do next."
  while true; do
    printf '\n'
    say "動画生成メニュー" "Video generation menu"
    say "  1. キャラクターと音声を作る (Web UI)" "  1. Create characters and speech (Web UI)"
    say "  2. 動画の構成選択と準備" "  2. Choose and prepare a video configuration"
    say "  3. 動画を生成" "  3. Generate a video"
    say "  4. 生成状況を確認" "  4. Check generation status"
    say "  5. 実行中の生成を中止" "  5. Cancel the current generation"
    say "  6. TTS WebUIのパスワードを再設定" "  6. Reset the TTS WebUI password"
    say "  0. 終了" "  0. Exit"
    say "番号を入力してください: " "Enter a number: "
    IFS= read -r choice || return 0
    case "$choice" in
      1) action=tts-web ;;
      2) action=plan ;;
      3) action=run ;;
      4) action=status ;;
      5)
        say "実行中の動画生成を中止しますか？ [y/N]" "Cancel the current video generation? [y/N]"
        IFS= read -r answer || return 0
        case "$answer" in y|Y|yes|YES|Yes) action=cancel ;; *) continue ;; esac
        ;;
      6) action="tts web reset-password" ;;
      0) return 0 ;;
      *) say "0から6の番号を入力してください。" "Enter a number from 0 to 6."; continue ;;
    esac
    if [ "$action" = "tts-web" ]; then
      if open_tts_web_ui; then menu_status=0; else menu_status=$?; fi
    elif [ "$action" = "tts web reset-password" ]; then
      if run_video_menu_command tts web reset-password; then menu_status=0; else menu_status=$?; fi
    else
      if run_video_menu_command "$action"; then menu_status=0; else menu_status=$?; fi
    fi
    if [ "$menu_status" -eq 0 ]; then
      :
    else
      say "操作は完了していません。表示された内容を確認してください。" \
        "The operation did not complete. Check the message above."
    fi
  done
}

run_wizard() {
  print_status

  if "$is_wsl"; then
    printf '\nNext:\n'
    say "Windows側の設定は docs/manual/windows-wsl2.md を確認してください。" \
      "See docs/manual/windows-wsl2_en.md for Windows setup."
    return
  fi

  if ! has_nvidia_pci_device; then
    printf '\nNext:\n'
    say "NVIDIA GPUが見つかりません。ハードウェアまたはGPUパススルーを確認してください。" \
      "No NVIDIA GPU was found. Check the hardware or GPU passthrough."
    return
  fi

  if system_reboot_pending; then
    printf '\nNext:\n'
    say "保留中のシステム更新を反映するため、再起動します。" \
      "Reboot to apply pending system updates."
    if confirm_reboot; then
      say "再起動します。ログイン後、scripts/setup-linux.sh を再実行してください。" \
        "Rebooting now. Run scripts/setup-linux.sh again after login."
      sudo_run systemctl reboot
    else
      say "再起動を中止しました。準備ができたら scripts/setup-linux.sh を再実行してください。" \
        "Reboot cancelled. Run scripts/setup-linux.sh again when ready."
    fi
    return
  fi

  if driver_reboot_pending; then
    printf '\nNext:\n'
    say "インストールしたNVIDIAドライバーを読み込むため、再起動します。" \
      "Reboot to load the installed NVIDIA driver."
    if confirm_reboot; then
      say "再起動します。ログイン後、scripts/setup-linux.sh を再実行してください。" \
        "Rebooting now. Run scripts/setup-linux.sh again after login."
      sudo_run systemctl reboot
    else
      say "再起動を中止しました。準備ができたら scripts/setup-linux.sh を再実行してください。" \
        "Reboot cancelled. Run scripts/setup-linux.sh again when ready."
    fi
    return
  fi

  run_apt_preflight

  if system_reboot_pending; then
    printf '\nNext:\n'
    say "パッケージ更新を反映してからドライバーを設定するため、再起動します。" \
      "Reboot to apply package updates before configuring the driver."
    if confirm_reboot; then
      say "再起動します。ログイン後、scripts/setup-linux.sh を再実行してください。" \
        "Rebooting now. Run scripts/setup-linux.sh again after login."
      sudo_run systemctl reboot
    else
      say "再起動を中止しました。準備ができたら scripts/setup-linux.sh を再実行してください。" \
        "Reboot cancelled. Run scripts/setup-linux.sh again when ready."
    fi
    return
  fi

  if ! has_nvidia_driver; then
    if driver_marker_exists; then
      printf '\nNext:\n'
      say "NVIDIAドライバーを利用できません。Secure Boot/MOKとログを確認してください。" \
        "The NVIDIA driver is unusable. Check Secure Boot/MOK and the logs."
      say "再インストール: scripts/setup-linux.sh --install-driver --yes" \
        "Reinstall: scripts/setup-linux.sh --install-driver --yes"
      return
    fi
    has_nvidia_pci_device \
      || die_ui "NVIDIA GPUが見つかりません。ハードウェアまたはGPUパススルーを先に直してください" \
        "no NVIDIA GPU is visible; fix hardware or GPU passthrough first"
    printf '\nNext:\n'
    say "Ubuntu推奨のNVIDIAドライバーをインストールします。" \
      "Install Ubuntu's recommended NVIDIA driver."
    say "完了後に再起動が必要です。Secure BootではMOK登録が必要な場合があります。" \
      "Reboot required; Secure Boot may require MOK enrollment."
    if ! confirm; then
      say "NVIDIAドライバーのインストールを中止しました。" \
        "NVIDIA driver installation cancelled."
      return
    fi
    install_driver
    printf '\nNext:\n'
    say "インストールしたNVIDIAドライバーを読み込むため、再起動します。" \
      "Reboot to load the installed NVIDIA driver."
    if confirm_reboot; then
      say "再起動します。ログイン後、scripts/setup-linux.sh を再実行してください。" \
        "Rebooting now. Run scripts/setup-linux.sh again after login."
      sudo_run systemctl reboot
    else
      say "再起動を中止しました。準備ができたら scripts/setup-linux.sh を再実行してください。" \
        "Reboot cancelled. Run scripts/setup-linux.sh again when ready."
    fi
    return
  fi

  if account_in_docker_group && ! session_has_docker_group; then
    activate_docker_group_or_reboot
    return
  fi

  if command -v docker >/dev/null && session_has_docker_group && ! docker_works; then
    printf '\nNext:\n'
    say "Dockerデーモンを起動または修復してください。" \
      "Start or repair the Docker daemon."
    return
  fi

  if ! docker_works || ! nvidia_runtime_registered || ! account_in_docker_group; then
    printf '\nNext:\n'
    say "不足しているDocker EngineとNVIDIA Container Toolkitをセットアップします。" \
      "Set up the missing Docker Engine and NVIDIA Container Toolkit components."
    say "Dockerを再起動し、$(invoking_user) をdockerグループへ追加する場合があります。" \
      "Docker may restart and $(invoking_user) may be added to the docker group."
    if ! confirm; then
      say "Dockerのセットアップを中止しました。" "Docker setup cancelled."
      return
    fi
    clear_driver_marker_if_satisfied
    install_docker
    install_toolkit
    if ! account_in_docker_group; then
      sudo_run usermod -aG docker "$(invoking_user)"
      say "$(invoking_user) をdockerグループへ追加しました。" \
        "Added $(invoking_user) to the docker group."
    fi
  fi

  if ! session_has_docker_group; then
    activate_docker_group_or_reboot
    return
  fi

  setup_swap_if_needed

  if gpu_probe_verified; then
    printf '\nNext:\n'
    show_video_menu
    return
  fi

  printf '\nNext:\n'
  say "小さなCUDAイメージを取得し、DockerからGPUを確認します。" \
    "Download a small CUDA image and verify Docker GPU access."
  if confirm; then
    clear_driver_marker_if_satisfied
    test_gpu_container
    show_video_menu
  else
    say "後で実行: scripts/setup-linux.sh --test-gpu --yes" \
      "Run later: scripts/setup-linux.sh --test-gpu --yes"
  fi
}

# Reject incomplete actions before probing the host or preparing any changes.
case "$mode" in
  setup|install-driver|remove-swap|test-gpu) need_confirmation "--$mode" ;;
esac
if [ -n "${WSL_INTEROP:-}" ] \
  || grep -qi microsoft /proc/sys/kernel/osrelease 2>/dev/null; then
  is_wsl=true
fi
if "$is_wsl"; then
  case "$mode" in
    setup)
      die_ui "WSLでは--setupを使用できません。Docker DesktopのWSL統合を使用してください" \
        "--setup is disabled under WSL; use Docker Desktop WSL integration"
      ;;
    install-driver)
      die_ui "WSLでは--install-driverを使用できません。Windows側へGPUドライバーを導入してください" \
        "--install-driver is disabled under WSL; install the GPU driver on Windows"
      ;;
    remove-swap)
      die_ui "WSLでは--remove-swapを使用できません。Windows/WSL側でswapを管理してください" \
        "--remove-swap is disabled under WSL; manage swap in Windows/WSL"
      ;;
  esac
fi
require_supported_host
if "$is_wsl"; then
  case "$root" in
    /mnt|/mnt/*)
      say "警告: Windows側のドライブ上ではモデルI/Oが遅くなります。WSL側の ~/narration-video-gen を使用してください。" \
        "Warning: model I/O is slower on a Windows-mounted drive; use ~/narration-video-gen inside WSL."
      printf '\n'
      ;;
  esac
fi
case "$mode" in
  wizard)
    run_wizard
    ;;
  check)
    print_status
    print_next_for_check
    ;;
  setup)
    has_nvidia_driver \
      || die_ui "NVIDIAドライバーを利用できません。先に--install-driverを確認してください" \
        "NVIDIA driver is not usable; review --install-driver first"
    clear_driver_marker_if_satisfied
    install_docker
    install_toolkit
    if ! account_in_docker_group; then
      sudo_run usermod -aG docker "$(invoking_user)"
      say "$(invoking_user) をroot相当のdockerグループへ追加しました。ログアウトして再ログインしてください。" \
        "Added $(invoking_user) to the root-equivalent docker group. Log out and back in before running without sudo."
    fi
    print_status
    print_next_for_check
    ;;
  install-driver)
    system_reboot_pending \
      && die_ui "システムの再起動が必要です。NVIDIAドライバー導入前に再起動してください" \
        "a system reboot is already required; reboot before installing the NVIDIA driver"
    install_driver
    clear_driver_marker_if_satisfied
    ;;
  remove-swap)
    remove_managed_swap
    ;;
  test-gpu)
    has_nvidia_driver \
      || die_ui "NVIDIAドライバーを利用できません。導入して再起動してください" \
        "NVIDIA driver is not usable; install it and reboot first"
    clear_driver_marker_if_satisfied
    nvidia_runtime_registered_privileged \
      || die_ui "Docker NVIDIA runtimeが登録されていません。先に--setupを実行してください" \
        "Docker NVIDIA runtime is not registered; run --setup first"
    test_gpu_container
    ;;
esac
