#!/bin/sh
# Appended after the distribution's original init-bottom hooks, only in test copies.
if [ "$1" = prereqs ]; then exit 0; fi
exec >/dev/@CONSOLE@ 2>&1
fail() {
    echo "RUFUS_PERSISTENCE_FAIL_$1"
    cat /proc/mounts
    sync
    poweroff -f
    exit 1
}
if [ '@KIND@' = casper ]; then
    store=$(awk '$2 == "/cow" && $3 == "ext4" {print $2; exit}' /proc/mounts)
else
    store=$(awk '$3 == "ext4" && $2 ~ /\/live\/persistence\// {print $2; exit}' /proc/mounts)
fi
[ -n "$store" ] || fail NO_EXT4_STORE
options=$(awk -v root="$rootmnt" '$2 == root && $3 == "overlay" {options=$4} END {print options}' /proc/mounts)
[ -n "$options" ] || fail NO_ROOT_OVERLAY
upper=${options##*upperdir=}
upper=${upper%%,*}
case "$upper" in "$store"/*) ;; *) fail WRONG_UPPER_STORE ;; esac
if [ '@KIND@' = debian ]; then
    [ "$(cat "$store/persistence.conf")" = '/ union' ] || fail WRONG_UNION_CONFIG
fi
# /run is moved into the real root by initramfs-tools. Keep all instrumentation
# there, so the user's persistent root never contains test services or scripts.
mkdir -p /run/rufus-userspace /run/systemd/system/multi-user.target.wants || fail INSTALL
# Give the harness sole ownership of this serial port. A concurrent getty's
# vhangup would invalidate the checker's output descriptor and hide results.
ln -s /dev/null /run/systemd/system/serial-getty@@CONSOLE@.service || fail INSTALL
cp /rufus-userspace/* /run/rufus-userspace/ || fail INSTALL
printf '%s\n' "${upper#"$store"}" >/run/rufus-userspace/upper
cat >/run/systemd/system/rufus-userspace.service <<'EOF'
[Unit]
Description=Rufus disposable VM userspace acceptance check
After=systemd-user-sessions.service live-config.service
[Service]
Type=simple
ExecStart=/bin/sh /run/rufus-userspace/live-userspace-check.sh
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/@CONSOLE@
Environment=SYSTEMD_COLORS=0 SYSTEMD_URLIFY=0 TERM=dumb
EOF
ln -s ../rufus-userspace.service /run/systemd/system/multi-user.target.wants/rufus-userspace.service || fail INSTALL
echo RUFUS_USERSPACE_INSTRUMENTATION_READY
