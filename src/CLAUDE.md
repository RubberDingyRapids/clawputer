# You are running ON a CardputerZero (via Clawputer)

You are Claude Code running directly on an **M5Stack CardputerZero**: a pocket Linux handheld
(Raspberry Pi CM0, 4x Cortex-A53 @ 1GHz, 512MB RAM, Debian aarch64). The user types on its tiny
built-in keyboard and reads a **320x170 screen** through the Clawputer chat app.

## How the user sees you
- Clawputer shows your replies in full, word-wrapped. Tool calls are squashed to one line and
  **tool output is trimmed on screen** (first few lines), so summarise results in your reply
  instead of saying "see above".
- Permission prompts appear as `allow? y / n / a`. AskUserQuestion works (options + own answer).
- You can't see the screen or press keys. If something needs the user's hands (a button,
  plugging something in, a reboot), say exactly what to do.

## Keep replies short
- ~50 columns x ~15 lines on screen. Lead with the answer, skip preamble, avoid wide tables and
  long code blocks unless asked.

## Resources are tight
- ~350MB RAM free normally (more if the user enabled Clawputer's RAM boost). You use ~200MB.
- Don't run heavy things (big compiles, large npm/pip installs, Docker, big downloads) without
  asking first. Swap is compressed RAM; if things crawl, it's memory pressure.

## The device
- Built in: LCD (`/dev/fb_lcd`), keyboard, battery gauge (`/sys/class/power_supply/*/capacity`),
  backlight (`/sys/class/backlight/*/brightness`), IR tx/rx, IMU, mic/speaker, maybe a camera.
- Top EXT header: an optional **Cap module** may be attached (e.g. Cap LoRa-1262 with GNSS, or a
  Cap CC1101 + NFC) or nothing. Check before assuming: the header's 5V is often off by default
  (`/sys/class/leds/ext_5v_out/brightness`, 255 = on); `i2cdetect -y 1` lists I2C devices
  (0x43 = the LoRa cap's IO expander). Ask the user if unsure. Caps are **not hot-swappable**:
  tell the user to power off before changing them. Only one app may use a radio at a time.
- `sudo` needs the user's password. Don't try sudo commands; tell the user what to run.
- Launcher apps are `.desktop` files in `/usr/share/APPLaunch/applications/` (root-owned).

## Working style
- This folder (`~/clawputer`) is your working folder. Put projects under it.
- Python 3 is available (PIL, numpy installed). Prefer Python or bash.
- If a task really needs a bigger machine, say so: Clawputer can also run Claude on a PC
  (remote mode) and use the Cardputer as the screen.
