"""Ollama HTTP API client with streaming support.

Talks to a local Ollama instance at http://127.0.0.1:11434.
All network calls happen off the Qt main thread via generate_stream().
"""

from __future__ import annotations

import json
import subprocess
import shutil
import time
from typing import Generator, List, Optional

import urllib.request
import urllib.error

DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "llama3.2"
DEFAULT_TIMEOUT_SEC = 120


class OllamaClient:
    """Lightweight wrapper around the Ollama REST API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: int = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._server_proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    def start_server(self, wait: bool = True, timeout_sec: int = 20) -> bool:
        """Launch ``ollama serve`` as a background process.

        Returns True once the server is responding (or immediately if
        *wait* is False).  Returns False if the server didn't come up
        within *timeout_sec*.
        """
        # Already reachable?
        if self.is_available():
            return True

        ollama_exe = shutil.which("ollama")
        if not ollama_exe:
            ollama_exe = self._find_ollama_windows()
        if not ollama_exe:
            raise FileNotFoundError(
                "'ollama' executable not found on PATH. "
                "Install Ollama from https://ollama.com"
            )

        # On Windows, use a hidden window (STARTUPINFO) instead of
        # CREATE_NO_WINDOW so that ollama serve can initialise its
        # console handles without error.
        popen_kwargs: dict = {}
        if hasattr(subprocess, "STARTUPINFO"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            popen_kwargs["startupinfo"] = si

        self._server_proc = subprocess.Popen(
            [ollama_exe, "serve"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )

        # Give the process a moment then check it didn't exit immediately
        time.sleep(1.5)
        rc = self._server_proc.poll()
        if rc is not None:
            # Process died — try the Windows tray app as a fallback
            stderr_out = ""
            try:
                stderr_out = self._server_proc.stderr.read(500).decode(errors="replace")
            except Exception:
                pass
            self._server_proc = None

            if self._try_ollama_app():
                pass  # fallback started, continue to wait loop
            else:
                reason = stderr_out.strip().split("\n")[-1] if stderr_out.strip() else f"exit code {rc}"
                raise RuntimeError(f"ollama serve failed: {reason}")

        if not wait:
            return True

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if self.is_available():
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _find_ollama_windows() -> Optional[str]:
        """Search common Windows install locations for ollama.exe."""
        import os
        candidates = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Ollama", "ollama.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return None

    def _try_ollama_app(self) -> bool:
        """Try launching the Ollama Windows tray/desktop app as a server fallback."""
        import os
        app_candidates = [
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama app.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Ollama", "ollama app.exe"),
        ]
        for app_path in app_candidates:
            if app_path and os.path.isfile(app_path):
                try:
                    popen_kwargs: dict = {}
                    if hasattr(subprocess, "STARTUPINFO"):
                        si = subprocess.STARTUPINFO()
                        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        si.wShowWindow = 0  # SW_HIDE
                        popen_kwargs["startupinfo"] = si
                    self._server_proc = subprocess.Popen(
                        [app_path],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **popen_kwargs,
                    )
                    return True
                except Exception:
                    continue
        return False

    def stop_server(self) -> None:
        """Terminate the server process we started (if any)."""
        if self._server_proc is not None:
            try:
                self._server_proc.terminate()
                self._server_proc.wait(timeout=5)
            except Exception:
                pass
            self._server_proc = None

    @property
    def server_running(self) -> bool:
        """True if we started a server and it's still alive."""
        if self._server_proc is None:
            return False
        return self._server_proc.poll() is None

    # ------------------------------------------------------------------
    # Health / model listing
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True if Ollama is reachable and has at least one model."""
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """Return installed model names (e.g. ['llama3.2:latest', 'qwen2.5:latest'])."""
        try:
            req = urllib.request.Request(f"{self.base_url}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Chat completion (streaming)
    # ------------------------------------------------------------------

    def generate_stream(
        self,
        messages: List[dict],
        model: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """Yield content tokens as they arrive from ``POST /api/chat``.

        *messages* follows the OpenAI-compatible format::

            [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]

        Raises ``ConnectionError`` if Ollama is unreachable.
        """
        payload = json.dumps({
            "model": model or self.model,
            "messages": messages,
            "stream": True,
        }).encode()

        req = urllib.request.Request(
            f"{self.base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.URLError as exc:
            raise ConnectionError(
                f"Cannot reach Ollama at {self.base_url}: {exc}"
            ) from exc

        try:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                token = chunk.get("message", {}).get("content", "")
                if token:
                    yield token
                if chunk.get("done"):
                    break
        finally:
            resp.close()

    # ------------------------------------------------------------------
    # Non-streaming convenience
    # ------------------------------------------------------------------

    def generate(
        self,
        messages: List[dict],
        model: Optional[str] = None,
    ) -> str:
        """Return complete response text (blocks until done)."""
        return "".join(self.generate_stream(messages, model=model))
