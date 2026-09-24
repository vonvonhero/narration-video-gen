#!/usr/bin/env bash
# Temporarily enable or disable passwordless sudo for the invoking user.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/passwordless-sudo.sh [status | enable | disable]

Manages one project-owned file under /etc/sudoers.d for the invoking user.

  status    Show and validate the managed sudoers state (default).
  enable    Grant the invoking user passwordless sudo for all commands.
  disable   Remove that grant and invalidate the sudo credential cache.

Run this as your normal user, not from a root shell. Enabling passwordless sudo
grants full root access without authentication and remains active until disable
is run. Use it only on a personal development machine and disable it as soon as
the temporary setup work is finished.
EOF
}

die() { echo "error: $*" >&2; exit 1; }
note() { echo "$*"; }

action="${1:-status}"
[ "$#" -le 1 ] || { usage >&2; die "too many arguments"; }
case "$action" in
  status|enable|disable) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; die "unknown action: $action" ;;
esac

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
expected_tmp="$(mktemp)"
system_tmp=""
created_dropin=false

cleanup() {
  rm -f -- "$expected_tmp"
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

chmod 0600 "$expected_tmp"
printf '%s\n' \
  '# Managed by narration-video-gen scripts/passwordless-sudo.sh.' \
  '# Remove with: scripts/passwordless-sudo.sh disable' \
  "$target_user ALL=(ALL:ALL) NOPASSWD: ALL" >"$expected_tmp"

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

managed_file_is_exact() {
  local runner="$1"
  "$runner" test -f "$dropin" \
    && ! "$runner" test -L "$dropin" \
    && [ "$("$runner" stat -c '%U:%G:%a' "$dropin")" = "root:root:440" ] \
    && "$runner" cmp -s "$expected_tmp" "$dropin"
}

managed_path_exists() {
  [ -e "$dropin" ] || [ -L "$dropin" ]
}

invalidate_target_cache() {
  if [ "$(id -u)" -eq 0 ]; then
    /usr/bin/sudo -u "$target_user" /usr/bin/sudo -k
  else
    sudo -k
  fi
}

if [ "$action" = "status" ]; then
  if ! managed_path_exists; then
    note "disabled: no managed sudoers file for $target_user"
  elif managed_file_is_exact root_run_noninteractive; then
    note "enabled: managed sudoers file is valid for $target_user ($dropin)"
  else
    die "unsafe or unrecognized sudoers file exists: $dropin"
  fi
  exit 0
fi

command -v sudo >/dev/null || die "sudo is required"

if [ "$action" = "disable" ]; then
  if ! managed_path_exists; then
    invalidate_target_cache
    note "already disabled: no managed sudoers file for $target_user"
    exit 0
  fi
  managed_file_is_exact root_run \
    || die "refusing to remove an unsafe or unrecognized sudoers file: $dropin"
  root_run rm -- "$dropin"
  invalidate_target_cache
  note "disabled passwordless sudo for $target_user"
  note "the next sudo command will require a password"
  exit 0
fi

id -nG -- "$target_user" | tr ' ' '\n' | grep -qx sudo \
  || die "$target_user is not in the sudo group; refusing to grant new privileges"
root_run test -x /usr/sbin/visudo || die "/usr/sbin/visudo is required"

if managed_path_exists; then
  managed_file_is_exact root_run \
    || die "refusing to overwrite an unsafe or unrecognized sudoers file: $dropin"
  note "already enabled: managed sudoers file is valid for $target_user"
  exit 0
fi

system_tmp="$(root_run mktemp /etc/sudoers.d/.narration-video-gen-passwordless.XXXXXX)"
root_run tee "$system_tmp" <"$expected_tmp" >/dev/null
root_run chown root:root "$system_tmp"
root_run chmod 0440 "$system_tmp"
root_run /usr/sbin/visudo -cf "$system_tmp" >/dev/null

# A hard link is atomic and fails rather than replacing an existing path.
created_dropin=true
if ! root_run ln "$system_tmp" "$dropin"; then
  created_dropin=false
  die "refusing to replace a sudoers path created concurrently: $dropin"
fi
if ! root_run /usr/sbin/visudo -cf /etc/sudoers >/dev/null; then
  if "$created_dropin"; then
    root_run rm -f -- "$dropin"
  fi
  die "sudoers validation failed; removed the managed file"
fi

# Commit the validated change without an interruptible gap between dropping the
# comparison link and marking the final link as persistent.
trap '' HUP INT TERM
root_run rm -- "$system_tmp"
system_tmp=""
created_dropin=false

note "enabled passwordless sudo for $target_user"
note "full root access now requires no password and does not expire automatically"
note "disable it when setup is finished: scripts/passwordless-sudo.sh disable"
trap - HUP INT TERM
