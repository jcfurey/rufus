#!/bin/sh
# Runs under the real distribution's systemd, after its normal live setup.
exec >/dev/@CONSOLE@ 2>&1
fail() {
    echo "RUFUS_USERSPACE_FAIL_$1"
    systemctl --failed --no-pager
    journalctl -b --no-pager -o short-monotonic
    sync
    systemctl poweroff --no-block
    exit 1
}
state=$(systemctl is-system-running --wait)
case "$state" in running|degraded) ;; *) fail SYSTEM_STARTUP ;; esac
systemctl is-active --quiet multi-user.target || fail MULTI_USER
echo "RUFUS_USERSPACE_STATE $state"
echo "RUFUS_USERSPACE_UPPER $(cat /run/rufus-userspace/upper)"
systemctl --failed --no-pager
loginctl list-sessions --no-pager
# Casper's live UID can be below 1000. Require an ordinary home and login shell,
# and refuse ambiguous accounts rather than creating a synthetic test user.
users=$(awk -F: '$3 > 0 && $6 ~ /^\/home\/[a-z_][a-z0-9_-]*$/ && $7 !~ /(nologin|false)$/ {print $1}' /etc/passwd)
set -- $users
[ "$#" = 1 ] || fail LIVE_USER_SELECTION
user=$1
systemd-run --quiet --wait --pty --collect --service-type=exec \
    --unit=rufus-user-session --uid="$user" --property=PAMName=login \
    /bin/sh /run/rufus-userspace/live-session-check.sh || fail USER_SESSION
echo RUFUS_USERSPACE_JOURNAL_BEGIN
journalctl -b --no-pager -o short-monotonic
echo RUFUS_USERSPACE_JOURNAL_END
sync
# No forced shutdown: wait for the distribution's normal service and filesystem
# teardown. The host requires the kernel's final power-down message too.
systemctl poweroff --no-block || fail SHUTDOWN
