#!/bin/sh
set -eu
trap 'if [ "$?" != 0 ]; then echo RUFUS_USERSPACE_FAIL_SESSION_CHECK; fi' EXIT
[ "$(id -u)" -gt 0 ]
[ -n "${XDG_SESSION_ID:-}" ]
loginctl show-session "$XDG_SESSION_ID" -p User -p Class -p State -p Type
[ "$(loginctl show-session "$XDG_SESSION_ID" -p User --value)" = "$(id -u)" ]
# Recent systemd uses user-light for Casper's live account (UID 890). It is
# still an interactive user session, without automatically starting user@.service.
case "$(loginctl show-session "$XDG_SESSION_ID" -p Class --value)" in
    user|user-light) ;;
    *) exit 1 ;;
esac
[ "$(loginctl show-session "$XDG_SESSION_ID" -p Type --value)" = tty ]
[ -t 0 ] && [ -t 1 ]
[ -d "$HOME" ] && [ -w "$HOME" ]
cd "$HOME"
marker="$HOME/rufus-persistence-@NONCE@"
if [ -e "$marker" ]; then
    [ "$(cat "$marker")" = '@NONCE@' ]
    result=SECOND
else
    (umask 077; set -C; printf '%s\n' '@NONCE@' >"$marker")
    result=FIRST
fi
[ "$(stat -c %u "$marker")" = "$(id -u)" ]
sync
echo "RUFUS_USER_SESSION $(id -un) $(id -u) $HOME $XDG_SESSION_ID"
echo "RUFUS_USERSPACE_${result}_BOOT_OK"
