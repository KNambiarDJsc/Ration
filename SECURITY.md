# Security Policy

RATION runs inside your coding-agent workflow (Claude Code hooks and status line), so security and privacy issues are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository. Include steps to reproduce, the affected version (`ration --version`), and your OS / Claude Code version. You should get a response within a few days.

## What counts as a security issue

- Anything that sends local data off the machine, or could be made to.
- Storing prompt text, source code, diffs, full commands, tool output or credentials (RATION promises not to).
- A RATION hook blocking or breaking a Claude Code session, or being abusable to change agent behaviour beyond its documented, opt-in guard mode.
- Modifying Claude Code / Codex settings beyond the documented hook, status line and backup behaviour (for example permissions, auth or sandbox settings).
- Command injection via configuration, acceptance checks or chained status line commands that goes beyond what the user configured.

## Design guarantees

- No network code and no telemetry.
- Hooks fail open; internal errors are written to `~/.config/ration/hook-errors.log`.
- `ration init` backs up `~/.claude/settings.json` before writing and refuses to touch it if it isn't valid JSON.
- RATION never passes `bypassPermissions` / `--dangerously-*` flags and never reads credential files.
