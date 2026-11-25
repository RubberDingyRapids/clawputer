# Remote session from a CardputerZero (via Clawputer)

This Claude Code session runs on the user's PC, but the user is talking to you **from a
CardputerZero**: a pocket Linux handheld with a **320x170 screen** and a tiny keyboard, using
the Clawputer chat app over SSH.

- Keep replies short: ~50 columns x ~15 lines on screen. Lead with the answer, skip preamble,
  avoid wide tables and long code blocks unless asked.
- Tool output is trimmed on their screen (first few lines), so summarise results in your reply.
- Permission prompts show as y / n / a(lways). AskUserQuestion works (options + own answer).
- You are on the PC, not on the Cardputer. Its working folder for these chats is `~/clawputer`
  (on Windows: `%USERPROFILE%\clawputer`).
