"""最小的 JSON-RPC 客戶端(僅標準庫)。只做唯讀呼叫,不含任何簽章或送交易的功能。"""
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_RPC = "https://mainnet.base.org"
_RETRY_HTTP = (429, 500, 502, 503, 504)


class RpcError(Exception):
    pass


class Rpc:
    def __init__(self, url=None, timeout=20.0, retries=3):
        self.url = url or os.environ.get("BASE_RPC_URL") or DEFAULT_RPC
        if urlparse(self.url).scheme not in ("http", "https"):
            raise ValueError("RPC 網址必須是 http(s)")
        self.timeout = timeout
        self.retries = retries

    @property
    def host(self):
        # 錯誤訊息只顯示主機名稱:付費節點的網址常夾帶 API 金鑰,不能外洩到輸出
        return urlparse(self.url).netloc.split("@")[-1]

    def call(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        last = "未知錯誤"
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(
                    self.url,
                    data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "defi-grid/0.1"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.load(resp)
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code not in _RETRY_HTTP:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                last = type(e).__name__  # 不用 str(e):訊息可能含完整網址
            else:
                if "error" in data:
                    err = data["error"]
                    raise RpcError(f"{method} 回傳錯誤 {err.get('code')}: {err.get('message')}")
                if "result" not in data:
                    raise RpcError(f"{method} 的回應缺少 result")
                return data["result"]
            time.sleep(1.5 * (attempt + 1))
        raise RpcError(f"{self.host} 呼叫 {method} 失敗: {last}")

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": data}, block])
