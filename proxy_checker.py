"""
Proxy Checker - Valida proxies y muestra IP, país, latencia
Uso:
  python proxy_checker.py                          # Prueba el proxy de braintree_config.json
  python proxy_checker.py --url http://proxy:8080  # Prueba un proxy específico
  python proxy_checker.py --file proxies.txt       # Prueba múltiples proxies desde archivo
  python proxy_checker.py --direct                 # Prueba conexión directa (sin proxy)
"""

import json, requests, sys, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any

TEST_URLS = [
    ("https://httpbin.org/ip", "json"),
    ("https://api.ipify.org?format=json", "json"),
    ("http://ip-api.com/json/", "json"),
    ("https://ifconfig.me/all.json", "json"),
]

def load_config_proxy() -> str:
    """Extrae la URL del proxy desde braintree_config.json"""
    cfg_path = "braintree_config.json"
    if not os.path.exists(cfg_path):
        return ""
    with open(cfg_path, "r") as f:
        cfg = json.load(f)
    apify = cfg.get("proxy", {}).get("apify", {})
    if apify.get("enabled"):
        import uuid
        return apify["url_template"].format(
            country="US",
            session=uuid.uuid4().hex,
            password=apify["password"]
        )
    fallbacks = cfg.get("proxy", {}).get("fallback_proxies", [])
    return fallbacks[0] if fallbacks else ""


def test_single_proxy(proxy_url: str, timeout: int = 15) -> Dict[str, Any]:
    """Prueba un proxy y retorna resultados detallados."""
    result = {
        "proxy": proxy_url,
        "working": False,
        "ip": "",
        "country": "",
        "isp": "",
        "latency_ms": 0,
        "error": ""
    }

    proxies = {"http": proxy_url, "https": proxy_url}
    start = time.time()

    for url, mode in TEST_URLS:
        try:
            r = requests.get(
                url,
                proxies=proxies,
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json"
                }
            )
            if r.status_code != 200:
                continue

            elapsed = (time.time() - start) * 1000
            result["latency_ms"] = round(elapsed, 1)

            data = r.json()
            # httpbin
            if "origin" in data:
                result["ip"] = data["origin"].split(", ")[0]
            # ipify
            elif "ip" in data:
                result["ip"] = data["ip"]
            # ip-api
            elif "query" in data:
                result["ip"] = data["query"]
                result["country"] = data.get("country", "")
                result["isp"] = data.get("isp", "")
            # ifconfig
            elif "ip_addr" in data:
                result["ip"] = data["ip_addr"]

            # Extraer país de cualquier fuente disponible
            if not result["country"]:
                result["country"] = data.get("country", "") or data.get("countryCode", "") or data.get("country_name", "")

            result["working"] = True
            return result

        except requests.exceptions.ConnectTimeout:
            result["error"] = "Connection timeout"
        except requests.exceptions.ProxyError as e:
            result["error"] = f"Proxy error: {e}"
        except requests.exceptions.SSLError as e:
            result["error"] = f"SSL error: {e}"
        except Exception as e:
            result["error"] = str(e)[:120]

    if not result["error"]:
        result["error"] = "All test endpoints failed"
    return result


def test_direct(timeout: int = 15) -> Dict[str, Any]:
    """Prueba conexión directa (sin proxy)."""
    return test_single_proxy("", timeout)


def load_proxies_from_file(path: str) -> List[str]:
    """Carga proxies desde archivo (uno por línea)."""
    proxies = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("//"):
                # Formato: http://user:pass@host:port
                if not line.startswith("http"):
                    line = f"http://{line}"
                proxies.append(line)
    return proxies


def print_result(result: Dict[str, Any], index: int = 0):
    """Imprime resultado formateado."""
    status = "✓" if result["working"] else "✗"
    color = "\033[92m" if result["working"] else "\033[91m"
    reset = "\033[0m"

    print(f"\n{color}{status} [{index}]{reset}")
    print(f"  Proxy:    {result['proxy'][:80]}{'...' if len(result['proxy']) > 80 else ''}")
    if result["working"]:
        print(f"  IP:       {result['ip']}")
        print(f"  Country:  {result['country']}")
        print(f"  ISP:      {result['isp']}")
        print(f"  Latency:  {result['latency_ms']}ms")
    else:
        print(f"  Error:    {result['error']}")


def main():
    proxies_to_test = []

    # Parse arguments
    args = sys.argv[1:]

    if "--direct" in args:
        print("Testing direct connection (no proxy)...")
        result = test_direct()
        print_result(result, 0)
        return

    if "--url" in args:
        idx = args.index("--url")
        if idx + 1 < len(args):
            proxies_to_test.append(args[idx + 1])

    if "--file" in args:
        idx = args.index("--file")
        if idx + 1 < len(args):
            file_proxies = load_proxies_from_file(args[idx + 1])
            proxies_to_test.extend(file_proxies)
            print(f"Loaded {len(file_proxies)} proxies from {args[idx + 1]}")

    # Default: load from config
    if not proxies_to_test:
        cfg_proxy = load_config_proxy()
        if cfg_proxy:
            proxies_to_test.append(cfg_proxy)
            print("Loaded proxy from braintree_config.json")
        else:
            print("No proxy found in config. Use --url, --file, or --direct")
            print("\nUsage:")
            print("  python proxy_checker.py                          # Config proxy")
            print("  python proxy_checker.py --url http://proxy:8080  # Specific proxy")
            print("  python proxy_checker.py --file proxies.txt       # Batch from file")
            print("  python proxy_checker.py --direct                 # No proxy")
            return

    # Test
    if len(proxies_to_test) == 1:
        print(f"\nTesting proxy...")
        result = test_single_proxy(proxies_to_test[0])
        print_result(result, 0)
    else:
        print(f"\nTesting {len(proxies_to_test)} proxies (concurrent)...")
        with ThreadPoolExecutor(max_workers=min(20, len(proxies_to_test))) as ex:
            futures = {ex.submit(test_single_proxy, p): i for i, p in enumerate(proxies_to_test)}
            results = []
            for f in as_completed(futures):
                idx = futures[f]
                try:
                    res = f.result()
                    res["_idx"] = idx
                    results.append(res)
                except Exception as e:
                    results.append({"proxy": proxies_to_test[idx], "working": False, "error": str(e), "_idx": idx})

        # Sort: working first, then by latency
        results.sort(key=lambda x: (not x["working"], x.get("latency_ms", 9999)))

        working = sum(1 for r in results if r["working"])
        print(f"\n{'='*60}")
        print(f"  Results: {working}/{len(results)} working")
        print(f"{'='*60}")

        for r in results:
            print_result(r, r["_idx"])


if __name__ == "__main__":
    main()