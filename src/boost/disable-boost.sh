#!/bin/bash
# Remove Clawputer's optional RAM boost:  sudo /usr/share/clawputer/boost/disable-boost.sh
set -u
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
[ -x /usr/local/sbin/clawputer-boost ] && /usr/local/sbin/clawputer-boost off
[ -x /usr/local/sbin/clawputer-bootmode ] && /usr/local/sbin/clawputer-bootmode reset
systemctl disable clawputer-bootmode-reset.service >/dev/null 2>&1
rm -f /etc/systemd/system/clawputer-bootmode-reset.service /etc/sudoers.d/clawputer \
      /usr/local/sbin/clawputer-boost /usr/local/sbin/clawputer-bootmode
systemctl daemon-reload
echo "RAM boost removed. (/etc/clawputer/boost.conf kept)"
