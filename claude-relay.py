#!/usr/bin/env python3
"""claude-relay — lightweight tmux bridge to Claude Code.

Attaches to an existing Claude Code tmux session and provides a simple
CLI for sending prompts and receiving plain-text responses. You start
the session yourself (keeping Keychain/OAuth alive), the relay just
drives it.

Designed to be called over SSH from macbridge on a Pi Zero 2W, feeding
an 80x24 VT100 terminal at 9600 baud.

Zero dependencies beyond Python 3.9+ stdlib and tmux.

Setup:
    # On core-01, start the session yourself:
    tmux new -s claude-relay
    claude
    # Detach with Ctrl-b d — session stays alive

Usage:
    claude-relay ask "what pods are unhealthy?"
    claude-relay ask "explain this error" --raw
    claude-relay status
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SESSION_NAME = "claude-relay"
RESPONSE_TIMEOUT = 300   # seconds to wait for a response
POLL_INTERVAL = 0.5      # seconds between idle checks
STABLE_THRESHOLD = 3     # consecutive stable polls before we accept response
OUTPUT_WIDTH = 76         # wrap width for Mac Plus (80 cols minus margin)

# Idle detection patterns (Claude Code TUI)
IDLE_MARKER = "\u276f"            # ❯ prompt character
BUSY_MARKERS = ["\u280b", "\u280d", "\u2839", "\u2838", "\u2834", "\u2826",
                "\u2807", "\u280f", "Thinking", "Running"]

# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------


def _run(cmd: list, timeout: int = 30) -> str:
    """Run a command and return stdout, or empty string on failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def _send_keys(keys: list) -> None:
    """Send keys to the tmux session."""
    subprocess.run(
        ["tmux", "send-keys", "-t", SESSION_NAME] + keys,
        capture_output=True, text=True,
    )


