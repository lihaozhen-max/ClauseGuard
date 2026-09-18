"""极简 Chrome DevTools Protocol 客户端（**只用标准库**，不引入任何新依赖）。

为什么自己写而不是上 Playwright：本项目对新增依赖有明确门槛（见 ``docs/M6-验收记录.md`` §3.1），
而这里需要的只是"打开页面 → 点一下 → 读一段文本"，用 CDP 的
``Target.createTarget`` + ``Runtime.evaluate`` 两个命令就够了。

组成：

- :class:`WebSocket` —— RFC 6455 客户端帧的最小实现（手写握手、掩码、分片与 ping/pong）；
- :class:`Chrome` —— 拉起 headless Chrome、找到 browser 级 WebSocket 地址、开标签页；
- :class:`Page` —— ``eval``（求值 JS）与 ``wait_for``（轮询直到为真），供用例直接使用。

协议参考：https://chromedevtools.github.io/devtools-protocol/
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

#: 常见的 Chrome / Edge 安装位置（Windows）。Edge 是 Chromium 内核，同样能跑。
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)


def find_browser() -> str | None:
    """返回可用的 Chromium 系浏览器可执行文件路径（找不到则 ``None``）。"""
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("chrome", "chromium", "google-chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    """够用即止的 WebSocket 客户端（只支持 CDP 用到的那部分）。"""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws":
            raise WebSocketError(f"只支持 ws:// ，收到 {url}")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._handshake()
        self._next_id = 0

    # ── 握手 ───────────────────────────────────────────────────────────
    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode())
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("握手期间连接被关闭")
            buffer += chunk
        status_line = buffer.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status_line:
            raise WebSocketError(f"握手失败：{status_line}")

    # ── 收发 ───────────────────────────────────────────────────────────
    def _recv_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise WebSocketError("连接被关闭")
            data += chunk
        return data

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read_message(self) -> str:
        chunks: list[bytes] = []
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x9:  # ping → pong
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise WebSocketError("服务端关闭了 WebSocket")
            chunks.append(payload)
            if first & 0x80:  # FIN
                return b"".join(chunks).decode("utf-8", errors="replace")

    # ── CDP 调用 ───────────────────────────────────────────────────────
    def call(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> Any:
        """发一条 CDP 命令并等它的应答（事件一律丢弃）。"""
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        self._send_frame(json.dumps(message).encode())

        while True:
            data = json.loads(self._read_message())
            if data.get("id") != self._next_id:
                continue  # 事件（如 Page.loadEventFired）直接忽略
            if "error" in data:
                raise WebSocketError(f"{method} 失败：{data['error']}")
            return data.get("result")

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Chrome:
    """一个 headless Chrome 进程 + 一个标签页会话。"""

    def __init__(self, window: tuple[int, int] = (1760, 1080)) -> None:
        binary = find_browser()
        if binary is None:
            raise WebSocketError("未找到 Chrome/Edge")
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="clauseguard-cdp-")
        self.process = subprocess.Popen(  # noqa: S603 - 路径来自固定候选列表
            [
                binary,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                f"--window-size={window[0]},{window[1]}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ws = WebSocket(self._ws_url(), timeout=60.0)
        self.session_id: str | None = None

    def _ws_url(self, timeout: float = 30.0) -> str:
        import httpx

        deadline = time.time() + timeout
        last: Exception | None = None
        while time.time() < deadline:
            try:
                payload = httpx.get(f"http://127.0.0.1:{self.port}/json/version", timeout=2.0).json()
                url = payload.get("webSocketDebuggerUrl")
                if url:
                    return url
            except Exception as exc:  # noqa: BLE001 - 启动期连接失败是常态，重试即可
                last = exc
            time.sleep(0.2)
        raise WebSocketError(f"Chrome 调试端口未就绪：{last}")

    def open(self, url: str) -> "Page":
        target = self.ws.call("Target.createTarget", {"url": url})
        attached = self.ws.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        self.session_id = attached["sessionId"]
        self.ws.call("Page.enable", session_id=self.session_id)
        self.ws.call("Runtime.enable", session_id=self.session_id)
        return Page(self.ws, self.session_id)

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


class Page:
    """标签页：``eval`` 求值、``wait_for`` 轮询、``navigate`` 跳转。"""

    def __init__(self, ws: WebSocket, session_id: str) -> None:
        self.ws = ws
        self.session_id = session_id

    def eval(self, expression: str, timeout: float = 60.0) -> Any:
        """在页面里求值并取回 JSON 化的结果（``awaitPromise`` 打开，可直接用 Promise）。"""
        result = self.ws.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            session_id=self.session_id,
        )
        if result.get("exceptionDetails"):
            raise WebSocketError(f"页面 JS 抛错：{result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def text(self) -> str:
        return str(self.eval("document.body ? document.body.innerText : ''") or "")

    def navigate(self, url: str, settle: float = 1.0) -> None:
        self.ws.call("Page.navigate", {"url": url}, session_id=self.session_id)
        time.sleep(settle)

    def wait_for(self, expression: str, *, timeout: float = 30.0, interval: float = 0.25) -> bool:
        """轮询直到 ``expression`` 为真；超时返回 ``False``（由调用方决定怎么报错）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.eval(expression):
                    return True
            except WebSocketError:
                pass
            time.sleep(interval)
        return False

    def wait_for_text(self, needle: str, *, timeout: float = 30.0) -> bool:
        return self.wait_for(
            f"document.body && document.body.innerText.includes({json.dumps(needle, ensure_ascii=False)})",
            timeout=timeout,
        )

    def click_by_text(self, text: str, *, selector: str = "button", timeout: float = 15.0) -> bool:
        """找到文本包含 ``text`` 的元素并点击（先滚动到可见处）。"""
        expression = f"""
        (() => {{
          const nodes = [...document.querySelectorAll({json.dumps(selector)})];
          const node = nodes.find((n) => (n.textContent || '').includes({json.dumps(text, ensure_ascii=False)}));
          if (!node) return false;
          node.scrollIntoView({{block: 'center'}});
          node.click();
          return true;
        }})()
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expression):
                return True
            time.sleep(0.25)
        return False

    def click_tab(self, label: str) -> bool:
        """点 Element Plus 的页签。"""
        return self.click_by_text(label, selector=".el-tabs__item")

    def shot(self, path: str | Path) -> None:
        """截图（可选，用于排查失败原因）。"""
        import base64 as _b64

        result = self.ws.call(
            "Page.captureScreenshot", {"format": "png"}, session_id=self.session_id
        )
        Path(path).write_bytes(_b64.b64decode(result["data"]))

    def set_viewport(self, width: int, height: int) -> None:
        """覆盖视口尺寸（等价于改窗口大小，但不用重启浏览器）。

        窄屏适配用例靠它把视口切到 1024/800，再断言"没有横向溢出"。
        """
        self.ws.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
            session_id=self.session_id,
        )
        time.sleep(0.3)

    def clear_viewport(self) -> None:
        """撤销 :meth:`set_viewport` 的覆盖，恢复真实窗口尺寸。"""
        self.ws.call("Emulation.clearDeviceMetricsOverride", session_id=self.session_id)
        time.sleep(0.3)
