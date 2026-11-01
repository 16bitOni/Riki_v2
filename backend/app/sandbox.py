"""run_python: run a short Python program in a throwaway Docker container.

No network, a read-only disk, 512 MB, one CPU, 60 seconds. Your uploaded files are mounted read-only at /work, and the
program is /work/main.py. Only text comes back (stdout and stderr). Because it can touch nothing outside the container,
it is a safe tool. Off unless RIKI_SANDBOX=true and Docker is installed; RIKI_SANDBOX_IMAGE picks the image (the default has
no pandas: build one with the packages you want and name it there).
"""

import asyncio
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from langchain_core.tools import tool

from .agent.policy import SAFE
from .agent.tools import ToolEntry
from .db import Artifact

log = logging.getLogger(__name__)
TIMEOUT_S = 60
OUTPUT_CHARS = 8000


def docker_command(name: str, image: str, workdir: Path) -> list[str]:
    return ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only", "--memory", "512m", "--cpus", "1",
            "--pids-limit", "128", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp:size=64m",
            "-v", f"{workdir}:/work:ro", "-w", "/work", image, "python", "-I", "/work/main.py"]


def safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:80] or "file"


def sandbox_tools(rt) -> list[ToolEntry]:
    s = rt.settings
    if not s.sandbox:
        return []
    if shutil.which("docker") is None:
        log.warning("RIKI_SANDBOX is on but docker was not found; run_python is not available")
        return []

    @tool
    async def run_python(code: str, file_ids: list[str] | None = None) -> str:
        """Run Python 3 in an isolated container: no network, 60 s, 512 MB. Print what you want to see; only stdout and
        stderr come back. Pass the ids of files the user attached in `file_ids`: they appear in the working directory under
        their original names (the reply header lists them), as text. Use it for exact arithmetic, parsing a CSV or table,
        or checking a calculation, instead of doing sums in your head."""
        with tempfile.TemporaryDirectory(prefix="riki-sandbox-") as tmp:
            work = Path(tmp)
            (work / "main.py").write_text(code, encoding="utf-8")
            listed = []
            for fid in (file_ids or [])[:5]:
                async with rt.db.session() as db:
                    a = await db.get(Artifact, fid) if len(fid) == 32 else None
                if a is not None and a.kind == "upload":
                    fname = safe_filename(a.title)
                    (work / fname).write_text(a.content, encoding="utf-8")
                    listed.append(fname)
            name = f"riki-sandbox-{uuid.uuid4().hex[:8]}"
            proc = await asyncio.create_subprocess_exec(*docker_command(name, s.sandbox_image, work),
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
            except TimeoutError:
                await (await asyncio.create_subprocess_exec("docker", "kill", name, stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL)).wait()
                return f"Stopped: the program ran longer than {TIMEOUT_S} s."
        text = out.decode("utf-8", "replace") + (("\n[stderr]\n" + err.decode("utf-8", "replace")) if err else "")
        head = f"[exit {proc.returncode}" + (f" · files: {', '.join(listed)}" if listed else "") + "]\n"
        return head + text[:OUTPUT_CHARS] + ("\n[output cut]" if len(text) > OUTPUT_CHARS else "")

    return [ToolEntry(run_python, SAFE)]