def _capture_pane(full_history: bool = False) -> str:
    """Capture the tmux pane content, optionally with full scrollback."""
    cmd = ["tmux", "capture-pane", "-p", "-t", SESSION_NAME]
    if full_history:
        cmd.extend(["-S", "-", "-E", "-"])
    return _run(cmd, timeout=10)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences."""
    return re.sub(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])", "", text)


def _has_session() -> bool:
    """Check if the tmux session exists."""
    p = subprocess.run(
        ["tmux", "has-session", "-t", SESSION_NAME],
        capture_output=True, text=True,
    )
    return p.returncode == 0


def _is_idle(screen: str) -> bool:
    """Check if Claude Code is at an idle prompt.

    Only checks the last few lines for busy markers, since words like
    "Running" can appear in response content.
    """
    has_prompt = IDLE_MARKER in screen
    # Check only the bottom 5 lines for spinner/busy indicators
    tail = "\n".join(screen.split("\n")[-5:])
    is_busy = any(m in tail for m in BUSY_MARKERS)
    return has_prompt and not is_busy


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


def _wait_for_idle(timeout: int) -> bool:
    """Poll the tmux pane until Claude Code is idle or timeout."""
    start = time.time()
    last_screen = ""
    stable = 0

    while time.time() - start < timeout:
        raw = _capture_pane()
        screen = _strip_ansi(raw)

        if _is_idle(screen):
            if screen == last_screen:
                stable += 1
                if stable >= STABLE_THRESHOLD:
                    return True
            else:
                stable = 0
                last_screen = screen
        else:
            stable = 0

        time.sleep(POLL_INTERVAL)

    elapsed = int(time.time() - start)
    print(f"warning: timeout after {elapsed}s", file=sys.stderr)
    return False


def session_status() -> None:
    """Print session status."""
    if not _has_session():
        print("no session — start one with: tmux new -s claude-relay")
        print("then run: claude")
        return

    raw = _capture_pane()
    screen = _strip_ansi(raw)
    idle = _is_idle(screen)
    print(f"running ({'idle' if idle else 'busy'})")


# ---------------------------------------------------------------------------
# Ask
# ---------------------------------------------------------------------------


def ask(prompt: str, raw_output: bool = False, timeout: int = 0) -> str:
    """Send a prompt to Claude Code and return the response.

    Args:
        prompt: The question or command to send.
        raw_output: If True, return unformatted response. Otherwise,
                    strip markdown and wrap to OUTPUT_WIDTH.
        timeout: Response timeout in seconds. 0 = use RESPONSE_TIMEOUT default.

    Returns:
        The response text, or an error message prefixed with "error:".
    """
    resp_timeout = timeout if timeout > 0 else RESPONSE_TIMEOUT

    if not _has_session():
        return "error: no claude-relay session. start one on core-01 first."

    # Check Claude is idle before we send anything
    raw = _capture_pane()
    screen = _strip_ansi(raw)
    if not _is_idle(screen):
        return "error: claude is busy — try again in a moment"

    # Clear screen for clean capture
    _send_keys(["C-l"])
    time.sleep(0.3)

    # Write prompt to a temp file and paste via tmux buffer
    # (avoids shell escaping issues with large or complex prompts)
    fd, tmp = tempfile.mkstemp(prefix="claude-relay-", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(prompt)
        _run(["tmux", "load-buffer", "-b", "relay-buf", tmp])
        _run(["tmux", "paste-buffer", "-b", "relay-buf", "-p", "-t", SESSION_NAME])
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    time.sleep(0.3)

    # Submit
    _send_keys(["Enter"])

    # Wait for response
    if not _wait_for_idle(resp_timeout):
        pass  # still try to capture whatever we got

    # Extract response from full scrollback
    raw_scrollback = _capture_pane(full_history=True)
    clean = _strip_ansi(raw_scrollback)
    response = _extract_response(clean)

    if raw_output:
        return response

    return _format_for_terminal(response)


def _extract_response(scrollback: str) -> str:
    """Extract the last Claude response from the scrollback.

    Claude Code format:
        ❯ <user prompt>
        ● <response text...>
           <continued...>
        ❯ (next idle prompt)

    Also handles ⎿ (continuation/result marker).
    """
    lines = scrollback.split("\n")

    # Find all response markers
    # Claude Code uses ● (U+25CF) or ⏺ (U+23FA) depending on version
    marker_positions = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(stripped.startswith(m) for m in ("\u25cf ", "\u25cf\u200a",
                                                 "\u23fa ", "\u23fa\u200a")):
            marker_positions.append(i)

    if not marker_positions:
        # Fallback: find content between the last two ❯ prompts
        prompt_positions = [i for i, l in enumerate(lines) if IDLE_MARKER in l]
        if len(prompt_positions) >= 2:
            start = prompt_positions[-2] + 1
            end = prompt_positions[-1]
            content = lines[start:end]
            return "\n".join(l for l in content if l.strip()).strip()
        return ""

    # Take from the last ● marker to the next ❯ or end
    start = marker_positions[-1]
    result = []

    for i in range(start, len(lines)):
        line = lines[i]
        stripped = line.strip()

        # Stop at the idle prompt
        if i > start and IDLE_MARKER in line:
            break

        # Stop at UI chrome (status bar, borders, ASCII art)
        if any(b in line for b in ("\u2500\u2500", "\u2580\u2580", "\u2584\u2584",
                                    ".---.", "(\u00b0>\u00b0)", "\u2501\u2501")):
            break

        # Strip the response marker from the first line
        if i == start:
            for prefix in ("\u25cf\u200a", "\u25cf ", "\u23fa\u200a", "\u23fa "):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix):]
                    break
            result.append(stripped)
        else:
            result.append(line.rstrip())

    return "\n".join(result).strip()


# ---------------------------------------------------------------------------
# Output formatting (for 80x24 VT100 at 9600 baud)
# ---------------------------------------------------------------------------

# Markdown patterns to strip
_MD_PATTERNS = [
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),              # headings → plain
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),                       # bold
    (re.compile(r"\*(.+?)\*"), r"\1"),                           # italic
    (re.compile(r"`{3}[\w]*\n?", re.MULTILINE), ""),            # code fences
    (re.compile(r"`(.+?)`"), r"\1"),                             # inline code
    (re.compile(r"^\s*[-*]\s+", re.MULTILINE), "- "),            # normalise bullets
    (re.compile(r"^\s*\d+\.\s+", re.MULTILINE), "- "),          # numbered → bullets
    (re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),               # links → text
]


def _format_for_terminal(text: str) -> str:
    """Strip markdown and wrap text for an 80-column terminal."""
    for pattern, replacement in _MD_PATTERNS:
        text = pattern.sub(replacement, text)

    # Collapse triple+ newlines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Wrap long lines
    wrapped_lines = []
    for line in text.split("\n"):
        if len(line) <= OUTPUT_WIDTH:
            wrapped_lines.append(line)
        elif line.startswith("- "):
            sub_lines = textwrap.wrap(line, width=OUTPUT_WIDTH,
                                      subsequent_indent="  ")
            wrapped_lines.extend(sub_lines)
        else:
            wrapped_lines.extend(textwrap.wrap(line, width=OUTPUT_WIDTH) or [""])

    return "\n".join(wrapped_lines).strip()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        prog="claude-relay",
        description="Lightweight tmux bridge to Claude Code",
    )
    sub = parser.add_subparsers(dest="command")

    # ask
    ask_p = sub.add_parser("ask", help="Send a prompt and get a response")
    ask_p.add_argument("prompt", help="The prompt to send")
    ask_p.add_argument("--raw", action="store_true",
                       help="Return unformatted response (no markdown stripping)")
    ask_p.add_argument("--timeout", type=int, default=RESPONSE_TIMEOUT,
                       help=f"Response timeout in seconds (default: {RESPONSE_TIMEOUT})")

    # status
    sub.add_parser("status", help="Check session status")

    args = parser.parse_args()

    if args.command == "ask":
        result = ask(args.prompt, raw_output=args.raw, timeout=args.timeout)
        print(result)
    elif args.command == "status":
        session_status()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
