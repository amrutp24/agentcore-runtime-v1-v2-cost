#!/usr/bin/env python3
"""Build build/agent.zip (default) or build/agent_<name>.zip (`package_agent.py echo|alloc`) for direct code deploy.

AgentCore Runtime is linux/arm64 only, so dependencies are fetched as arm64
wheels. Works on Windows without a zip binary: uses uv (a pip-installable dependency in
requirements.txt) and zipfile, and sets 644 file permissions as the docs require.
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
# name -> (source dir, staging dir, zip). The default entry is the original layout.
TARGETS = {
    "agent": (ROOT / "agent", BUILD / "pkg", BUILD / "agent.zip"),
    "echo": (ROOT / "agent_echo", BUILD / "pkg_echo", BUILD / "agent_echo.zip"),
    "alloc": (ROOT / "agent_alloc", BUILD / "pkg_alloc", BUILD / "agent_alloc.zip"),
}
PY_VERSION = "3.13"  # must match runtime = "PYTHON_3_13" in infra/runtime.tf
MAX_ZIPPED = 250 * 1024 * 1024


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "agent"
    if name not in TARGETS:
        sys.exit(f"unknown target {name}; use one of {list(TARGETS)}")
    AGENT, PKG, ZIP = TARGETS[name]
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)

    # uv (not plain pip): pip evaluates environment markers on the Windows host and
    # tries to resolve pywin32 for the arm64 target. uv handles --python-platform.
    cmd = [
        sys.executable, "-m", "uv", "pip", "install",
        "--python-platform", "aarch64-manylinux2014",
        "--python-version", PY_VERSION,
        "--target", str(PKG),
        "--only-binary=:all:",
        "-r", str(AGENT / "requirements.txt"),
    ]
    subprocess.run(cmd, check=True)
    shutil.copy2(AGENT / "main.py", PKG / "main.py")

    if ZIP.exists():
        ZIP.unlink()
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(PKG.rglob("*")):
            if f.is_dir() or "__pycache__" in f.parts:
                continue
            info = zipfile.ZipInfo.from_file(f, f.relative_to(PKG).as_posix())
            info.external_attr = (0o100644 << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, f.read_bytes())

    size = ZIP.stat().st_size
    print(f"wrote {ZIP} ({size / 1e6:.1f} MB zipped)")
    if size > MAX_ZIPPED:
        sys.exit("zip exceeds the 250 MB direct-deploy limit")


if __name__ == "__main__":
    main()
