"""Windows job bootstrap. Wait for parent job assignment before starting a CLI."""
import json
import os
import subprocess
import sys


def main():
    # The parent sends this trusted launch header only after assigning this
    # process to its kill-on-close Job Object. No provider executes before it.
    header = sys.stdin.buffer.readline(65536)
    if not header.endswith(b"\n"):
        raise SystemExit(2)
    command = json.loads(header)
    data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if not isinstance(command, list) or not command or not all(isinstance(s, str) for s in command) or len(data) > 4 * 1024 * 1024:
        raise SystemExit(2)
    process = subprocess.run(command, input=data, shell=False,
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
