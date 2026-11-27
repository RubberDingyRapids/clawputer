#!/bin/bash
# Optional Clawputer RAM boost. Run once with:  sudo /usr/share/clawputer/boost/enable-boost.sh
# Installs two small root helpers + a sudoers rule so Clawputer can pause background services
# (and optionally do a one-boot low-GPU-memory reboot) without asking for your password.
# Undo with: sudo clawputer-boost-uninstall
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
U="${SUDO_USER:-}"
[ -n "$U" ] && [ "$U" != root ] || { echo "run with sudo from your normal user"; exit 1; }
D=/usr/share/clawputer/boost

install -m 755 -o root -g root "$D/clawputer-boost" /usr/local/sbin/clawputer-boost
install -m 755 -o root -g root "$D/clawputer-bootmode" /usr/local/sbin/clawputer-bootmode
# removal command that keeps working even after the app is uninstalled
install -m 755 -o root -g root "$D/disable-boost.sh" /usr/local/sbin/clawputer-boost-uninstall
mkdir -p /etc/clawputer
[ -f /etc/clawputer/boost.conf ] || cat > /etc/clawputer/boost.conf <<'EOF'
# services Clawputer's RAM boost pauses while Claude runs on the Cardputer (restored after)
SERVICES="lightdm cups cups-browsed avahi-daemon udisks2 accounts-daemon packagekit"
EOF

cat > /etc/systemd/system/clawputer-bootmode-reset.service <<'EOF'
[Unit]
Description=Clawputer: make the low-GPU boot last only one boot
After=local-fs.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/clawputer-bootmode reset
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable clawputer-bootmode-reset.service >/dev/null 2>&1

cat > /etc/sudoers.d/clawputer <<EOF
$U ALL=(root) NOPASSWD: /usr/local/sbin/clawputer-boost on, /usr/local/sbin/clawputer-boost off, /usr/local/sbin/clawputer-bootmode enter, /usr/local/sbin/clawputer-bootmode exit, /usr/local/sbin/clawputer-bootmode keep on, /usr/local/sbin/clawputer-bootmode keep off
EOF
chmod 440 /etc/sudoers.d/clawputer
visudo -cf /etc/sudoers.d/clawputer >/dev/null
echo "RAM boost enabled for $U."
