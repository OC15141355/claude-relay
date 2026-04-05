#!/bin/bash
# Deploy claude-relay to a remote host
# Usage: ./deploy.sh [user@host]
#
# Defaults to CLAUDE_RELAY_HOST env var, or prompts if not set.

HOST="${1:-${CLAUDE_RELAY_HOST:-}}"
if [ -z "$HOST" ]; then
    echo "Usage: ./deploy.sh user@host"
    echo "Or set CLAUDE_RELAY_HOST=user@host"
    exit 1
fi

REMOTE_DIR="~/claude-relay"
LOCAL="$(dirname "$0")/claude-relay.py"

echo "Checking syntax..."
python3 -c "import ast; ast.parse(open('$LOCAL').read())" || { echo "Syntax error — aborting"; exit 1; }

echo "Deploying to $HOST..."
ssh "$HOST" "mkdir -p $REMOTE_DIR"
scp "$LOCAL" "$HOST:$REMOTE_DIR/claude-relay.py" || { echo "scp failed"; exit 1; }
ssh "$HOST" "chmod +x $REMOTE_DIR/claude-relay.py"

echo "Checking tmux..."
ssh "$HOST" "source ~/.zshrc 2>/dev/null; which tmux" || {
    echo "tmux not found — install it on the remote host"
    exit 1
}

echo "Testing..."
ssh "$HOST" "source ~/.zshrc 2>/dev/null; python3 $REMOTE_DIR/claude-relay.py status"

echo "Done."
