"""Optional UI test: run the REAL bookmarklet on a job page and check the app receives it (needs playwright + a running server)."""
import sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots"
FIX = Path(__file__).parent / "fixtures"

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        name = "jsonld_job.html" if self.path.startswith("/jsonld") else "plain_job.html"
        body = (FIX / name).read_bytes()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
errors = []
with sync_playwright() as pw:
    b = pw.chromium.launch(); ctx = b.new_context(viewport={"width": 1280, "height": 900})
    app = ctx.new_page(); app.on("pageerror", lambda e: errors.append(str(e)))
    app.goto(BASE); app.wait_for_selector("#pills .pill")
    href = app.get_attribute("#bookmarklet", "href")
    assert href.startswith("javascript:"), href[:40]
    code = unquote(href[len("javascript:"):])
    for path, expect_title, expect_company in (("/plain", "Stage Ingénieur Data", "DataFlow"), ("/jsonld", "Backend Intern", "Acme Cloud")):
        page = ctx.new_page(); page.goto(f"http://127.0.0.1:{port}{path}")
        with ctx.expect_page() as popup:
            page.evaluate(code)
        pop = popup.value; pop.wait_for_selector("#job-preview:not([hidden])", timeout=15000); time.sleep(0.5)
        title, company, desc = pop.input_value("#job-title"), pop.input_value("#job-company"), pop.input_value("#job-desc")
        print(f"{path}: title={title!r} company={company!r} desc={len(desc)} chars | hash after load: {pop.evaluate('location.hash')!r}")
        assert expect_title in title and expect_company in company, (title, company)
        assert "é" in desc or "Kubernetes" in desc or "Postgres" in desc
        pop.screenshot(path=f"{OUT}/10_bookmarklet{path.replace('/', '_')}.png")
        pop.close(); page.close()
    b.close()
print("bookmarklet round-trip OK; page errors:", errors or "none")
