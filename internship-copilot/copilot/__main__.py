"""Command line: ``python -m copilot [serve|doctor|init]``."""
from __future__ import annotations

import argparse
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path


def _line(status: str, text: str, hint: str = "") -> bool:
    tag = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}[status]
    print(f"{tag} {text}")
    if hint:
        for h in hint.splitlines():
            print(f"       {h}")
    return status != "fail"


def cmd_init(_: argparse.Namespace) -> int:
    from . import config

    config.ensure_data_dir()
    print(f"Data folder ready: {config.DATA_DIR}")
    print(f"  1. Edit your facts:   {config.user_path('profile')}")
    print("  2. Start the app:     python -m copilot serve")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    from . import config, latex

    ok = True
    print(f"Internship Copilot doctor  (data folder: {config.DATA_DIR})\n")
    v = sys.version_info
    ok &= _line("ok" if v >= (3, 10) else "fail", f"Python {v.major}.{v.minor}.{v.micro}", "" if v >= (3, 10) else "Python 3.10 or newer is required.")
    missing = []
    for mod in ("fastapi", "uvicorn", "httpx", "pydantic", "yaml", "jinja2", "bs4", "lxml", "trafilatura"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    ok &= _line("ok" if not missing else "fail", "Python packages" + (f" - missing: {', '.join(missing)}" if missing else ""),
                "" if not missing else "Run:  pip install -r requirements.txt")
    config.ensure_data_dir()

    # profile
    from . import profile as prof

    try:
        p = prof.load_profile()
        if p.example:
            _line("warn", "Profile is still the SAMPLE data", f"Edit {config.user_path('profile')} (or use the app's “Profile & templates” tab), then delete `example: true`.")
        else:
            _line("ok", f"Profile loaded for {p.identity.name} ({len(p.projects)} projects, {len(p.experience)} experiences)")
    except prof.ConfigFileError as exc:
        ok &= _line("fail", "Profile has errors", "\n".join(exc.errors[:6]))
    for kind in ("letter", "email"):
        try:
            prof.load_blueprint(kind)
            _line("ok", f"{kind} blueprint is valid")
        except prof.ConfigFileError as exc:
            ok &= _line("fail", f"{kind} blueprint has errors", "\n".join(exc.errors[:6]))

    # LaTeX
    st = config.load_settings()
    eng = latex.find_engine(st.latex.engine)
    if not eng:
        ok &= _line("fail", "No LaTeX engine found (pdflatex / xelatex / lualatex / tectonic)",
                    "Install TeX Live (Linux/macOS: texlive-full or MacTeX) or MiKTeX (Windows), then re-run.\n"
                    "Without LaTeX the app still writes .tex files you can compile in Overleaf.")
    else:
        with tempfile.TemporaryDirectory() as td:
            tex = ("\\documentclass{article}\\usepackage[T1]{fontenc}\\usepackage[utf8]{inputenc}\\usepackage[french]{babel}"
                   "\\usepackage{enumitem,titlesec,xcolor,microtype,lmodern,hyperref}\\begin{document}Bonjour, élève.\\end{document}")
            res = latex.compile_tex(tex, Path(td), "t", st.latex.engine, st.latex.timeout_s)
        if res.ok:
            _line("ok", f"LaTeX: {eng[0]} compiles (babel-french, enumitem, titlesec, hyperref present)")
        else:
            ok &= _line("fail", f"LaTeX: {eng[0]} found but a test document failed", res.message[:400] + "\nInstall the missing packages (TeX Live: texlive-latex-extra, texlive-lang-french; MiKTeX installs on demand).")

    # LLM
    from . import llm

    s = llm.get_llm().status()
    if st.llm.backend == "fake":
        _line("warn", "Demo mode (COPILOT_LLM=fake): no real model is used")
    elif not s["ok"]:
        ok &= _line("fail", s["error"], "Install Ollama from https://ollama.com and start it, then run:  ollama pull " + st.llm.model)
    elif not s["model_installed"]:
        ok &= _line("fail", f"Ollama {s['version']} is running but model “{s['model']}” is not installed",
                    f"Run:  ollama pull {s['model']}\nInstalled: " + (", ".join(m['name'] for m in s['models']) or "(none)"))
    else:
        _line("ok", f"Ollama {s['version']} is running and “{s['model']}” is installed")

    from . import fetch

    _line("ok" if fetch.playwright_available() else "warn", "Playwright (optional, for JavaScript-only job pages): " + ("installed" if fetch.playwright_available() else "not installed"),
          "" if fetch.playwright_available() else "Optional:  pip install playwright && playwright install chromium")
    print("\nAll required checks passed." if ok else "\nSome required checks failed: see the hints above.")
    return 0 if ok else 1


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from .server import allowed_hosts, create_app

    if a.host not in ("127.0.0.1", "localhost", "::1") and not a.allow_any_host:
        print(f"Note: binding to {a.host}. The app only answers to Host names {sorted(allowed_hosts())} unless you pass --allow-any-host.")
    if a.host in ("0.0.0.0", "::") or a.allow_any_host:
        print("Security note: this app can read your profile and write files. Only expose it on networks you trust.")
    url = f"http://localhost:{a.port}"
    print(f"\nInternship Copilot is starting on {url}  (Ctrl+C to stop)\n")
    if a.open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(allow_any_host=a.allow_any_host), host=a.host, port=a.port, log_level="warning" if not a.verbose else "info")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="copilot", description="Internship application copilot (local, Ollama + LaTeX).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="start the web app (default)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--allow-any-host", action="store_true", help="disable the Host-header check (needed behind a proxy)")
    s.add_argument("--open", action="store_true", help="open the browser")
    s.add_argument("--verbose", action="store_true")
    sub.add_parser("doctor", help="check Python, LaTeX, Ollama and your profile")
    sub.add_parser("init", help="create the data folder with sample files")
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(["serve", *(argv or [])])
    return {"serve": cmd_serve, "doctor": cmd_doctor, "init": cmd_init}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
