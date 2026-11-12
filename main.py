import asyncio
import fcntl
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    os.chdir(ROOT)
    (ROOT / "data").mkdir(exist_ok=True)

    lock = os.open(ROOT / "data" / "bot.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("LeadHunter is already running.")
        sys.exit(0)

    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)

    from leadhunter.main import run

    asyncio.run(run())


if __name__ == "__main__":
    main()
