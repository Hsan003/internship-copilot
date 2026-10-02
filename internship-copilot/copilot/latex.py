"""LaTeX helpers: safe escaping, a Jinja environment with LaTeX-friendly delimiters, and compilation.

Template syntax (so it never clashes with LaTeX braces):
    \\VAR{ value }            print a value - AUTOMATICALLY ESCAPED for LaTeX
    \\VAR{ value|raw }        print raw LaTeX (for fragments you build yourself)
    \\BLOCK{ for x in xs } ... \\BLOCK{ endfor }       control flow
    \\#{ comment }            template comment (not emitted)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import jinja2


class RawTeX(str):
    """A string that is already valid LaTeX (never escaped again)."""


_REPL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "\u00a0": "~", "\u202f": r"\,", "\u2009": r"\,", "\u2007": "~", "\u2011": "-", "\u2212": "-", "\u00ad": r"\-",
    "\u2192": r"$\rightarrow$", "\u2190": r"$\leftarrow$", "\u21d2": r"$\Rightarrow$", "\u2194": r"$\leftrightarrow$",
    "\u2022": r"\textbullet{}", "\u25cf": r"\textbullet{}", "\u00b7": r"\textperiodcentered{}", "\u2026": r"\ldots{}",
    "\u20ac": r"\texteuro{}", "\u2264": r"$\leq$", "\u2265": r"$\geq$", "\u00d7": r"\texttimes{}", "\u2248": r"$\approx$",
    "\u2122": r"\texttrademark{}", "\u00ae": r"\textregistered{}", "\u00a9": r"\textcopyright{}",
    "\u2713": "", "\u2714": "", "\u2705": "", "\u2757": "", "\ufe0f": "", "\u200b": "", "\u200d": "", "\ufeff": "",
}
# characters pdfLaTeX (utf8 + T1) can print natively besides Latin-1 / Latin Extended-A/B
_KEEP = set("\u2013\u2014\u2018\u2019\u201a\u201c\u201d\u201e\u00ab\u00bb\u2039\u203a\u0152\u0153\u0178")


def tex_escape(s: Any) -> str:
    if s is None:
        return ""
    out: list[str] = []
    for ch in str(s):
        if ch in _REPL:
            out.append(_REPL[ch])
        elif ch == "\n":
            out.append(" ")
        elif ord(ch) < 32:
            continue
        elif ord(ch) <= 0x024F or ch in _KEEP:
            out.append(ch)
        # else: symbols / emoji / scripts pdfLaTeX cannot typeset -> dropped instead of failing the build
    return "".join(out)


def url_escape(u: str) -> str:
    u = (u or "").strip().replace(" ", "%20")
    return u.replace("\\", "/").replace("%", r"\%").replace("#", r"\#").replace("&", r"\&")


def href(url: str, text: str) -> RawTeX:
    return RawTeX(rf"\href{{{url_escape(url)}}}{{{tex_escape(text)}}}")


def tex_lines(lines: Iterable[str]) -> RawTeX:
    """Escape each line and join with LaTeX line breaks (no trailing break -> safe before blank lines)."""
    return RawTeX(r" \\ ".join(tex_escape(l) for l in lines if l is not None and str(l).strip() != ""))


def join_raw(parts: Iterable[Any], sep: str) -> RawTeX:
    return RawTeX(sep.join(p if isinstance(p, RawTeX) else tex_escape(p) for p in parts))


def _finalize(value: Any) -> Any:
    if isinstance(value, RawTeX):
        return str(value)
    if value is None:
        return ""
    if isinstance(value, str):
        return tex_escape(value)
    return value


class _Env(jinja2.Environment):
    """``x.items`` must mean the *key* "items" of a dict, not dict.items (a classic template trap)."""

    def getattr(self, obj, attribute):  # noqa: A003 - jinja API name
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


def make_env() -> jinja2.Environment:
    env = _Env(
        block_start_string=r"\BLOCK{", block_end_string="}",
        variable_start_string=r"\VAR{", variable_end_string="}",
        comment_start_string=r"\#{", comment_end_string="}",
        trim_blocks=True, lstrip_blocks=True, autoescape=False, keep_trailing_newline=True,
        finalize=_finalize, undefined=jinja2.StrictUndefined,
    )
    env.filters["raw"] = lambda v: RawTeX(v if v is not None else "")
    env.filters["url"] = lambda v: RawTeX(url_escape(v))
    env.filters["tex"] = lambda v: RawTeX(tex_escape(v))
    return env


class TemplateError(Exception):
    pass


def validate_template(text: str) -> None:
    try:
        make_env().parse(text)
    except jinja2.TemplateSyntaxError as exc:
        raise TemplateError(f"Template syntax error on line {exc.lineno}: {exc.message}") from exc


def render(template_text: str, context: dict) -> str:
    env = make_env()
    try:
        return env.from_string(template_text).render(**context)
    except jinja2.TemplateSyntaxError as exc:
        raise TemplateError(f"Template syntax error on line {exc.lineno}: {exc.message}") from exc
    except jinja2.UndefinedError as exc:
        raise TemplateError(f"Template uses an unknown variable: {exc.message}") from exc
    except jinja2.TemplateError as exc:
        raise TemplateError(f"Template error: {exc}") from exc


# --------------------------------------------------------------------------- compilation
@dataclass
class CompileResult:
    ok: bool
    pdf: Optional[Path] = None
    pages: Optional[int] = None
    engine: str = ""
    error: str = ""  # machine code: no_engine | timeout | failed
    message: str = ""  # human text
    log_excerpt: str = ""
    log: str = field(default="", repr=False)


def find_engine(pref: str = "auto") -> Optional[tuple[str, str]]:
    order = ["pdflatex", "xelatex", "lualatex", "tectonic"] if pref in ("", "auto") else [pref]
    for name in order:
        path = shutil.which(name)
        if path:
            return name, path
    return None


_PAGES = re.compile(r"Output written on .*?\((\d+) pages?", re.IGNORECASE)


def parse_pages(log: str) -> Optional[int]:
    m = _PAGES.search(log or "")
    return int(m.group(1)) if m else None


def parse_error(log: str) -> str:
    lines = (log or "").splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("!"):
            ctx = [ln] + [l for l in lines[i + 1: i + 6] if l.strip()][:4]
            return "\n".join(ctx)
    return "\n".join([l for l in lines[-12:] if l.strip()])


def assets_dir() -> Path:
    from . import config

    return config.DATA_DIR / "templates" / "assets"


def tex_env() -> dict:
    """Environment for the LaTeX run: your own .cls/.sty/images in data/templates/assets are found without copying."""
    env = os.environ.copy()
    assets = assets_dir()
    if assets.is_dir():
        # trailing separator = "then the default search path" (kpathsea convention)
        env["TEXINPUTS"] = f"{assets}//{os.pathsep}" + env.get("TEXINPUTS", "")
    return env


def compile_tex(tex: str, outdir: Path, name: str, engine_pref: str = "auto", timeout: int = 120) -> CompileResult:
    outdir.mkdir(parents=True, exist_ok=True)
    tex_path = outdir / f"{name}.tex"
    tex_path.write_text(tex, encoding="utf-8")
    pdf_path = outdir / f"{name}.pdf"
    if pdf_path.exists():
        pdf_path.unlink()
    eng = find_engine(engine_pref)
    if not eng:
        return CompileResult(False, error="no_engine",
                             message="No LaTeX engine found (pdflatex / xelatex / lualatex / tectonic). The .tex file was saved - "
                                     "open it in Overleaf or install TeX Live / MiKTeX.")
    engine, path = eng
    if engine == "tectonic":
        cmd = [path, "-X", "compile", "--outdir", str(outdir), tex_path.name]
    else:
        cmd = [path, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape", f"-output-directory={outdir}", tex_path.name]
    try:
        proc = subprocess.run(cmd, cwd=outdir, capture_output=True, text=True, timeout=timeout, errors="replace", env=tex_env())
    except subprocess.TimeoutExpired:
        return CompileResult(False, engine=engine, error="timeout", message=f"LaTeX took longer than {timeout}s and was stopped.")
    log_file = outdir / f"{name}.log"
    log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else (proc.stdout + proc.stderr)
    for ext in (".aux", ".out", ".toc", ".fls", ".fdb_latexmk"):
        (outdir / f"{name}{ext}").unlink(missing_ok=True)
    if proc.returncode == 0 and pdf_path.exists():
        pages = parse_pages(log) or _pdfinfo_pages(pdf_path)
        return CompileResult(True, pdf=pdf_path, pages=pages, engine=engine, log=log)
    excerpt = parse_error(log)
    return CompileResult(False, engine=engine, error="failed", message="LaTeX reported an error:\n" + excerpt, log_excerpt=excerpt, log=log)


def _pdfinfo_pages(pdf: Path) -> Optional[int]:
    exe = shutil.which("pdfinfo")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, str(pdf)], capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.MULTILINE)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def build_pdf(render_fn, outdir: Path, name: str, lang: str, engine_pref: str = "auto", timeout: int = 120) -> CompileResult:
    """Render with babel for ``lang`` and compile; if the French babel files are missing, retry with English."""
    babel = "french" if lang == "fr" else "english"
    res = compile_tex(render_fn(babel), outdir, name, engine_pref, timeout)
    if not res.ok and babel == "french" and res.error == "failed" and re.search(r"french|babel", res.log, re.IGNORECASE):
        res2 = compile_tex(render_fn("english"), outdir, name, engine_pref, timeout)
        if res2.ok:
            res2.message = "French babel support is missing from your LaTeX install, so English hyphenation was used."
            return res2
    return res
