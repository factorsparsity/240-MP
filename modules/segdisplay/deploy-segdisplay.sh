#!/usr/bin/env bash
#
# deploy-segdisplay.sh — install the front-panel display (SSD1306 128x32 OLED,
# drawn as 7-segment digits) into a 240-MP install on the Pi.
#
# NOTHING IS COMPILED. Like the HDHomeRun and Music TV modules this is pure QML
# + Python, dropped into modules/segdisplay, so a 240-MP upgrade leaves it alone.
#
# The one exception, and it matters: this deploy also patches upstream's
# Main.qml (step 6). That file IS replaced by a 240-MP upgrade, so re-run this
# script after upgrading or the panel will show only the clock. Same deal as the
# patched YouTube module.
#
# Idempotent: re-run after any local change.
#
# Usage:  ./deploy-segdisplay.sh [user@host]
#
set -euo pipefail

# mDNS name, not an IP: the box's DHCP lease has already moved once (10.0.1.27 ->
# 10.0.1.28), and the old address still answers ping, so a stale IP fails as a
# confusing SSH refusal rather than as "host not found".
PI="${1:-vcr@240mp.local}"
KEY="${PI_SSH_KEY:-$HOME/.ssh/id_ed25519_pi240mp}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the segdisplay/ directory

ssh_pi() { ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 "$PI" "$@"; }
scp_pi() { scp -i "$KEY" -o BatchMode=yes -q "$@"; }
say() { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------------------
say "1/7 locating 240-MP install"
# An install.sh install is what the autostart service runs, so it wins over a
# source checkout left lying around. Deploying to the wrong root is silent —
# the module simply never appears — so print the one chosen.
APP_ROOT="${APP_ROOT:-$(ssh_pi 'if   [ -d /opt/240mp/share/240mp/modules ]; then echo /opt/240mp/share/240mp
                   elif [ -d /usr/local/share/240mp/modules ]; then echo /usr/local/share/240mp
                   elif [ -d "$HOME/240-MP/modules" ];         then echo "$HOME/240-MP"
                   fi')}"
if [[ -z "$APP_ROOT" ]]; then
    echo "ERROR: no 240-MP install found on $PI." >&2
    exit 1
fi
MOD="$APP_ROOT/modules/segdisplay"
HOME_DIR="$(ssh_pi 'echo $HOME')"
USER_NAME="$(ssh_pi 'id -un')"
echo "  appRoot: $APP_ROOT"

if ssh_pi "[ -w '$APP_ROOT/modules' ]"; then SUDO=""; else SUDO="sudo"; echo "  (using sudo to write)"; fi

# ---------------------------------------------------------------------------
say "2/7 copying module files"
ssh_pi "$SUDO mkdir -p '$MOD/views' '$MOD/scripts' '$MOD/assets/images' && \
        $SUDO chown -R \$(id -u):\$(id -g) '$MOD'"
scp_pi "$SRC"/manifest.json              "$PI:$MOD/"
scp_pi "$SRC"/views/*.qml                "$PI:$MOD/views/"
scp_pi "$SRC"/scripts/segdisplay.py      "$PI:$MOD/scripts/"
scp_pi "$SRC"/scripts/patch_main_qml.py  "$PI:$MOD/scripts/"
scp_pi "$SRC"/assets/images/logo.svg     "$PI:$MOD/assets/images/"
# Qt caches compiled QML by mtime+size; stale bytecode silently wins otherwise.
ssh_pi "rm -rf ~/.cache/240-MP/qmlcache"
echo "  ok (module files + QML bytecode cache invalidated)"

# ---------------------------------------------------------------------------
say "3/7 enabling I2C"
# The display hangs off I2C-1 (GPIO2/GPIO3). Both halves are needed and both are
# quiet when missing: without the dtparam there is no /dev/i2c-1 at all, and
# without the group the daemon gets EACCES opening it.
ssh_pi "CFG=/boot/firmware/config.txt
        [ -f \$CFG ] || CFG=/boot/config.txt
        if grep -qE '^dtparam=i2c_arm=on' \$CFG; then
            echo '  dtparam=i2c_arm=on already set'
        else
            echo 'dtparam=i2c_arm=on' | sudo tee -a \$CFG >/dev/null
            echo \"  dtparam=i2c_arm=on appended to \$CFG — NEEDS A REBOOT\"
        fi
        # dtparam enables the controller; i2c-dev is what actually creates
        # /dev/i2c-N. raspi-config does both, and doing only the first leaves a
        # box where lsmod shows i2c_bcm2835 loaded and there is still no device
        # node — which reads as a wiring fault, not a missing module.
        sudo modprobe i2c-dev 2>/dev/null || true
        if [ -f /etc/modules-load.d/i2c-dev.conf ]; then
            echo '  i2c-dev already set to load at boot'
        else
            echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf >/dev/null
            echo '  i2c-dev added to /etc/modules-load.d'
        fi
        if id -nG | tr ' ' '\n' | grep -qx i2c; then
            echo '  user already in the i2c group'
        else
            sudo usermod -aG i2c \$(id -un)
            echo '  user added to the i2c group — NEEDS A REBOOT'
        fi
        [ -e /dev/i2c-1 ] && echo '  /dev/i2c-1 present' || echo '  /dev/i2c-1 absent (expected before the reboot)'"

# ---------------------------------------------------------------------------
say "4/7 enabling the module in config.json"
# Merge, never rewrite: config.json is user work product (every other module's
# settings live in it too).
ssh_pi "python3 - <<'PYEOF'
import json, os
p = os.path.expanduser('~/.local/share/240-MP/config.json')
cfg = {}
if os.path.exists(p):
    with open(p) as fh:
        cfg = json.load(fh)
mods = cfg.setdefault('modules', {})
seg = mods.setdefault('com.240mp.segdisplay', {})
seg.setdefault('enabled', True)
with open(p, 'w') as fh:
    json.dump(cfg, fh, indent=2)
print('  ' + json.dumps(seg))
PYEOF"

# ---------------------------------------------------------------------------
say "5/7 systemd unit"
sed -e "s|@USER@|$USER_NAME|g" -e "s|@MODULE_DIR@|$MOD|g" -e "s|@HOME@|$HOME_DIR|g" \
    "$SRC/scripts/segdisplay.service" > /tmp/segdisplay.service
scp_pi /tmp/segdisplay.service "$PI:/tmp/segdisplay.service.staged"
ssh_pi "sudo install -m 644 /tmp/segdisplay.service.staged /etc/systemd/system/segdisplay.service && \
        rm -f /tmp/segdisplay.service.staged && \
        sudo systemctl daemon-reload && \
        sudo systemctl enable segdisplay.service >/dev/null 2>&1 && \
        sudo systemctl restart segdisplay.service && \
        sleep 1 && systemctl is-active segdisplay.service | sed 's/^/  segdisplay.service: /'"
rm -f /tmp/segdisplay.service

# ---------------------------------------------------------------------------
say "6/7 patching Main.qml"
# The only upstream file this project's own modules touch. 240-MP has no global
# "current screen" state, so two lines in Main.qml are the only way the panel can
# know which module is on screen (and therefore show StP, or honour the
# per-module toggles). Overwritten by a 240-MP upgrade — re-run this script then.
ssh_pi "$SUDO python3 '$MOD/scripts/patch_main_qml.py' '$APP_ROOT/Main.qml' | sed 's/^/  /'"

# ---------------------------------------------------------------------------
say "7/7 checking the display"
# 0x3C is the default on every SSD1306 breakout; a few are strapped to 0x3D, so
# say so here rather than leaving a bare NOT FOUND to read as a wiring fault.
ssh_pi "python3 '$MOD/scripts/segdisplay.py' --probe | sed 's/^/  /' || \
        echo '  (if the panel is strapped to 0x3D, set SEGDISPLAY_I2C_ADDR=0x3D in segdisplay.service)'"

cat <<EOF

=== done ===
Wiring (Pi 4 header):  GND->pin 6   VCC->pin 1 (3V3)   SDA->pin 3   SCL->pin 5

3V3, not 5V: the Pi's I2C lines never swing above 3.3V, and every SSD1306
breakout runs happily on 3V3 — no level shifter, nothing out of spec.

Next:
  1. If step 3 said NEEDS A REBOOT, reboot now.
  2. Bench-test the panel:
       python3 $MOD/scripts/segdisplay.py --test
     Whole screen lit, then each segment in turn, then 8.8.8.8 with the colon.
     If the image is upside down, that is a mounting choice, not a fault — add
     Environment=SEGDISPLAY_FLIP=1 to segdisplay.service.
  3. Load the new QML:  sudo reboot
     Do NOT use 'systemctl restart 240mp.service' — its ExecStopPost powers the
     machine off for every app exit status except 10 and 11.
  4. In the app: Settings > Modules > Front Panel for the per-module toggles,
     or open the module itself for a live status/display test.

Logs:  journalctl -u segdisplay.service -f
EOF
