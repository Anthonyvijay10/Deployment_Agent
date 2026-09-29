"""LangGraph workflow: scan -> generate -> build -> smoke test -> (debug -> build ...) -> finalize."""
import os
import time
from typing import List, TypedDict

from langchain.chat_models import init_chat_model
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

import docker_tools as dk
from fs_tools import Workspace

ENTRY_CANDIDATES = ["main.py", "app.py", "run.py", "server.py", "manage.py", "wsgi.py", "index.js"]


class FileEdit(BaseModel):
    path: str = Field(description="Path relative to the project root")
    content: str = Field(description="COMPLETE new file content")


class Plan(BaseModel):
    reasoning: str = Field(description="Short root-cause explanation / what you changed")
    files: List[FileEdit] = Field(default_factory=list)
    unfixable: bool = Field(default=False, description=(
        "True ONLY if the failure cannot be fixed by editing project files "
        "(e.g. needs real secrets, an external database, or Docker itself is broken)."))


TRANSIENT = ("503", "unavailable", "429", "resource_exhausted", "overloaded", "internal", "timeout", "deadline")


class RetryLLM:
    """Retries transient API errors (503/429...) with backoff, then tries an optional fallback model."""

    def __init__(self, model: str, fallback: str | None = None, retries: int = 6):
        names = [model] + ([fallback] if fallback and fallback != model else [])
        self.models = [(n, init_chat_model(n).with_structured_output(Plan)) for n in names]
        self.retries = retries

    def invoke(self, messages):
        last = None
        for name, m in self.models:
            for i in range(self.retries):
                try:
                    return m.invoke(messages)
                except Exception as e:  # noqa: BLE001
                    if not any(t in str(e).lower() for t in TRANSIENT):
                        raise
                    last = e
                    wait = min(2 ** (i + 1), 60)
                    print(f"   ⏳ {name} busy, retry {i + 1}/{self.retries} in {wait}s", flush=True)
                    time.sleep(wait)
            print(f"   ↪ giving up on {name}", flush=True)
        raise last


class State(TypedDict, total=False):
    context: str
    attempt: int
    stage: str          # "build" | "smoke"
    ok: bool
    log: str
    history: List[str]
    unfixable: bool
    run_cmd: str
    result: str         # "success" | "success_with_warning" | "failed"


SYSTEM = """You are a senior DevOps engineer who containerises Python projects.
Rules:
- Use a slim official base image matching the project's Python needs.
- Copy requirements.txt first and pip install with --no-cache-dir (layer caching).
- Add EXPOSE if the app listens on a port; bind servers to 0.0.0.0.
- Always output COMPLETE file contents, never diffs or placeholders.
- Make the smallest change that fixes the actual root cause.
- Do NOT repeat a fix that already failed (see history)."""


def build_graph(ws: Workspace, image: str, env_file: str | None, max_attempts: int, model: str,
                fallback_model: str | None = None):
    llm = RetryLLM(model, fallback_model or os.getenv("AGENT_FALLBACK_MODEL"))

    def log(msg):
        print(msg, flush=True)

    def gather_context() -> str:
        files = ws.files()
        chunks = ["FILE TREE:\n" + "\n".join(files)]
        wanted = [f for f in files if f.lower().startswith(("requirements", "pyproject", "setup.py", "dockerfile", ".dockerignore", ".env.example"))]
        wanted += [f for f in files if f in ENTRY_CANDIDATES or f.endswith("/main.py")]
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
        log("🔍 Scanning project...")
        return {"context": gather_context(), "attempt": 0, "history": []}

    def generate(state: State):
        log("🛠  Generating Dockerfile...")
        plan = llm.invoke([
            ("system", SYSTEM),
            ("human", f"Create a Dockerfile and .dockerignore for this project.\n\n{state['context']}"),
        ])
        changed = apply(plan)
        log(f"   wrote: {', '.join(changed)}")
        return {}

    def build(state: State):
        log(f"🐳 docker build (attempt {state['attempt'] + 1})...")
        ok, out = dk.build(ws.root, image)
        log("   ✅ build ok" if ok else "   ❌ build failed")
        return {"stage": "build", "ok": ok, "log": out}

    def smoke(state: State):
        log("🚦 Smoke-testing container...")
        ok, out = dk.smoke_test(image, env_file)
        log("   ✅ container runs" if ok else "   ❌ container crashed")
        return {"stage": "smoke", "ok": ok, "log": out}

    def debug(state: State):
        n = state["attempt"] + 1
        log(f"🩺 Debugging ({state['stage']} failure), fix attempt {n}/{max_attempts}...")
        current = "\n\n".join(f"--- {f} ---\n{ws.read(f, 6000)}"
                              for f in ws.files() if f.lower().startswith(("dockerfile", "requirements", ".dockerignore")))
        plan = llm.invoke([
            ("system", SYSTEM),
            ("human",
             f"The {state['stage'].upper()} step failed.\n\n"
             f"PROJECT CONTEXT:\n{gather_context()}\n\n"
             f"CURRENT DOCKER FILES:\n{current}\n\n"
             f"ERROR LOG (tail):\n{state['log']}\n\n"
             f"PREVIOUS ATTEMPTS:\n" + ("\n".join(state["history"]) or "none") + "\n\n"
             "Diagnose the root cause and return the corrected files. Edit source code only if the "
             "error is a genuine code bug; prefer fixing Dockerfile/requirements.txt.")
        ])
        changed = apply(plan) if not plan.unfixable else []
        log(f"   {plan.reasoning}\n   changed: {changed or 'nothing'}")
        return {"attempt": n, "unfixable": plan.unfixable,
                "history": state["history"] + [f"#{n} [{state['stage']}] {plan.reasoning} | files: {changed}"]}

    def finalize(state: State):
        cmd = dk.run_command(ws.read("Dockerfile"), image, env_file)
        warn = state.get("stage") == "smoke" and not state.get("ok")
        log("\n" + "=" * 60)
        if warn:
            log("⚠️  Image built, but the container exits at startup — likely needs "
                "secrets/external services:\n" + state["log"][-800:])
        log(f"✅ IMAGE READY: {image}\n\n▶ Run it with:\n\n    {cmd}\n")
        log("Originals of any modified files are in .dockerize_backup/")
        return {"run_cmd": cmd, "result": "success_with_warning" if warn else "success"}

    def fail(state: State):
        log("\n" + "=" * 60 + f"\n❌ Gave up after {state['attempt']} fix attempts.\nLast error:\n{state['log'][-1500:]}")
        return {"result": "failed"}

    # ---- routing ----
    def after_check(state: State):
        if state["ok"]:
            return "smoke" if state["stage"] == "build" else "finalize"
        return "debug" if state["attempt"] < max_attempts else "fail"

    def after_debug(state: State):
        if state.get("unfixable"):
            return "finalize" if state["stage"] == "smoke" else "fail"
        return "build"

    g = StateGraph(State)
    for name, fn in [("scan", scan), ("generate", generate), ("build", build), ("smoke", smoke),
                     ("debug", debug), ("finalize", finalize), ("fail", fail)]:
        g.add_node(name, fn)
    g.add_edge(START, "scan")
    g.add_edge("scan", "generate")
    g.add_edge("generate", "build")
    g.add_conditional_edges("build", after_check, ["smoke", "debug", "fail"])
    g.add_conditional_edges("smoke", after_check, ["finalize", "debug", "fail"])
    g.add_conditional_edges("debug", after_debug, ["build", "finalize", "fail"])
    g.add_edge("finalize", END)
    g.add_edge("fail", END)
    return g.compile()
