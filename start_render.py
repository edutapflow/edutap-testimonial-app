"""Start Streamlit on Render's assigned port. Does not read credential files."""
import os
from pathlib import Path
import sys


def build_command(port_text: str) -> list[str]:
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError("PORT must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be an integer between 1 and 65535")
    root = Path(__file__).resolve().parent
    return [
        sys.executable, "-m", "streamlit", "run", str(root / "app.py"),
        "--server.address=0.0.0.0", f"--server.port={port}",
        "--server.headless=true", "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
    ]


if __name__ == "__main__":
    try:
        command = build_command(os.getenv("PORT", "10000"))
    except ValueError as error:
        raise SystemExit(str(error))
    os.chdir(Path(__file__).resolve().parent)
    os.execv(sys.executable, command)
