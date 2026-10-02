"""LLM access: a thin, robust Ollama client + a deterministic FakeLLM for demos and tests."""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable, Optional

import httpx

from . import config
from .textutil import one_line


class LLMError(Exception):
    """User-presentable failure (message is shown in the UI)."""

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- JSON helpers
_FENCE = re.compile(r"^```[a-zA-Z0-9]*\s*|\s*```\s*$")
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> Any:
    """Parse a JSON object from model output, tolerating fences, <think> blocks and trailing commas."""
    t = _THINK.sub("", text or "").strip()
    t = _FENCE.sub("", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b > a:
        frag = t[a : b + 1]
        for candidate in (frag, re.sub(r",\s*([}\]])", r"\1", frag)):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    raise LLMError("The model did not return valid JSON.", kind="bad_json")


# --------------------------------------------------------------------------- base class
class BaseLLM:
    name = "base"
    model = ""

    def chat(
        self,
        system: str,
        user: str,
        *,
        task: str = "",
        schema: Optional[dict] = None,
        temperature: float = 0.2,
        max_tokens: int = 512,
        ctx: Optional[dict] = None,
        history: Optional[list[dict]] = None,
        cancel: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def status(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def chat_json(self, system: str, user: str, *, schema: dict, validate: Callable[[Any], Any], retries: int = 1, **kw) -> Any:
        """Ask for schema-constrained JSON, validate it, and retry once with the validation error."""
        history: list[dict] = []
        last_err = ""
        for attempt in range(retries + 1):
            raw = self.chat(system, user, schema=schema, history=history or None, **kw)
            try:
                return validate(extract_json(raw))
            except LLMError as exc:
                last_err = str(exc)
            except Exception as exc:  # validation error from pydantic etc.
                last_err = f"{type(exc).__name__}: {exc}"
            history = [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That output was invalid ({one_line(last_err)[:300]}). Return ONLY a valid JSON object matching the schema."},
            ]
        raise LLMError(f"The model could not produce valid structured output ({last_err}). Try a larger model.", kind="bad_json")


# --------------------------------------------------------------------------- Ollama
class OllamaLLM(BaseLLM):
    name = "ollama"

    def __init__(self, settings: config.LLMSettings, model: Optional[str] = None):
        self.s = settings
        self.base = settings.base_url.rstrip("/")
        self.model = model or settings.model
        self._caps_cache: dict[str, dict] = {}

    # ---- discovery ------------------------------------------------------------------
    def _client(self, read: float = 15.0) -> httpx.Client:
        return httpx.Client(timeout=httpx.Timeout(connect=4.0, read=read, write=30.0, pool=5.0))

    def status(self) -> dict:
        try:
            with self._client() as c:
                ver = c.get(f"{self.base}/api/version").json().get("version", "?")
                tags = c.get(f"{self.base}/api/tags").json().get("models", [])
        except httpx.HTTPError as exc:
            return {"ok": False, "backend": "ollama", "error": f"Cannot reach Ollama at {self.base} ({type(exc).__name__}).",
                    "models": [], "model": self.model, "model_installed": False, "version": ""}
        models = []
        for m in tags:
            d = m.get("details", {}) or {}
            models.append({
                "name": m.get("name"),
                "size_gb": round((m.get("size") or 0) / 1e9, 1),
                "params": d.get("parameter_size", ""),
                "quant": d.get("quantization_level", ""),
            })
        names = {m["name"] for m in models}
        want = self.model
        installed = want in names or (":" not in want and f"{want}:latest" in names)
        return {"ok": True, "backend": "ollama", "version": ver, "models": models, "model": want,
                "model_installed": installed, "error": ""}

    def _think_param(self, model: str) -> Any:
        """Decide the `think` flag from /api/show metadata (thinking models are slow and unnecessary here)."""
        if self.s.think == "model-default":
            return None
        if model in self._caps_cache:
            return self._caps_cache[model].get("think")
        think: Any = None
        try:
            with self._client() as c:
                r = c.post(f"{self.base}/api/show", json={"model": model})
                if r.status_code == 200:
                    vals = (r.json().get("thinking") or {}).get("values") or []
                    if False in vals:
                        think = False
                    else:
                        strs = [v for v in vals if isinstance(v, str)]
                        if strs:
                            think = "low" if "low" in strs else strs[0]
        except httpx.HTTPError:
            pass
        self._caps_cache[model] = {"think": think}
        return think

    # ---- chat ------------------------------------------------------------------------
    def chat(self, system, user, *, task="", schema=None, temperature=0.2, max_tokens=512, ctx=None,
             history=None, cancel=None, on_progress=None) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if history:
            messages += history
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.s.keep_alive,
            "options": {"temperature": temperature, "num_ctx": self.s.num_ctx, "num_predict": max_tokens},
        }
        if schema:
            payload["format"] = schema
        think = self._think_param(self.model)
        if think is not None:
            payload["think"] = think

        timeout = httpx.Timeout(connect=5.0, read=float(self.s.timeout_s), write=30.0, pool=5.0)
        for attempt in (1, 2):
            parts: list[str] = []
            tokens = 0
            try:
                with httpx.Client(timeout=timeout) as c, c.stream("POST", f"{self.base}/api/chat", json=payload) as r:
                    if r.status_code != 200:
                        body = r.read().decode("utf-8", "replace")
                        err = _error_text(body)
                        if r.status_code == 400 and "think" in err.lower() and "think" in payload and attempt == 1:
                            payload.pop("think", None)  # model doesn't accept the flag -> retry without it
                            continue
                        raise _map_http_error(r.status_code, err, self.model)
                    for line in r.iter_lines():
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if chunk.get("error"):
                            raise _map_http_error(500, str(chunk["error"]), self.model)
                        piece = (chunk.get("message") or {}).get("content") or ""
                        if piece:
                            parts.append(piece)
                            tokens += 1
                            if on_progress and tokens % 8 == 0:
                                on_progress(tokens)
                        if chunk.get("done"):
                            break
                return "".join(parts)
            except httpx.ConnectError as exc:
                raise LLMError(
                    f"Cannot reach Ollama at {self.base}. Start the Ollama app (or run `ollama serve`) and try again.",
                    kind="unreachable",
                ) from exc
            except httpx.ReadTimeout as exc:
                raise LLMError(
                    "The model took too long to answer. Use a smaller model or raise llm.timeout_s in Settings.",
                    kind="timeout",
                ) from exc
            except httpx.HTTPError as exc:
                raise LLMError(f"Network error talking to Ollama: {exc}", kind="unreachable") from exc
        raise LLMError("Ollama request failed.")  # pragma: no cover


def _error_text(body: str) -> str:
    try:
        return str(json.loads(body).get("error", body))
    except Exception:
        return body[:300]


def _map_http_error(status: int, err: str, model: str) -> LLMError:
    low = err.lower()
    if status == 404 or "not found" in low:
        return LLMError(f"Model '{model}' is not installed in Ollama. Run:  ollama pull {model}", kind="model_missing")
    if "memory" in low or "oom" in low or "unexpected eof" in low or "runner" in low and "stopped" in low or "exit status" in low:
        return LLMError(
            f"The model process for '{model}' stopped, most likely because the computer ran out of memory. "
            f"Close other programs, choose a smaller model (Setup tab), or lower llm.num_ctx in the settings. ({err[:120]})", kind="oom")
    return LLMError(f"Ollama error ({status}): {err[:300]}")


# --------------------------------------------------------------------------- FakeLLM (demo / tests)
class FakeLLM(BaseLLM):
    """Deterministic stand-in so the UI and tests run without any model. Output is obviously templated."""

    name = "fake"
    model = "demo-mode"

    def status(self) -> dict:
        return {"ok": True, "backend": "fake", "version": "demo", "models": [{"name": "demo-mode", "size_gb": 0, "params": "", "quant": ""}],
                "model": "demo-mode", "model_installed": True, "error": ""}

    def chat(self, system, user, *, task="", schema=None, temperature=0.2, max_tokens=512, ctx=None,
             history=None, cancel=None, on_progress=None) -> str:
        ctx = ctx or {}
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if task == "analysis":
            return json.dumps(self._analysis(ctx))
        if task == "facts":
            return json.dumps(self._facts(ctx))
        if task.startswith("paragraph"):
            return self._paragraph(ctx)
        return "{}"

    # ---- helpers ----
    @staticmethod
    def _analysis(ctx: dict) -> dict:
        from . import techvocab

        text = ctx.get("text", "")
        terms = techvocab.find_terms(text)
        stack = [techvocab.display(t) for t, _ in terms.most_common(6)]
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", one_line(text)) if len(s.strip()) > 40]
        mission = next((s for s in sents if re.search(r"mission|you will|vous|aufgaben|d[ée]velopp|build|conce", s, re.I)), sents[0] if sents else "")
        about = next((s for s in sents if re.search(r"we are|nous sommes|notre|our (company|team|mission)|wir sind|founded|fond", s, re.I)), "")
        return {
            "role_title": ctx.get("title", ""),
            "company_name": ctx.get("company", ""),
            "mission": mission[:240],
            "must_have": stack[:5],
            "nice_to_have": stack[5:6],
            "domains": techvocab.infer_domains(terms, top=2) or ["other"],
            "company_description": about[:240],
        }

    @staticmethod
    def _facts(ctx: dict) -> dict:
        text = one_line(ctx.get("text", ""))
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 40 < len(s.strip()) < 220]
        picks = [s for s in sents if re.search(r"we |our |nous |notre |wir |unsere |build|develop|platform|plateforme|customers|clients|mission", s, re.I)][:2]
        return {"facts": [{"fact": s, "quote": s[:120]} for s in picks]}

    @staticmethod
    def _paragraph(ctx: dict) -> str:
        lang = ctx.get("lang", "en")
        pid = ctx.get("pid", "")
        company = ctx.get("company") or ("votre entreprise" if lang == "fr" else "your company")
        role = ctx.get("role") or ("stage" if lang == "fr" else "internship")
        facts = ctx.get("facts") or []
        ev = ctx.get("evidence") or []
        note = ctx.get("note", "")
        first_ev = ev[0] if ev else {}
        title = first_ev.get("title", "")
        stack = ", ".join((first_ev.get("stack") or [])[:3])
        bullet = (first_ev.get("bullets") or [""])[0]
        if lang == "fr":
            if pid in ("intro", "hook", "motivation"):
                return (f"Je souhaite postuler au poste « {role} » chez {company}. "
                        + (f"{note} " if note else "")
                        + "Étudiant en dernière année, je recherche une expérience concrète où je pourrai appliquer ce que j'ai appris.")
            if pid in ("company", "why"):
                f = facts[0]["fact"] if facts else ""
                return (f"Ce qui me motive chez {company} : {f}" if f else
                        f"Le poste proposé par {company} correspond précisément aux domaines dans lesquels je veux progresser.")
            if pid in ("fit", "value", "project"):
                return (f"Dans mon projet « {title} », j'ai travaillé avec {stack} : {bullet}" if title else
                        "Mes projets académiques m'ont permis de développer des bases solides que je souhaite mettre au service de votre équipe.")
            return "Version de démonstration : paragraphe généré sans modèle."
        if pid in ("intro", "hook", "motivation"):
            return (f"I am applying for the {role} position at {company}. "
                    + (f"{note} " if note else "")
                    + "As a final-year student, I am looking for hands-on experience where I can apply what I have learned.")
        if pid in ("company", "why"):
            f = facts[0]["fact"] if facts else ""
            return (f"What draws me to {company}: {f}" if f else
                    f"The role at {company} matches exactly the areas where I want to grow.")
        if pid in ("fit", "value", "project"):
            return (f"In my project “{title}”, I worked with {stack}: {bullet}" if title else
                    "My academic projects gave me solid foundations that I would like to put to work for your team.")
        return "Demo mode: paragraph generated without a model."


# --------------------------------------------------------------------------- factory
_cache: dict[tuple, BaseLLM] = {}
_cache_lock = threading.Lock()


def get_llm(model: Optional[str] = None) -> BaseLLM:
    s = config.load_settings().llm
    if s.backend == "fake":
        return FakeLLM()
    key = (s.base_url, model or s.model, s.num_ctx, s.keep_alive, s.think, s.timeout_s)
    with _cache_lock:
        if key not in _cache:
            _cache[key] = OllamaLLM(s, model)
        return _cache[key]


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()
