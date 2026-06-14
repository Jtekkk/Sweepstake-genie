"""
CAPTCHA solving via third-party services.

Supported services:
  2captcha   — https://2captcha.com  (~$3/1000 solves)
  capsolver  — https://capsolver.com (~$1.5/1000 solves)

Usage:
    solver = CaptchaSolver(service="2captcha", api_key="YOUR_KEY")
    token = solver.solve_recaptcha(sitekey="...", page_url="https://...")
    # token is the g-recaptcha-response string to inject into the page
"""

from __future__ import annotations

import logging
import time
from typing import Literal

import requests

logger = logging.getLogger(__name__)

Service = Literal["none", "2captcha", "capsolver"]

_POLL_INTERVAL = 5   # seconds between status polls
_MAX_POLLS     = 24  # max 2 minutes total wait


class CaptchaSolver:
    def __init__(self, service: Service | None = "none", api_key: str = "") -> None:
        self.service  = (service or "none").lower()
        self.api_key  = api_key or ""
        self._session = requests.Session()

    @property
    def enabled(self) -> bool:
        return self.service != "none" and bool(self.api_key)

    # ── Public API ────────────────────────────────────────────────────────────

    def solve_recaptcha(self, sitekey: str, page_url: str) -> str | None:
        """Solve reCAPTCHA v2 and return the g-recaptcha-response token."""
        if not self.enabled:
            return None
        logger.info("Solving reCAPTCHA via %s for %s", self.service, page_url)
        if self.service == "2captcha":
            return self._2captcha(sitekey, page_url, method="userrecaptcha")
        if self.service == "capsolver":
            return self._capsolver(sitekey, page_url, task_type="ReCaptchaV2TaskProxyLess")
        return None

    def solve_hcaptcha(self, sitekey: str, page_url: str) -> str | None:
        """Solve hCaptcha and return the response token."""
        if not self.enabled:
            return None
        logger.info("Solving hCaptcha via %s for %s", self.service, page_url)
        if self.service == "2captcha":
            return self._2captcha(sitekey, page_url, method="hcaptcha")
        if self.service == "capsolver":
            return self._capsolver(sitekey, page_url, task_type="HCaptchaTaskProxyLess")
        return None

    def check_balance(self) -> float | None:
        """Return account balance in USD, or None on error."""
        try:
            if self.service == "2captcha":
                r = self._session.get(
                    "https://2captcha.com/res.php",
                    params={"key": self.api_key, "action": "getbalance", "json": 1},
                    timeout=15,
                )
                data = r.json()
                if data.get("status") == 1:
                    return float(data["request"])
            elif self.service == "capsolver":
                r = self._session.post(
                    "https://api.capsolver.com/getBalance",
                    json={"clientKey": self.api_key},
                    timeout=15,
                )
                data = r.json()
                if data.get("errorId") == 0:
                    return float(data.get("balance", 0))
        except Exception as exc:
            logger.warning("Balance check failed: %s", exc)
        return None

    # ── 2captcha ──────────────────────────────────────────────────────────────

    def _2captcha(self, sitekey: str, page_url: str, method: str) -> str | None:
        try:
            resp = self._session.post(
                "https://2captcha.com/in.php",
                data={
                    "key":       self.api_key,
                    "method":    method,
                    "googlekey": sitekey,
                    "pageurl":   page_url,
                    "json":      1,
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("status") != 1:
                logger.warning("2captcha submit error: %s", data)
                return None
            captcha_id = data["request"]
        except Exception as exc:
            logger.error("2captcha submit exception: %s", exc)
            return None

        for attempt in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                resp = self._session.get(
                    "https://2captcha.com/res.php",
                    params={"key": self.api_key, "action": "get", "id": captcha_id, "json": 1},
                    timeout=30,
                )
                data = resp.json()
                if data.get("status") == 1:
                    logger.info("2captcha solved after %d polls", attempt + 1)
                    return str(data["request"])
                if data.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                    logger.warning("2captcha poll error: %s", data)
                    return None
            except Exception as exc:
                logger.warning("2captcha poll exception: %s", exc)

        logger.warning("2captcha timed out after %d polls", _MAX_POLLS)
        return None

    # ── CapSolver ─────────────────────────────────────────────────────────────

    def _capsolver(self, sitekey: str, page_url: str, task_type: str) -> str | None:
        try:
            resp = self._session.post(
                "https://api.capsolver.com/createTask",
                json={
                    "clientKey": self.api_key,
                    "task": {
                        "type":       task_type,
                        "websiteURL": page_url,
                        "websiteKey": sitekey,
                    },
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("errorId", 1) != 0:
                logger.warning("CapSolver submit error: %s", data)
                return None
            task_id = data["taskId"]
        except Exception as exc:
            logger.error("CapSolver submit exception: %s", exc)
            return None

        for attempt in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                resp = self._session.post(
                    "https://api.capsolver.com/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                    timeout=30,
                )
                data = resp.json()
                if data.get("status") == "ready":
                    solution = data.get("solution", {})
                    token = solution.get("gRecaptchaResponse")
                    if not token:
                        logger.warning("CapSolver returned no token in solution: %s", solution)
                        return None
                    logger.info("CapSolver solved after %d polls", attempt + 1)
                    return token
                if data.get("errorId", 0) != 0:
                    logger.warning("CapSolver poll error: %s", data)
                    return None
            except Exception as exc:
                logger.warning("CapSolver poll exception: %s", exc)

        logger.warning("CapSolver timed out after %d polls", _MAX_POLLS)
        return None
