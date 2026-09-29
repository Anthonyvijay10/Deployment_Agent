# Deployment Agent

A LangGraph-based agent that takes a project folder and packages it — either into a
**Docker image** or a **Windows `.exe`** — and automatically debugs and retries until the
build actually works.

It uses Gemini (Google AI Studio) as the LLM that reads error logs and rewrites the
Dockerfile / spec file / requirements.txt when something fails.

---

## 1. How it works

### Docker pipeline (`graph.py`)

```
scan project → generate Dockerfile → docker build ──fail──▶ debug ──▶ rebuild
                                          │ ok
                                          ▼
                                     smoke test ──fail──▶ debug ──▶ rebuild
                                          │ ok
                                          ▼
                                      finalize → prints `docker run ...`
```

- **Scan:** reads your file tree, `requirements.txt`, and the main entry script.
- **Generate:** the LLM writes a `Dockerfile` and `.dockerignore`.
- **Build:** runs `docker build`.
- **Smoke test:** starts the container for ~10s. Passes if it's still running (a server)
  or exits with code 0 (a script). Catches runtime errors a successful build wouldn't show.
- **Debug loop:** on any failure, the LLM gets the error log + current files + history of
  previous fix attempts, and rewrites the broken file(s). It won't repeat a fix that
  already failed. Loops up to `--max-attempts` times.
- **Finalize:** prints the exact `docker run` command, with ports pulled from `EXPOSE`.

### Exe pipeline (`exe_graph.py`)

```
scan → install deps + pyinstaller ──fail──▶ debug ──▶ reinstall
   │ ok
   ▼
generate .spec → pyinstaller build ──fail──▶ debug ──▶ rebuild
   │ ok
   ▼
smoke test (launch the exe) ──fail──▶ debug ──▶ rebuild
   │ ok                        (skipped for --windowed GUI apps)
   ▼
finalize → prints path to dist/<name>.exe
```

Same debug-loop idea, but editing a PyInstaller `.spec` file (hidden imports, data
files) or `requirements.txt` instead of a Dockerfile.

> **⚠️ Important:** PyInstaller does **not** cross-compile. Run `--exe` **on Windows** to
> get a real `.exe`. Running it on Linux/Mac still works, but produces a native binary
> for that OS instead, with a warning printed.

---

## 2. Project files

| File | Purpose |
|---|---|
| `main.py` | CLI entry point — picks the Docker or exe pipeline |
| `graph.py` | Docker LangGraph pipeline + shared `RetryLLM` helper |
| `docker_tools.py` | Thin wrappers around the `docker` CLI |
| `exe_graph.py` | PyInstaller LangGraph pipeline |
| `exe_tools.py` | Thin wrappers around `pip` / `PyInstaller` |
| `fs_tools.py` | Sandboxed read/write access, scoped to the project folder only |
| `requirements.txt` | Dependencies for this agent (not your target project) |

---

## 3. Setup

```bash
git clone <this repo>   # or just copy the folder
cd Deployment_Agent

python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

### Get a Gemini API key

1. Go to [Google AI Studio](https://aistudio.google.com/apikey).
2. Create an API key (this is the **AI Studio** key, not a Vertex AI / GCP service account).
3. Export it:

```bash
export GOOGLE_API_KEY=your_key_here      # Windows PowerShell: $env:GOOGLE_API_KEY="your_key_here"
```

`GEMINI_API_KEY` also works if that's what you already have set.

### Docker mode also needs Docker installed & running

```bash
docker info     # must succeed WITHOUT sudo
```

If it asks for `sudo` or says "permission denied":

```bash
sudo usermod -aG docker $USER
sudo reboot     # a full logout/login is required — a new terminal is not enough
```

### Exe mode also needs PyInstaller

The agent installs it automatically into the current environment if missing — no manual
step needed, as long as `pip` can reach PyPI.

---

## 4. Usage

### Docker

```bash
python main.py /path/to/project
python main.py /path/to/project --image myapp --max-attempts 10
python main.py /path/to/project --env-file .env      # passed to the smoke test & run command
```

| Flag | Meaning | Default |
|---|---|---|
| `project_dir` | Folder to containerize | required |
| `--image` | Docker image name/tag | folder name |
| `--env-file` | `.env` file for the smoke test + final run command | none |
| `--max-attempts` | Debug-loop retry limit | `8` |
| `--model` | Gemini model | `gemini-2.5-flash` |
| `--fallback-model` | Backup model if the main one keeps failing | none |

On success:

```
✅ IMAGE READY: myapp
▶ Run it with:
    docker run --rm -p 8000:8000 myapp
```

### Windows .exe

```powershell
python main.py C:\path\to\project --exe --name myapp
python main.py C:\path\to\project --exe --windowed --icon app.ico   # GUI app (Tkinter/PyQt)
python main.py C:\path\to\project --exe --entry app.py              # force the entry script
```

| Flag | Meaning | Default |
|---|---|---|
| `--exe` | Switch to the exe pipeline | off (Docker is default) |
| `--name` | Output name → `dist/<name>.exe` | folder name |
| `--entry` | Entry `.py` script | auto-detected (`main.py`, `app.py`, ...) |
| `--windowed` | GUI app: hide console, skip the launch test | off |
| `--icon` | Path to a `.ico` file | none |

On success:

```
✅ EXE READY: C:\path\to\project\dist\myapp.exe
▶ Double-click "C:\path\to\project\dist\myapp.exe"  or run it directly
```

### Shared behavior

- **Backups:** the first time the agent overwrites a file, the original is saved to
  `.dockerize_backup/` inside your project folder.
- **Sandboxing:** the agent can only read/write inside the project folder you gave it —
  paths that try to escape it (`../../etc`) are rejected.
- **Unfixable failures:** if a failure needs something the agent can't provide (real API
  secrets, an external database, GCP credentials), the LLM flags it and the loop stops
  instead of retrying forever.

---

## 5. Troubleshooting

**`Docker is not installed or the daemon isn't running`**
Run `docker info` yourself — if it needs `sudo`, see the setup section above. If it
says the daemon isn't running: `sudo systemctl start docker`.

**`Project folder not found`**
Usually a leading `./` combined with an absolute-looking path underneath it (e.g.
`./home/ubuntu/...` gets resolved relative to the *current* directory, not `/home`).
Use an absolute path (`/home/ubuntu/...`) or `~/...` instead. The error message
suggests the corrected path when it detects this.

**`GoogleAuthError: Unable to find your project` / mentions Vertex AI or GCP**
This means the model name got routed to Vertex AI instead of AI Studio. The agent
auto-prefixes bare model names with `google_genai:` to prevent this — make sure you're
on the latest `main.py`. If you pass a model with an explicit provider prefix yourself
(e.g. `google_vertexai:...`), that's on you to configure GCP credentials for.

**`503 UNAVAILABLE` / `high demand` from Gemini**
Transient — the agent already retries automatically (up to 6 times with backoff) and
falls back to `--fallback-model` if set. If it still fails, Google's servers were down
for longer than the retry window; just re-run.

**Debug loop keeps failing and hits `--max-attempts`**
Read the last printed error — the failure is likely something the agent genuinely can't
fix from inside the project folder (missing secrets, a private package registry it
can't reach, network access blocked mid-build). Check `.dockerize_backup/` to see what
the agent changed and revert manually if needed.

**Windows `.exe` crashes on launch with `ModuleNotFoundError`**
This is what the smoke test step is designed to catch — if it slipped through, tell the
agent to add the missing package to `hiddenimports` in `<name>.spec` and re-run, or just
re-run the whole command (the debug loop should catch it automatically next time).
