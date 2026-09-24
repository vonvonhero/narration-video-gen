#!/usr/bin/env bash
# Temporarily enable or disable passwordless sudo for the invoking user.
set -euo pipefail

default_duration="2h"
max_seconds=$((12 * 3600))

usage() {
  cat <<'EOF'
Usage: scripts/passwordless-sudo.sh [status | enable [--for DURATION] | disable]

Manages one project-owned file under /etc/sudoers.d for the invoking user and a
systemd timer that removes it again.

  status    Show and validate the managed state (default).
  enable    Grant the invoking user passwordless sudo for all commands for
            DURATION (default 2h, at most 12h; for example 30m, 90m or 4h).
            Running it again while enabled starts a new period.
  disable   Remove the grant now and invalidate the sudo credential cache.

The grant ends when the period is over or at the next boot, whichever comes
first. With the original sudo (Ubuntu 24.04) the rule also carries NOTAFTER,
so sudo itself refuses it after the deadline. Enabling requires systemd and an
interactive terminal: a person starts the period, not an automated agent.

Run this as your normal user, not from a root shell. While enabled, anything
running as your user has full root access without authentication. Use it only
on a personal development machine and disable it once the setup work is done.
EOF
}

die() { echo "error: $*" >&2; exit 1; }
note() { echo "$*"; }

action="${1:-status}"
[ "$#" -eq 0 ] || shift
duration="$default_duration"
case "$action" in
  status|disable)
    [ "$#" -eq 0 ] || { usage >&2; die "too many arguments"; } ;;
  enable)
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --for) [ "$#" -ge 2 ] || die "--for needs a value"; duration="$2"; shift 2 ;;
        --for=*) duration="${1#--for=}"; shift ;;
        *) usage >&2; die "unknown option: $1" ;;
      esac
    done ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; die "unknown action: $action" ;;
esac

if [[ "$duration" =~ ^([1-9][0-9]{0,4})([mh])$ ]]; then
  duration_seconds="${BASH_REMATCH[1]}"
  [ "${BASH_REMATCH[2]}" = "h" ] && duration_seconds=$((duration_seconds * 3600)) \
    || duration_seconds=$((duration_seconds * 60))
else
  die "invalid duration: $duration (use minutes or hours, for example 30m or 2h)"
fi
[ "$duration_seconds" -le "$max_seconds" ] \
  || die "duration is longer than the 12h maximum: $duration"

if [ "$(id -u)" -eq 0 ]; then
  [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] \
    || die "run this script as the target normal user, not from a root shell"
  [[ "${SUDO_UID:-}" =~ ^[0-9]+$ ]] \
    || die "SUDO_UID is unavailable; run this script as the target normal user"
  target_user="$SUDO_USER"
else
  target_user="$(id -un)"
fi

[[ "$target_user" =~ ^[a-z_][a-z0-9_-]*$ ]] \
  || die "unsupported user name: $target_user"
getent passwd "$target_user" >/dev/null \
  || die "user does not exist: $target_user"
target_uid="$(id -u -- "$target_user")"
if [ "$(id -u)" -eq 0 ] && [ "$target_uid" != "$SUDO_UID" ]; then
  die "SUDO_USER and SUDO_UID do not identify the same account"
fi

dropin="/etc/sudoers.d/narration-video-gen-passwordless-$target_user"
unit="narration-video-gen-passwordless-sudo-expire-$target_user"
unit_dir="/etc/systemd/system"
service_file="$unit_dir/$unit.service"
timer_file="$unit_dir/$unit.timer"
service_link="$unit_dir/sysinit.target.wants/$unit.service"
timer_link="$unit_dir/timers.target.wants/$unit.timer"
unit_paths=("$service_file" "$timer_file" "$service_link" "$timer_link")

work_dir="$(mktemp -d)"
system_tmp=""
created_dropin=false

root_run() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

root_run_noninteractive() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

