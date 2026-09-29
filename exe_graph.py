"""LangGraph workflow: scan -> install deps -> generate .spec -> build -> smoke test
-> (debug -> re-run failed stage ...) -> finalize."""
import os
from typing import List, TypedDict

from langgraph.graph import END, START, StateGraph

import exe_tools as ex
from fs_tools import Workspace
from graph import ENTRY_CANDIDATES, Plan, RetryLLM

SPEC_TEMPLATE = """# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['{entry}'],
    pathex=[],
    binaries=[],
    datas=[],            # (source, dest) tuples for non-code files read at runtime
    hiddenimports=[],    # add any import PyInstaller's static analysis misses
    hookspath=[],
    hooksconfig={{}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    [],
    name='{name}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console={console},
    icon={icon!r},
)
"""

SYSTEM = """You package Python projects into a single-file Windows .exe with PyInstaller.
Rules:
- Output exactly ONE file: the spec file, with the exact name given.
- Keep the ONEFILE structure from the template (one EXE block, no COLLECT/onedir step).
- Add every third-party import PyInstaller's static analysis could miss to hiddenimports —
  this is the #1 cause of "ModuleNotFoundError" when the exe actually runs (common offenders:
  pydantic, uvicorn workers/loops, click, jinja2, engineio/socketio, pkg_resources plugins).
- If the project reads template/static/config files (non-.py) at runtime, add them to datas
  as (absolute_or_relative_source, dest_dir_in_bundle) tuples.
- Always return the COMPLETE spec file content, never a diff or placeholder.
- Do NOT repeat a fix that already failed (see history)."""


class State(TypedDict, total=False):
    context: str
    attempt: int
    stage: str            # "install" | "build" | "smoke"
    ok: bool
    log: str
    history: List[str]
    unfixable: bool
    exe_path: str
    run_cmd: str
    result: str


def _pick_entry(ws: Workspace) -> str:
    files = ws.files()
    for c in ENTRY_CANDIDATES:
        if c in files and c.endswith(".py"):
            return c
    py = [f for f in files if f.endswith(".py") and "/" not in f]
    return py[0] if py else "main.py"


