"""Explicit one-time download/setup; ordinary retrieval never permits network."""
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    runtime = ROOT / ".blog-agent-data"
    environment = runtime / "embedding-venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements-embedding.txt")], check=True)
    subprocess.run([str(python), str(ROOT / "code/blog_assistant/embedding_worker.py"),
                    "--cache", str(runtime / "embedding-models"), "--download"], check=True)
    print("Local embedding ready. Run python code/index_blog.py to prepare your selected articles.")


if __name__ == "__main__":
    main()
