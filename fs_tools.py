"""Sandboxed file access: the agent can only touch files inside the project folder."""
from pathlib import Path

IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea",
               ".vscode", ".dockerize_backup", ".mypy_cache", ".pytest_cache"}


class Workspace:
    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            hint = ""
            if root.startswith("./") and Path(root[1:]).is_dir():
                hint = f"\nDid you mean the absolute path '{root[1:]}'? (remove the leading '.')"
            raise NotADirectoryError(f"Project folder not found: {self.root}{hint}")

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and not p.is_relative_to(self.root):
            raise PermissionError(f"Path escapes project folder: {rel}")
        return p

    def exists(self, rel: str) -> bool:
        return self.resolve(rel).is_file()

    def read(self, rel: str, limit: int = 12000) -> str:
        try:
            text = self.resolve(rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            return ""
        return text if len(text) <= limit else text[:limit] + "\n...[truncated]"

    def write(self, rel: str, content: str) -> None:
        p = self.resolve(rel)
        if p.exists():  # keep the ORIGINAL version once, so nothing is lost
            backup = self.root / ".dockerize_backup" / rel
            if not backup.exists():
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(p.read_bytes())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def files(self, max_files: int = 200) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("*")):
            rel = p.relative_to(self.root)
            if any(part in IGNORE_DIRS for part in rel.parts) or not p.is_file():
                continue
            out.append(rel.as_posix())
            if len(out) >= max_files:
                break
        return out
