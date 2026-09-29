"""
Usage:
  Docker (default):
    python main.py /path/to/project [--image myapp] [--max-attempts 8] [--env-file .env]

  Windows .exe (run this ON WINDOWS to get a real .exe — PyInstaller does not cross-compile):
    python main.py /path/to/project --exe [--name myapp] [--entry main.py] [--windowed] [--icon app.ico]

Shared flags: --model, --fallback-model, --max-attempts
"""
import argparse
import os
import re
import sys

import docker_tools as dk
from fs_tools import Workspace
from graph import build_graph as build_docker_graph

# Accept either variable name for the Gemini key
if os.getenv("GEMINI_API_KEY") and not os.getenv("GOOGLE_API_KEY"):
    os.environ["GOOGLE_API_KEY"] = os.environ["GEMINI_API_KEY"]


def main():
    ap = argparse.ArgumentParser(description="Agentic auto-packager (LangGraph + Gemini): Docker image or Windows .exe")
    ap.add_argument("project_dir")

    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--docker", action="store_true", help="build a Docker image (default)")
    mode.add_argument("--exe", action="store_true", help="build a Windows .exe with PyInstaller")

    ap.add_argument("--max-attempts", type=int, default=8)
    ap.add_argument("--model", default=os.getenv("AGENT_MODEL", "google_genai:gemini-2.5-flash"),
                    help='e.g. "google_genai:gemini-2.5-pro" for harder projects')
    ap.add_argument("--fallback-model", default=os.getenv("AGENT_FALLBACK_MODEL"),
                    help="backup model if the main one keeps failing (e.g. google_genai:gemini-2.5-flash)")

    # --docker-only options
    ap.add_argument("--image", help="[docker] image name (default: folder name)")
    ap.add_argument("--env-file", help="[docker] env file passed to the smoke test & run command")

    # --exe-only options
    ap.add_argument("--name", help="[exe] output name, e.g. myapp -> dist/myapp.exe (default: folder name)")
    ap.add_argument("--entry", help="[exe] entry .py script (default: auto-detected, e.g. main.py)")
    ap.add_argument("--windowed", action="store_true", help="[exe] GUI app: hide the console window, skip the launch test")
    ap.add_argument("--icon", help="[exe] path to a .ico file")

    a = ap.parse_args()

    def force_genai(m):
        """Force the Google AI Studio provider. Without this, langchain's init_chat_model can
        guess 'vertexai' for a bare 'gemini-...' name and then demand GCP project credentials
        instead of just using the AI Studio API key."""
        if m and ":" not in m:
            return f"google_genai:{m}"
        return m

    a.model = force_genai(a.model)
    a.fallback_model = force_genai(a.fallback_model)

    if not os.getenv("GOOGLE_API_KEY"):
        sys.exit("Set your key first: export GOOGLE_API_KEY=your_gemini_key")

    ws = Workspace(a.project_dir)
    safe_name = re.sub(r"[^a-z0-9_.-]", "-", ws.root.name.lower())

    if a.exe:
        import exe_tools as ex
        from exe_graph import build_graph as build_exe_graph
        if not ex.IS_WINDOWS:
            print("⚠️  You're not on Windows. PyInstaller cannot cross-compile, so this will build a "
                  f"native {sys.platform} binary, not a .exe. Run this same command on a Windows "
                  "machine to get a real .exe.\n", flush=True)
        name = a.name or safe_name
        graph = build_exe_graph(ws, name, a.entry, a.windowed, a.icon, a.max_attempts, a.model, a.fallback_model)
    else:
        if not dk.docker_available():
            sys.exit("Docker is not installed or the daemon isn't running.")
        image = a.image or safe_name
        graph = build_docker_graph(ws, image, a.env_file, a.max_attempts, a.model, a.fallback_model)

    final = graph.invoke({}, config={"recursion_limit": 6 + 4 * a.max_attempts + 10})
    sys.exit(0 if final.get("result", "").startswith("success") else 1)


if __name__ == "__main__":
    main()