def build_graph(ws: Workspace, name: str, entry: str | None, windowed: bool, icon: str | None,
                max_attempts: int, model: str, fallback_model: str | None = None):
    llm = RetryLLM(model, fallback_model or os.getenv("AGENT_FALLBACK_MODEL"))
    entry_script = entry or _pick_entry(ws)
    spec_name = f"{name}.spec"
    console = "False" if windowed else "True"

    def log(msg):
        print(msg, flush=True)

    def gather_context() -> str:
        files = ws.files()
        chunks = ["FILE TREE:\n" + "\n".join(files)]
        wanted = [f for f in files if f.lower().startswith(("requirements", "pyproject", "setup.py"))]
        wanted += [entry_script] if entry_script in files else []
        wanted += [f for f in files if f.endswith(".py") and f not in wanted][:10]
        for f in dict.fromkeys(wanted):
            chunks.append(f"--- {f} ---\n{ws.read(f, 5000)}")
        return "\n\n".join(chunks)[:40000]

    def apply(plan: Plan) -> list[str]:
        changed = []
        for fe in plan.files:
            ws.write(fe.path, fe.content)
            changed.append(fe.path)
        return changed

    # ---- nodes ----
    def scan(state: State):
        log(f"🔍 Scanning project (entry point: {entry_script})...")
        return {"context": gather_context(), "attempt": 0, "history": []}

    def install(state: State):
        log(f"📦 Installing dependencies into {os.path.basename(os.sys.executable)}'s env...")
        ok1, out1 = ex.ensure_pyinstaller()
        ok2, out2 = ex.pip_install_requirements(ws.root)
        ok = ok1 and ok2
        log("   ✅ deps ready" if ok else "   ❌ install failed")
        return {"stage": "install", "ok": ok, "log": (out1 + "\n" + out2)[-6000:]}

    def generate(state: State):
        log("🛠  Generating PyInstaller spec...")
        template = SPEC_TEMPLATE.format(entry=entry_script, name=name, console=console, icon=icon)
        plan = llm.invoke([
            ("system", SYSTEM),
            ("human",
             f"Write '{spec_name}' for this project. Start from this template and fill in "
             f"hiddenimports/datas based on the code — keep the same overall structure:\n\n{template}\n\n"
             f"PROJECT:\n{state['context']}"),
        ])
        changed = apply(plan)
        if spec_name not in changed:  # safety net: force-write the template if the model drifted
            ws.write(spec_name, template)
        log(f"   wrote: {spec_name}")
        return {}

    def build(state: State):
        log(f"🧱 pyinstaller build (attempt {state['attempt'] + 1})...")
        ok, out = ex.build(ws.root, spec_name)
        log("   ✅ build ok" if ok else "   ❌ build failed")
        return {"stage": "build", "ok": ok, "log": out}

    def smoke(state: State):
        log("🚦 Smoke-testing the binary...")
        exe = ex.output_path(ws.root, name)
        ok, out = ex.smoke_test(exe)
        log("   ✅ it runs" if ok else "   ❌ it crashed on launch")
        return {"stage": "smoke", "ok": ok, "log": out, "exe_path": str(exe)}

    def debug(state: State):
        n = state["attempt"] + 1
        log(f"🩺 Debugging ({state['stage']} failure), fix attempt {n}/{max_attempts}...")
        current = ws.read(spec_name, 6000) if ws.exists(spec_name) else "(no spec file yet)"
        plan = llm.invoke([
            ("system", SYSTEM),
            ("human",
             f"The {state['stage'].upper()} step failed.\n\n"
             f"PROJECT CONTEXT:\n{gather_context()}\n\n"
             f"CURRENT SPEC FILE ({spec_name}):\n{current}\n\n"
             f"ERROR LOG (tail):\n{state['log']}\n\n"
             f"PREVIOUS ATTEMPTS:\n" + ("\n".join(state["history"]) or "none") + "\n\n"
             "Diagnose the root cause. If it's a missing package at install time, edit "
             "requirements.txt. If it's a missing hidden import or data file discovered at build/run "
             f"time, edit {spec_name}. Return the corrected complete file(s).")
        ])
        changed = apply(plan) if not plan.unfixable else []
        log(f"   {plan.reasoning}\n   changed: {changed or 'nothing'}")
        return {"attempt": n, "unfixable": plan.unfixable,
                "history": state["history"] + [f"#{n} [{state['stage']}] {plan.reasoning} | files: {changed}"]}

    def finalize(state: State):
        exe = ex.output_path(ws.root, name)
        cmd = ex.run_instructions(exe)
        warn = windowed or (state.get("stage") == "smoke" and not state.get("ok"))
        log("\n" + "=" * 60)
        if windowed:
            log("ℹ️  --windowed: skipped the automated launch test (GUI apps don't exit on their own).")
        elif warn:
            log("⚠️  Built, but it exited abnormally on launch:\n" + state["log"][-800:])
        log(f"✅ EXE READY: {exe}\n\n▶ {cmd}\n")
        log("Originals of any modified files are in .dockerize_backup/")
        return {"run_cmd": cmd, "exe_path": str(exe),
                "result": "success_with_warning" if (warn and not windowed) else "success"}

    def fail(state: State):
        log("\n" + "=" * 60 + f"\n❌ Gave up after {state['attempt']} fix attempts.\nLast error:\n{state['log'][-1500:]}")
        return {"result": "failed"}

    # ---- routing ----
    def after_install(state: State):
        return "generate" if state["ok"] else ("debug" if state["attempt"] < max_attempts else "fail")

    def after_build(state: State):
        if not state["ok"]:
            return "debug" if state["attempt"] < max_attempts else "fail"
        return "finalize" if windowed else "smoke"

    def after_smoke(state: State):
        if state["ok"]:
            return "finalize"
        return "debug" if state["attempt"] < max_attempts else "fail"

    def after_debug(state: State):
        if state.get("unfixable"):
            return "finalize" if state["stage"] == "smoke" else "fail"
        return "install" if state["stage"] == "install" else "build"

    g = StateGraph(State)
    for n, fn in [("scan", scan), ("install", install), ("generate", generate), ("build", build),
                  ("smoke", smoke), ("debug", debug), ("finalize", finalize), ("fail", fail)]:
        g.add_node(n, fn)
    g.add_edge(START, "scan")
    g.add_edge("scan", "install")
    g.add_conditional_edges("install", after_install, ["generate", "debug", "fail"])
    g.add_edge("generate", "build")
    g.add_conditional_edges("build", after_build, ["smoke", "finalize", "debug", "fail"])
    g.add_conditional_edges("smoke", after_smoke, ["finalize", "debug", "fail"])
    g.add_conditional_edges("debug", after_debug, ["install", "build", "finalize", "fail"])
    g.add_edge("finalize", END)
    g.add_edge("fail", END)
    return g.compile()
