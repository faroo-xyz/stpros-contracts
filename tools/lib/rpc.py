"""Read-only JSON-RPC client for the public Pharos endpoints.

- Only read methods are permitted (`READ_METHODS`); any attempt to call a
  state-changing or signing method raises before a request is made.
- Requests are rate limited (minimum interval between HTTP requests) and
  retried with exponential backoff on HTTP 429/5xx and on the provider's
  "cu limit exceeded" error (-32011).
- JSON-RPC batches are supported; failed batch items are retried
  individually in later batches, never silently dropped.
"""

import json
import time
import urllib.error
import urllib.request

PUBLIC_RPC = {
    "mainnet": "https://rpc.pharos.xyz",
    "testnet": "https://atlantic.dplabs-internal.com",
}

READ_METHODS = {
    "eth_blockNumber", "eth_chainId", "eth_call", "eth_getCode", "eth_getStorageAt",
    "eth_getBalance", "eth_getLogs", "eth_getBlockByNumber", "eth_getBlockByHash",
    "eth_getTransactionByHash", "eth_getTransactionReceipt", "eth_estimateGas",
    "eth_getProof", "eth_gasPrice", "net_version", "web3_clientVersion",
}

RETRYABLE_RPC_CODES = {-32011, -32603, 429}
USER_AGENT = "faroo-readonly-verification"


class RpcError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Client:
    def __init__(self, chain, url=None, min_interval=0.35, timeout=60, max_retries=8, verbose=False,
                 transport_retries=None):
        self.chain = chain
        self.url = url or PUBLIC_RPC[chain]
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        # Dropped connections on oversized responses do not heal by waiting; a
        # caller can fail fast on them and retry with a smaller request.
        self.transport_retries = max_retries if transport_retries is None else transport_retries
        self.verbose = verbose
        self._last = 0.0
        self._id = 0
        self.requests = 0
        self.retries = 0

    # -- transport -----------------------------------------------------
    def _throttle(self):
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _post(self, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        delay = 1.0
        transport_failures = 0
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.requests += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                body_txt = e.read()[:300].decode(errors="replace")
                retryable = e.code == 429 or e.code >= 500
                reason = f"HTTP {e.code}: {body_txt[:120]}"
                if not retryable or attempt == self.max_retries:
                    raise RpcError(f"HTTP {e.code}: {body_txt}", code=e.code)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                reason = f"transport error: {e}"
                transport_failures += 1
                if attempt == self.max_retries or transport_failures > self.transport_retries:
                    raise RpcError(reason)
            self.retries += 1
            if self.verbose:
                print(f"  [rpc {self.chain}] retry {attempt + 1} in {delay:.0f}s ({reason})", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise RpcError("unreachable")

    def _next_id(self):
        self._id += 1
        return self._id

    @staticmethod
    def _check_method(method):
        if method not in READ_METHODS:
            raise RpcError(f"method {method} is not an allowed read-only method")

    @staticmethod
    def _responses(response, expected_ids):
        """Validate the entire response before accepting results or retrying errors."""
        if not isinstance(response, list):
            raise RpcError("invalid JSON-RPC response collection")
        seen = set()
        for item in response:
            if not isinstance(item, dict) or item.get("jsonrpc") != "2.0":
                raise RpcError("invalid JSON-RPC envelope")
            rid = item.get("id")
            if type(rid) is not int or rid not in expected_ids or rid in seen:
                raise RpcError("wrong, unknown or duplicate JSON-RPC response id")
            seen.add(rid)
            if ("result" in item) == ("error" in item):
                raise RpcError("ambiguous or missing JSON-RPC result/error")
            if "error" in item:
                error = item["error"]
                if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                    raise RpcError("invalid JSON-RPC error")
        if seen != set(expected_ids):
            raise RpcError("missing JSON-RPC responses")
        return response

    # -- single call ---------------------------------------------------
    def call(self, method, params):
        self._check_method(method)
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            rid = self._next_id()
            r = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            self._responses([r], {rid})
            if "error" not in r:
                return r["result"]
            err = r["error"]
            if err.get("code") in RETRYABLE_RPC_CODES and attempt < self.max_retries:
                self.retries += 1
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise RpcError(f"{method}: {err.get('message')}", code=err.get("code"))
        raise RpcError("unreachable")

    # -- batch ---------------------------------------------------------
    def batch(self, calls):
        """calls: list of (method, params). Returns list of results in order.

        Items failing with a retryable error are re-sent; a non-retryable error
        raises RpcError carrying the failing index.
        """
        for m, _ in calls:
            self._check_method(m)
        if not calls:
            return []
        results = [None] * len(calls)
        pending = list(range(len(calls)))
        delay = 1.0
        for attempt in range(self.max_retries + 1):
            body = []
            ids = {}
            for i in pending:
                rid = self._next_id()
                ids[rid] = i
                body.append({"jsonrpc": "2.0", "id": rid, "method": calls[i][0], "params": calls[i][1]})
            resp = self._post(body)
            self._responses(resp, ids)
            failed = []
            for item in resp:
                i = ids[item["id"]]
                if "error" in item:
                    code = item["error"].get("code")
                    if code in RETRYABLE_RPC_CODES:
                        failed.append(i)
                    else:
                        e = RpcError(f"{calls[i][0]}: {item['error'].get('message')}", code=code)
                        e.index = i
                        raise e
                else:
                    results[i] = item["result"]
            if not failed:
                return results
            pending = failed
            if attempt == self.max_retries:
                break
            self.retries += 1
            if self.verbose:
                print(f"  [rpc {self.chain}] {len(failed)} batch items retried in {delay:.0f}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 60)
        raise RpcError(f"batch items still failing after retries: {pending}")

    # -- conveniences --------------------------------------------------
    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)

    def chain_id(self):
        return int(self.call("eth_chainId", []), 16)

    def eth_call(self, to, data, block):
        return self.call("eth_call", [{"to": to, "data": data}, block_tag(block)])

    def get_code(self, addr, block):
        return self.call("eth_getCode", [addr, block_tag(block)])

    def get_storage(self, addr, slot, block):
        return self.call("eth_getStorageAt", [addr, slot, block_tag(block)])


def block_tag(block):
    if isinstance(block, int):
        return hex(block)
    return block
