"""
CAPTCHA solving via third-party services.

Supported services:
  2captcha       — https://2captcha.com        (~$3/1000 solves)
  capsolver      — https://capsolver.com        (~$1.5/1000 solves)
  anticaptcha    — https://anti-captcha.com     (~$2/1000 solves)
  capmonster     — https://capmonster.cloud     (~$0.5/1000 solves)
  deathbycaptcha — https://deathbycaptcha.com   (~$1.39/1000 solves)
  azcaptcha      — https://azcaptcha.com        (~$1/1000 solves)
  ezcaptcha      — https://ez-captcha.com       (~$0.8/1000 solves)
  nextcaptcha    — https://nextcaptcha.com      (~$1/1000 solves)
  nocaptchaai    — https://nocaptchaai.com      (~$0.5/1000 solves)
  metabypass     — https://metabypass.tech      (~$2/1000 solves)
  nopecha        — https://nopecha.com          (~$5/1000 solves)
  yescaptcha     — https://yescaptcha.com       (~$1/1000 solves)

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

Service = Literal[
    "none", "2captcha", "capsolver", "anticaptcha", "capmonster",
    "deathbycaptcha", "azcaptcha", "ezcaptcha", "nextcaptcha",
    "nocaptchaai", "metabypass", "nopecha", "yescaptcha",
]

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
        if self.service == "azcaptcha":
            return self._2captcha_compat("https://azcaptcha.com", sitekey, page_url, "userrecaptcha")
        if self.service == "anticaptcha":
            return self._anticaptcha_compat("https://api.anti-captcha.com", sitekey, page_url, "NoCaptchaTaskProxyless")
        if self.service == "capmonster":
            return self._anticaptcha_compat("https://api.capmonster.cloud", sitekey, page_url, "NoCaptchaTaskProxyless")
        if self.service == "ezcaptcha":
            return self._anticaptcha_compat("https://api.ez-captcha.com", sitekey, page_url, "ReCaptchaV2TaskProxyless")
        if self.service == "nextcaptcha":
            return self._anticaptcha_compat("https://api.nextcaptcha.com", sitekey, page_url, "RecaptchaV2TaskProxyless")
        if self.service == "yescaptcha":
            return self._anticaptcha_compat("https://api.yescaptcha.com", sitekey, page_url, "NoCaptchaTaskProxyless")
        if self.service == "deathbycaptcha":
            return self._deathbycaptcha(sitekey, page_url, captcha_type=4)
        if self.service == "nocaptchaai":
            return self._nocaptchaai(sitekey, page_url, method="recaptcha")
        if self.service == "metabypass":
            return self._metabypass(sitekey, page_url, captcha_type="recaptcha_v2")
        if self.service == "nopecha":
            return self._nopecha(sitekey, page_url, captcha_type="recaptcha2")
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
        if self.service == "azcaptcha":
            return self._2captcha_compat("https://azcaptcha.com", sitekey, page_url, "hcaptcha")
        if self.service == "anticaptcha":
            return self._anticaptcha_compat("https://api.anti-captcha.com", sitekey, page_url, "HCaptchaTaskProxyless")
        if self.service == "capmonster":
            return self._anticaptcha_compat("https://api.capmonster.cloud", sitekey, page_url, "HCaptchaTaskProxyless")
        if self.service == "ezcaptcha":
            return self._anticaptcha_compat("https://api.ez-captcha.com", sitekey, page_url, "HCaptchaTaskProxyless")
        if self.service == "nextcaptcha":
            return self._anticaptcha_compat("https://api.nextcaptcha.com", sitekey, page_url, "HCaptchaTaskProxyless")
        if self.service == "yescaptcha":
            return self._anticaptcha_compat("https://api.yescaptcha.com", sitekey, page_url, "HCaptchaTaskProxyless")
        if self.service == "deathbycaptcha":
            return self._deathbycaptcha(sitekey, page_url, captcha_type=7)
        if self.service == "nocaptchaai":
            return self._nocaptchaai(sitekey, page_url, method="hcaptcha")
        if self.service == "metabypass":
            return self._metabypass(sitekey, page_url, captcha_type="hcaptcha")
        if self.service == "nopecha":
            return self._nopecha(sitekey, page_url, captcha_type="hcaptcha")
        return None

    def check_balance(self) -> float | None:
        """Return account balance in USD (approximate), or None on error."""
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
            elif self.service == "azcaptcha":
                r = self._session.get(
                    "https://azcaptcha.com/res.php",
                    params={"key": self.api_key, "action": "getbalance", "json": 1},
                    timeout=15,
                )
                data = r.json()
                if data.get("status") == 1:
                    return float(data["request"])
            elif self.service in ("capsolver", "ezcaptcha"):
                base = "https://api.capsolver.com" if self.service == "capsolver" else "https://api.ez-captcha.com"
                r = self._session.post(
                    f"{base}/getBalance",
                    json={"clientKey": self.api_key},
                    timeout=15,
                )
                data = r.json()
                if data.get("errorId") == 0:
                    return float(data.get("balance", 0))
            elif self.service in ("anticaptcha", "capmonster", "nextcaptcha", "yescaptcha"):
                _urls = {
                    "anticaptcha": "https://api.anti-captcha.com/getBalance",
                    "capmonster":  "https://api.capmonster.cloud/getBalance",
                    "nextcaptcha": "https://api.nextcaptcha.com/getBalance",
                    "yescaptcha":  "https://api.yescaptcha.com/getBalance",
                }
                r = self._session.post(
                    _urls[self.service],
                    json={"clientKey": self.api_key},
                    timeout=15,
                )
                data = r.json()
                if data.get("errorId") == 0:
                    return float(data.get("balance", 0))
            elif self.service == "deathbycaptcha":
                r = self._session.post(
                    "https://api.dbcapi.me/api/user",
                    data={"authtoken": self.api_key},
                    timeout=15,
                )
                data = r.json()
                if "balance" in data:
                    # balance is in credits; 1000 credits ≈ $1
                    return float(data["balance"]) / 1000
            elif self.service == "nocaptchaai":
                r = self._session.get(
                    "https://api.nocaptchaai.com/balance",
                    params={"key": self.api_key},
                    timeout=15,
                )
                data = r.json()
                if data.get("status") == "success":
                    return float(data.get("balance", 0))
            elif self.service == "metabypass":
                r = self._session.get(
                    "https://app.metabypass.tech/CaptchaSolver/api/v1/account",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=15,
                )
                data = r.json()
                if "balance" in data:
                    return float(data["balance"])
            elif self.service == "nopecha":
                r = self._session.get(
                    "https://api.nopecha.com/status",
                    params={"key": self.api_key},
                    timeout=15,
                )
                data = r.json()
                if data.get("error") == 0:
                    return float(data.get("data", {}).get("credit", 0))
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

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _2captcha_compat(self, base_url: str, sitekey: str, page_url: str, method: str) -> str | None:
        """2captcha-compatible API: POST /in.php to submit, GET /res.php to poll."""
        try:
            resp = self._session.post(
                f"{base_url}/in.php",
                data={
                    "key":       self.api_key,
                    "method":    method,
                    "googlekey": sitekey,
                    "sitekey":   sitekey,  # some services use this param name
                    "pageurl":   page_url,
                    "json":      1,
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("status") != 1:
                logger.warning("%s submit error: %s", self.service, data)
                return None
            captcha_id = data["request"]
        except Exception as exc:
            logger.error("%s submit exception: %s", self.service, exc)
            return None

        for attempt in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                resp = self._session.get(
                    f"{base_url}/res.php",
                    params={"key": self.api_key, "action": "get", "id": captcha_id, "json": 1},
                    timeout=30,
                )
                data = resp.json()
                if data.get("status") == 1:
                    logger.info("%s solved after %d polls", self.service, attempt + 1)
                    return str(data["request"])
                if data.get("request") not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                    logger.warning("%s poll error: %s", self.service, data)
                    return None
            except Exception as exc:
                logger.warning("%s poll exception: %s", self.service, exc)

        logger.warning("%s timed out after %d polls", self.service, _MAX_POLLS)
        return None

    def _anticaptcha_compat(self, base_url: str, sitekey: str, page_url: str, task_type: str) -> str | None:
        """Anti-captcha-compatible API: POST /createTask, poll /getTaskResult."""
        try:
            resp = self._session.post(
                f"{base_url}/createTask",
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
                logger.warning("%s submit error: %s", self.service, data)
                return None
            task_id = data["taskId"]
        except Exception as exc:
            logger.error("%s submit exception: %s", self.service, exc)
            return None

        for attempt in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                resp = self._session.post(
                    f"{base_url}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                    timeout=30,
                )
                data = resp.json()
                if data.get("status") == "ready":
                    solution = data.get("solution", {})
                    token = solution.get("gRecaptchaResponse") or solution.get("token")
                    if not token:
                        logger.warning("%s returned no token in solution: %s", self.service, solution)
                        return None
                    logger.info("%s solved after %d polls", self.service, attempt + 1)
                    return token
                if data.get("errorId", 0) != 0:
                    logger.warning("%s poll error: %s", self.service, data)
                    return None
            except Exception as exc:
                logger.warning("%s poll exception: %s", self.service, exc)

        logger.warning("%s timed out after %d polls", self.service, _MAX_POLLS)
        return None

    # ── DeathByCaptcha ────────────────────────────────────────────────────────

    def _deathbycaptcha(self, sitekey: str, page_url: str, captcha_type: int) -> str | None:
        """DeathByCaptcha authtoken API. captcha_type: 4=reCAPTCHA v2, 7=hCaptcha."""
        try:
            resp = self._session.post(
                "https://api.dbcapi.me/api/",
                data={
                    "authtoken": self.api_key,
                    "type":      captcha_type,
                    "googlekey": sitekey,
                    "pageurl":   page_url,
                },
                timeout=30,
            )
            data = resp.json()
            captcha_id = data.get("captcha")
            if not captcha_id:
                logger.warning("DeathByCaptcha submit error: %s", data)
                return None
        except Exception as exc:
            logger.error("DeathByCaptcha submit exception: %s", exc)
            return None

        for attempt in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                resp = self._session.get(
                    f"https://api.dbcapi.me/api/captcha/{captcha_id}",
                    params={"authtoken": self.api_key},
                    timeout=30,
                )
                data = resp.json()
                if data.get("status") == 1 and data.get("text"):
                    logger.info("DeathByCaptcha solved after %d polls", attempt + 1)
                    return str(data["text"])
                if data.get("is_correct") == 0:
                    logger.warning("DeathByCaptcha marked captcha incorrect")
                    return None
            except Exception as exc:
                logger.warning("DeathByCaptcha poll exception: %s", exc)

        logger.warning("DeathByCaptcha timed out after %d polls", _MAX_POLLS)
        return None

    # ── NoCaptchaAI ──────────────────────────────────────────────────────────

    def _nocaptchaai(self, sitekey: str, page_url: str, method: str) -> str | None:
        """NoCaptchaAI API. method: 'recaptcha' or 'hcaptcha'."""
        try:
            resp = self._session.post(
                "https://api.nocaptchaai.com/solve",
                json={
                    "key":     self.api_key,
                    "method":  method,
                    "sitekey": sitekey,
                    "url":     page_url,
                },
                timeout=30,
            )
            data = resp.json()
            # some responses solve immediately
            if data.get("status") == "solved" and data.get("text"):
                logger.info("NoCaptchaAI solved immediately")
                return str(data["text"])
            task_id = data.get("id")
            if not task_id:
                logger.warning("NoCaptchaAI submit error: %s", data)
                return None
        except Exception as exc:
            logger.error("NoCaptchaAI submit exception: %s", exc)
            return None

        for attempt in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                resp = self._session.get(
                    "https://api.nocaptchaai.com/solve",
                    params={"key": self.api_key, "id": task_id},
                    timeout=30,
                )
                data = resp.json()
                if data.get("status") == "solved" and data.get("text"):
                    logger.info("NoCaptchaAI solved after %d polls", attempt + 1)
                    return str(data["text"])
                if data.get("status") not in ("new", "processing", "solved"):
                    logger.warning("NoCaptchaAI poll error: %s", data)
                    return None
            except Exception as exc:
                logger.warning("NoCaptchaAI poll exception: %s", exc)

        logger.warning("NoCaptchaAI timed out after %d polls", _MAX_POLLS)
        return None

    # ── MetaBypass ────────────────────────────────────────────────────────────

    def _metabypass(self, sitekey: str, page_url: str, captcha_type: str) -> str | None:
        """MetaBypass synchronous API. captcha_type: 'recaptcha_v2' or 'hcaptcha'."""
        try:
            resp = self._session.post(
                "https://app.metabypass.tech/CaptchaSolver/api/v1/services/captchaSolver",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "captcha_type": captcha_type,
                    "data": {
                        "googlekey": sitekey,
                        "url":       page_url,
                    },
                },
                timeout=120,  # synchronous solve — they wait up to ~2 min
            )
            data = resp.json()
            token = (data.get("data") or {}).get("solution")
            if token:
                logger.info("MetaBypass solved")
                return str(token)
            logger.warning("MetaBypass error: %s", data)
        except Exception as exc:
            logger.error("MetaBypass exception: %s", exc)
        return None

    # ── NoPeCha ───────────────────────────────────────────────────────────────

    def _nopecha(self, sitekey: str, page_url: str, captcha_type: str) -> str | None:
        """NoPeCha API. captcha_type: 'recaptcha2' or 'hcaptcha'."""
        try:
            resp = self._session.post(
                "https://api.nopecha.com/",
                json={
                    "key":     self.api_key,
                    "type":    captcha_type,
                    "sitekey": sitekey,
                    "url":     page_url,
                },
                timeout=30,
            )
            data = resp.json()
            if data.get("error", 1) != 0:
                logger.warning("NoPeCha submit error: %s", data)
                return None
            job_id = data.get("data")
            if not job_id:
                logger.warning("NoPeCha returned no job_id: %s", data)
                return None
        except Exception as exc:
            logger.error("NoPeCha submit exception: %s", exc)
            return None

        for attempt in range(_MAX_POLLS):
            time.sleep(_POLL_INTERVAL)
            try:
                resp = self._session.get(
                    "https://api.nopecha.com/",
                    params={"key": self.api_key, "id": job_id},
                    timeout=30,
                )
                data = resp.json()
                if data.get("error") == 0:
                    tokens = data.get("data")
                    if isinstance(tokens, list) and tokens:
                        logger.info("NoPeCha solved after %d polls", attempt + 1)
                        return str(tokens[0])
                    return None
                # error=1 means still processing; anything else is a real error
                if data.get("error", 1) not in (0, 1):
                    logger.warning("NoPeCha poll error: %s", data)
                    return None
            except Exception as exc:
                logger.warning("NoPeCha poll exception: %s", exc)

        logger.warning("NoPeCha timed out after %d polls", _MAX_POLLS)
        return None
