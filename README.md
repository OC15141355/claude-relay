# claude-relay

Lightweight tmux bridge to Claude Code. Attaches to an existing Claude Code session inside tmux and exposes a simple CLI for sending prompts and receiving plain-text responses.

Built to be called over SSH from [macbridge](https://github.com/OC15141355/mac-plus) on a Pi Zero 2W, feeding an 80x24 VT100 terminal (Mac Plus + ZTerm) at 9600 baud.

```
Mac Plus ──serial──> Pi Zero 2W ──SSH──> core-01 ──tmux──> Claude Code
         9600 baud   (macbridge)          (claude-relay)     (OAuth)
```

## How it works

You start Claude Code yourself in a named tmux session on core-01. This keeps your Keychain unlocked and OAuth tokens active — no API keys needed. The relay script just drives that session: sends prompts via `tmux paste-buffer`, polls for the idle prompt, extracts the response, and strips markdown for terminal rendering.

1. Sends prompts via `tmux paste-buffer` (safe for any input size)
2. Polls `tmux capture-pane` for the idle prompt (`❯`)
3. Extracts the last response from full scrollback
4. Strips markdown and wraps to 76 columns for VT100 rendering

## Requirements

- Python 3.9+ (stdlib only, zero dependencies)
- tmux
- Claude Code (authenticated)

## Setup

On core-01:

```bash
# Clone the repo
git clone git@github.com:OC15141355/claude-relay.git ~/claude-relay

# Start a tmux session and launch Claude Code
tmux new -s claude-relay
claude

# Detach with Ctrl-b d — session stays alive
```

That's it. The session persists across SSH disconnects. Reattach anytime with `tmux attach -t claude-relay`.

## Usage

```bash
# Ask a question — response is plain text, markdown stripped, wrapped to 76 cols
claude-relay ask "what pods are unhealthy?"

# Raw output (no formatting)
claude-relay ask "explain this error" --raw

# Custom timeout
claude-relay ask "analyze this codebase" --timeout 600

# Check status
claude-relay status
```

### From macbridge (over SSH)

```bash
ssh core-01 "python3 ~/claude-relay/claude-relay.py ask 'what pods are unhealthy?'"
```

## Deploy

```bash
./deploy.sh   # scp to core-01, verify tmux is installed
```

## License

MIT
