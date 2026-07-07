"""
End-to-end integration test for the entry pipeline.

Serves realistic sweepstakes fixtures from a local HTTP server and runs the REAL
`enter_sweepstake` pipeline against each — the check that unit tests can't do,
because it exercises actual filling, submitting, navigation, and outcome
detection together. Run with:  python e2e_test.py

Exits non-zero if any fixture misbehaves, so it can gate a build.
"""
from __future__ import annotations

import asyncio
import http.server
import socketserver
import sys
import threading

from sweepstake_genie.browser import BrowserManager
from sweepstake_genie.form_filler import enter_sweepstake

PROFILE = {
    "first_name": "Jane", "last_name": "Doe",
    "email": "jane.doe@example.com", "email_confirm": "jane.doe@example.com",
    "phone": "555-123-4567", "address1": "123 Main St", "address2": "",
    "city": "Springfield", "state": "IL", "zip": "62701", "country": "US",
    "dob_month": "01", "dob_day": "15", "dob_year": "1990", "age": "34", "gender": "F",
}

# name -> (inner html, expected status)
FIXTURES = {
    "full_form": ("""<h1>Win a Trip Sweepstakes</h1>
      <form action="/thanks" method="get">
        <input name="first_name" placeholder="First Name" required>
        <input name="last_name" placeholder="Last Name" required>
        <input type="email" name="email" placeholder="Email" required>
        <input name="address" placeholder="Address"><input name="city" placeholder="City">
        <select name="state"><option value="">State</option><option value="IL">Illinois</option></select>
        <input name="zip" placeholder="Zip"><button type="submit">Enter to Win</button>
      </form>""", "entered"),
    "email_only": ("""<h1>Instant Win</h1>
      <form action="/thanks" method="get">
        <input type="email" name="email" placeholder="Enter your email" required>
        <button type="submit">Enter</button></form>""", "entered"),
    "required_select_radio": ("""<h1>Enter the Giveaway</h1>
      <form action="/thanks" method="get">
        <input name="first_name" required><input name="last_name" required>
        <input type="email" name="email" required>
        <select name="country" required><option value="">Country</option><option value="US">United States</option></select>
        <label><input type="radio" name="age18" value="no" required> No</label>
        <label><input type="radio" name="age18" value="yes"> Yes, 18+</label>
        <input type="checkbox" name="rules" required> I accept the rules
        <button type="submit">Submit Entry</button></form>""", "entered"),
    "landing_enter_here": ("""<h1>Amazing Prize</h1><p>Click below.</p>
      <a href="/form_page">Enter Here</a>""", "entered"),
    "newsletter_only": ("""<h2>Get our newsletter</h2>
      <form><input type="email" name="email"><button>Subscribe</button></form>""", "no_form"),
    "listing_search_box": ("""<input type="search" placeholder="Search sweeps">
      <div class="card"><h3>Prize A</h3><a href="/form_page">Enter Here</a></div>
      <div class="card"><h3>Prize B</h3><a href="/form_page">Enter Here</a></div>""", "entered"),
    "expired": ("""<h1>Sweepstakes</h1><p>This sweepstakes has ended. Winners have been selected.</p>
      <form><input name="email"></form>""", "expired"),
    "login_wall": ("""<h1>Sweepstakes</h1><p>Please log in to enter.</p><a href="/login">Sign In</a>""", "no_form"),
    "multi_step": ("""<h1>Step 1</h1>
      <form action="/step2" method="get"><input name="first_name" required>
        <input name="last_name" required><button type="submit">Continue</button></form>""", "entered"),
}

EXTRA_PAGES = {
    "/thanks": "<h1>Thank you for entering!</h1><p>Good luck!</p>",
    "/form_page": """<h1>Entry Form</h1><form action="/thanks" method="get">
        <input name="first_name" required><input name="last_name" required>
        <input type="email" name="email" required><input name="city">
        <button type="submit">Enter to Win</button></form>""",
    "/step2": """<h1>Step 2</h1><form action="/thanks" method="get">
        <input type="email" name="email" required><input name="zip">
        <button type="submit">Submit Entry</button></form>""",
    "/login": "<h1>Login</h1><form><input name=user><input name=pass></form>",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        key = path.strip("/")
        if key in FIXTURES:
            body = FIXTURES[key][0]
        elif path in EXTRA_PAGES:
            body = EXTRA_PAGES[path]
        else:
            body = "Not found"
        out = f"<!doctype html><html><body>{body}</body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(out)


async def _run() -> int:
    httpd = socketserver.TCPServer(("127.0.0.1", 8899), _Handler)
    httpd.allow_reuse_address = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:8899"
    rows = []
    async with BrowserManager(headless=True) as bm:
        for name, (_html, expected) in FIXTURES.items():
            page = await bm.new_page()
            try:
                res = await enter_sweepstake(page, f"{base}/{name}", PROFILE, manual_captcha=False)
                status = res.get("status")
            except Exception as exc:  # noqa: BLE001
                status = f"EXC:{exc}"
            finally:
                await page.close()
            rows.append((name, status, expected, status == expected))
    httpd.shutdown()

    print(f"\n{'FIXTURE':<22} {'GOT':<10} {'EXPECT':<10} OK")
    print("-" * 55)
    passed = 0
    for name, status, expected, ok in rows:
        passed += ok
        print(f"{name:<22} {str(status):<10} {expected:<10} {'OK' if ok else 'FAIL'}")
    print("-" * 55)
    print(f"{passed}/{len(rows)} fixtures behaved as expected")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