cleanup() {
  rm -rf -- "$work_dir"
  if "$created_dropin"; then
    if [ -n "$system_tmp" ] \
        && root_run test "$dropin" -ef "$system_tmp" >/dev/null 2>&1; then
      root_run rm -f -- "$dropin" >/dev/null 2>&1 || true
    fi
  fi
  if [ -n "$system_tmp" ]; then
    root_run rm -f -- "$system_tmp" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# The legacy format written by earlier versions, without any expiry.
render_legacy_dropin() {
  printf '%s\n' \
    '# Managed by narration-video-gen scripts/passwordless-sudo.sh.' \
    '# Remove with: scripts/passwordless-sudo.sh disable' \
    "$target_user ALL=(ALL:ALL) NOPASSWD: ALL"
}

# $1: expiry as YYYY-MM-DDTHH:MM:SSZ, $2: true to add NOTAFTER.
render_dropin() {
  local expires="$1" with_notafter="$2" date_spec=""
  if "$with_notafter"; then
    date_spec="NOTAFTER=$(tr -d ':T-' <<<"$expires") "
  fi
  printf '%s\n' \
    '# Managed by narration-video-gen scripts/passwordless-sudo.sh.' \
    "# Expires at $expires; removed by $unit.timer or at the next boot." \
    '# Remove now with: scripts/passwordless-sudo.sh disable' \
    "$target_user ALL=(ALL:ALL) ${date_spec}NOPASSWD: ALL"
}

# Removes the grant at the deadline (timer) or at the next boot (sysinit), then
# removes its own unit files. Only fixed system binaries and paths are used.
render_service() {
  printf '%s\n' \
    '# Managed by narration-video-gen scripts/passwordless-sudo.sh.' \
    '[Unit]' \
    "Description=Remove the temporary passwordless sudo grant for $target_user" \
    'DefaultDependencies=no' \
    'After=local-fs.target' \
    'Before=sysinit.target' \
    '' \
    '[Service]' \
    'Type=oneshot' \
    "ExecStart=/usr/bin/rm -f -- $dropin" \
    "ExecStartPost=/usr/bin/rm -f -- ${unit_paths[*]}" \
    '' \
    '[Install]' \
    'WantedBy=sysinit.target'
}

# $1: expiry as YYYY-MM-DDTHH:MM:SSZ. Default dependencies would order the
# timer after sysinit.target and the service after the timer, a cycle with the
# service's own Before=sysinit.target that would drop the removal at boot.
render_timer() {
  local expires="$1"
  printf '%s\n' \
    '# Managed by narration-video-gen scripts/passwordless-sudo.sh.' \
    '[Unit]' \
    "Description=Expire the temporary passwordless sudo grant for $target_user" \
    'DefaultDependencies=no' \
    'Conflicts=shutdown.target' \
    'Before=timers.target shutdown.target' \
    '' \
    '[Timer]' \
    "OnCalendar=${expires:0:10} ${expires:11:8} UTC" \
    'AccuracySec=1s' \
    '' \
    '[Install]' \
    'WantedBy=timers.target'
}

# Prints "legacy" or the expiry of the managed drop-in; fails when the path is
# not exactly a file this script writes. $1 runs commands as root.
inspect_dropin() {
  local runner="$1" content expires variant
  "$runner" test -f "$dropin" && ! "$runner" test -L "$dropin" || return 1
  [ "$("$runner" stat -c '%U:%G:%a' "$dropin")" = "root:root:440" ] || return 1
  content="$("$runner" cat -- "$dropin")" || return 1
  if [ "$content" = "$(render_legacy_dropin)" ]; then
    echo legacy
    return 0
  fi
  [[ "$content" =~ \#\ Expires\ at\ ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)\; ]] \
    || return 1
  expires="${BASH_REMATCH[1]}"
  for variant in true false; do
    if [ "$content" = "$(render_dropin "$expires" "$variant")" ]; then
      echo "$expires"
      return 0
    fi
  done
  return 1
}

managed_path_exists() {
  [ -e "$dropin" ] || [ -L "$dropin" ]
}

unit_files_exist() {
  local path
  for path in "${unit_paths[@]}"; do
    if [ -e "$path" ] || [ -L "$path" ]; then
      return 0
    fi
  done
  return 1
}

systemd_running() {
  [ -d /run/systemd/system ] && command -v systemctl >/dev/null
}

invalidate_target_cache() {
  if [ "$(id -u)" -eq 0 ]; then
    /usr/bin/sudo -u "$target_user" /usr/bin/sudo -k
  else
    sudo -k
  fi
}

format_local() { date -d "$1" '+%Y-%m-%d %H:%M:%S %Z'; }

if [ "$action" = "status" ]; then
  if ! managed_path_exists; then
    note "disabled: no managed sudoers file for $target_user"
    if unit_files_exist; then
      note "leftover expiry units exist; remove them with: scripts/passwordless-sudo.sh disable"
    fi
    exit 0
  fi
  root_run_noninteractive true 2>/dev/null \
    || die "a sudoers file exists at $dropin, but checking it needs a password: the grant has expired or was changed; run: scripts/passwordless-sudo.sh disable"
  state="$(inspect_dropin root_run_noninteractive)" \
    || die "unsafe or unrecognized sudoers file exists: $dropin"
  if [ "$state" = "legacy" ]; then
    note "enabled without expiry: legacy sudoers file for $target_user ($dropin)"
    note "replace it with a timed grant (enable) or remove it (disable)"
    exit 0
  fi
  remaining=$(( $(date -d "$state" +%s) - $(date +%s) ))
  if [ "$remaining" -le 0 ]; then
    die "the grant expired at $(format_local "$state") but is still installed; run: scripts/passwordless-sudo.sh disable"
  fi
  note "enabled: passwordless sudo for $target_user until $(format_local "$state") ($((remaining / 60)) min left)"
  if [ ! -e "$timer_file" ] || [ ! -e "$service_link" ]; then
    die "automatic removal is not scheduled; run: scripts/passwordless-sudo.sh disable"
  fi
  exit 0
fi

command -v sudo >/dev/null || die "sudo is required"

if [ "$action" = "disable" ]; then
  if managed_path_exists; then
    inspect_dropin root_run >/dev/null \
      || die "refusing to remove an unsafe or unrecognized sudoers file: $dropin"
  elif ! unit_files_exist; then
    invalidate_target_cache
    note "already disabled: no managed sudoers file for $target_user"
    exit 0
  fi
  # One command, so that no later step needs a password once the grant is gone.
  root_run rm -f -- "$dropin" "${unit_paths[@]}"
  if systemd_running; then
    root_run_noninteractive systemctl stop "$unit.timer" >/dev/null 2>&1 || true
    root_run_noninteractive systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  invalidate_target_cache
  note "disabled passwordless sudo for $target_user"
  note "the next sudo command will require a password"
  exit 0
fi

[ -t 0 ] && [ -t 2 ] \
  || die "enable must be run by a person in an interactive terminal"
systemd_running \
  || die "automatic expiry needs systemd, which is not running; not enabling"
id -nG -- "$target_user" | tr ' ' '\n' | grep -qx sudo \
  || die "$target_user is not in the sudo group; refusing to grant new privileges"
root_run test -x /usr/sbin/visudo || die "/usr/sbin/visudo is required"

replacing=false
if managed_path_exists; then
  inspect_dropin root_run >/dev/null \
    || die "refusing to overwrite an unsafe or unrecognized sudoers file: $dropin"
  replacing=true
fi

# sudo-rs (the default from Ubuntu 25.10) rejects NOTAFTER; the original sudo
# enforces it, which keeps the deadline even if the timer never runs.
with_notafter=false
if sudo -V 2>/dev/null | head -n 1 | grep -q '^Sudo version '; then
  with_notafter=true
fi
expires="$(date -u -d "@$(( $(date +%s) + duration_seconds ))" '+%Y-%m-%dT%H:%M:%SZ')"
render_dropin "$expires" "$with_notafter" >"$work_dir/dropin"
render_service >"$work_dir/service"
render_timer "$expires" >"$work_dir/timer"

# Schedule the removal before granting anything. Without a grant the units only
# remove themselves, so they never need to be rolled back.
root_run install -m 0644 -o root -g root "$work_dir/service" "$service_file"
root_run install -m 0644 -o root -g root "$work_dir/timer" "$timer_file"
root_run systemctl daemon-reload
root_run systemctl enable --quiet "$unit.service" "$unit.timer"
root_run systemctl restart "$unit.timer"
root_run systemctl is-active --quiet "$unit.timer" \
  || die "the expiry timer is not active; not enabling"
[ -L "$service_link" ] && [ -L "$timer_link" ] \
  || die "the expiry units were not enabled as expected; not enabling"

system_tmp="$(root_run mktemp /etc/sudoers.d/.narration-video-gen-passwordless.XXXXXX)"
root_run tee "$system_tmp" <"$work_dir/dropin" >/dev/null
root_run chown root:root "$system_tmp"
root_run chmod 0440 "$system_tmp"
root_run /usr/sbin/visudo -cf "$system_tmp" >/dev/null

if "$replacing"; then
  # An atomic rename keeps a grant in place throughout, so that no step in
  # between asks for a password that an unattended caller could not give.
  trap '' HUP INT TERM
  root_run mv -f -T -- "$system_tmp" "$dropin"
  system_tmp=""
else
  # A hard link is atomic and fails rather than replacing an existing path.
  created_dropin=true
  if ! root_run ln "$system_tmp" "$dropin"; then
    created_dropin=false
    die "refusing to replace a sudoers path created concurrently: $dropin"
  fi
fi
if ! root_run /usr/sbin/visudo -cf /etc/sudoers >/dev/null; then
  root_run rm -f -- "$dropin"
  created_dropin=false
  die "sudoers validation failed; removed the managed file"
fi

# Commit the validated change without an interruptible gap between dropping the
# comparison link and marking the final link as persistent.
trap '' HUP INT TERM
if [ -n "$system_tmp" ]; then
  root_run rm -- "$system_tmp"
  system_tmp=""
fi
created_dropin=false

note "enabled passwordless sudo for $target_user until $(format_local "$expires")"
note "it is removed automatically then, or at the next boot if that comes first"
note "to end it earlier: scripts/passwordless-sudo.sh disable"
trap - HUP INT TERM
