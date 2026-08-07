"""Environment, configuration, and subprocess helpers."""

import os
import re
import subprocess
import sys

FENCE_RE = re.compile(r"^```[a-zA-Z]*$")


def load_dotenv():
    """Load the .env file from the project root (the package's parent)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


def env_str(name, fallback):
    """String setting from environment (or .env), with fallback."""
    value = os.environ.get(name)
    return value if value else fallback


def env_int(name, fallback):
    """Integer setting from environment (or .env), with fallback."""
    value = os.environ.get(name)
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError:
        print(f"ERROR: {name} must be an integer, got {value!r}", file=sys.stderr)
        sys.exit(1)


def run(cmd, desc, capture=False, env=None, stdin_text=None, fatal=True):
    """Run a subprocess command. Non-fatal failures warn and return None."""
    shown = [a if len(a) <= 80 else a[:77] + "..." for a in cmd]
    print(f"  $ {' '.join(shown)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=True,
            env=env,
            input=stdin_text,
        )
        return result
    except FileNotFoundError:
        if fatal:
            print(f"  ERROR: '{cmd[0]}' not found. Is it installed?", file=sys.stderr)
            sys.exit(1)
        print(f"  WARNING: '{cmd[0]}' not found; skipping {desc}")
        return None
    except subprocess.CalledProcessError as e:
        if fatal:
            print(f"  ERROR: {desc} failed (exit {e.returncode})", file=sys.stderr)
            if e.stderr:
                print(e.stderr, file=sys.stderr)
            sys.exit(1)
        print(f"  WARNING: {desc} failed (exit {e.returncode}); continuing without it")
        return None


def parse_time(t):
    """Parse time string (seconds, MM:SS, or HH:MM:SS) to float seconds."""
    try:
        return float(t)
    except ValueError:
        pass
    parts = t.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    raise ValueError(f"Cannot parse time: {t}")
