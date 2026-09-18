from __future__ import annotations

import base64
import html
import json
import logging
import os
import random
import re
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen

LOG = logging.getLogger("streamly")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

TELEGRAM_TOKEN = os.getenv("TOKEN", "").strip()
META_AI_KEY = os.getenv("META_AI_API_KEY", "nxt_3a454c41e6a84aeead28d1fb4aec87a4").strip()
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
META_AI_GEM_ID = "ba0fbe0d-976e-493a-afdb-6d8469e53df0"
META_AI_ENDPOINT = f"https://nxtai.zipohostbd.workers.dev/api/use?gem={META_AI_GEM_ID}"
IMAGE_FALLBACK_ENDPOINT = "https://image.pollinations.ai/prompt/"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

MAX_TEXT_LENGTH = 3900


CHAT_MODES: dict[int, str] = {}
STATE_LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="streamly")
SHUTDOWN = threading.Event()


def log(message: str, *args: Any) -> None:
    LOG.info(message, *args)


def escape(value: Any) -> str:
    return html.escape(str(value), quote=False)


def http_request(
    url: str,
    *,
    method: str = "GET",
    payload: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 35,
    max_bytes: int = 3 * 1024 * 1024,
) -> tuple[int, dict[str, str], bytes]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            chunks: list[bytes] = []
            total = 0
            while total < max_bytes:
                chunk = response.read(min(64 * 1024, max_bytes - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            return response.status, dict(response.headers.items()), b"".join(chunks)
    except HTTPError as error:
        body = error.read(1024)
        raise RuntimeError(
            f"Remote service returned HTTP {error.code}: "
            f"{body.decode(errors='replace')[:180]}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError(f"Remote service could not be reached: {error}") from error


def json_request(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 35,
) -> Any:
    body = json.dumps(data).encode() if data is not None else None
    request_headers = {"Content-Type": "application/json"} if data is not None else {}
    if headers:
        request_headers.update(headers)
    _, _, raw = http_request(
        url,
        method=method,
        payload=body,
        headers=request_headers,
        timeout=timeout,
    )
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise RuntimeError("Remote service returned invalid JSON") from error


def telegram_call(method: str, data: dict[str, Any] | None = None) -> Any:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TOKEN is not configured")
    payload = (data or {}).copy()
    status, _, raw = http_request(
        f"{TELEGRAM_API}/{method}",
        method="POST",
        payload=urlencode(payload).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=45,
        max_bytes=4 * 1024 * 1024,
    )
    if status >= 400:
        raise RuntimeError(f"Telegram API HTTP {status}")
    try:
        result = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as error:
        raise RuntimeError("Telegram returned invalid JSON") from error
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API request failed"))
    return result.get("result")


def telegram_upload(
    method: str,
    field_name: str,
    file_name: str,
    file_bytes: bytes,
    fields: dict[str, str],
) -> Any:
    boundary = f"----Streamly{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                str(value).encode(),
                b"\r\n",
            ]
        )
    parts.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{file_name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            file_bytes,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    _, _, raw = http_request(
        f"{TELEGRAM_API}/{method}",
        method="POST",
        payload=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        timeout=120,
        max_bytes=4 * 1024 * 1024,
    )
    result = json.loads(raw.decode("utf-8", errors="replace"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram upload failed"))
    return result.get("result")


def send_message(chat_id: int | str, text: str, **extra: Any) -> Any:
    return telegram_call(
        "sendMessage",
        {"chat_id": str(chat_id), "text": text, "parse_mode": "HTML", **extra},
    )


def edit_message(chat_id: int | str, message_id: int, text: str, **extra: Any) -> Any:
    try:
        return telegram_call(
            "editMessageText",
            {
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "text": text,
                "parse_mode": "HTML",
                **extra,
            },
        )
    except RuntimeError as error:
        if "message is not modified" not in str(error).lower():
            log("Could not edit status message: %s", error)
        return None


def answer_callback(callback_id: str, text: str = "") -> None:
    try:
        telegram_call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
    except RuntimeError as error:
        log("Callback acknowledgement failed: %s", error)


def inline_keyboard(rows: list[list[dict[str, str]]]) -> str:
    return json.dumps({"inline_keyboard": rows})


def main_keyboard() -> str:
    return inline_keyboard(
        [
            [
                {"text": "🤖 AI assistant", "callback_data": "mode:ai", "style": "primary"},
                {"text": "ℹ️ How it works", "callback_data": "help", "style": "primary"},
            ],
            [
                {"text": "✖️ Cancel", "callback_data": "cancel", "style": "danger"},
            ],
        ]
    )


def ai_request(message: str) -> Any:
    if not META_AI_KEY:
        raise RuntimeError("META_AI_API_KEY is not configured")
    return json_request(
        META_AI_ENDPOINT,
        method="POST",
        data={"api_key": META_AI_KEY, "message": message},
        headers={"Accept": "application/json"},
        timeout=75,
    )


def extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("response", "reply", "message", "text", "content", "answer", "output"):
            if key in value:
                result = extract_text(value[key])
                if result:
                    return result
        for nested in value.values():
            result = extract_text(nested)
            if result:
                return result
    if isinstance(value, list):
        return "\n".join(result for item in value if (result := extract_text(item)))
    return ""


def extract_image(value: Any) -> tuple[str | None, bytes | None]:
    if isinstance(value, dict):
        for key, nested in value.items():
            lower = key.lower()
            if isinstance(nested, str) and nested.startswith(("http://", "https://")):
                if any(word in lower for word in ("image", "photo", "picture", "url", "src")):
                    return nested, None
            if isinstance(nested, str) and nested.startswith("data:image/"):
                try:
                    return None, base64.b64decode(nested.split(",", 1)[1])
                except (ValueError, IndexError):
                    pass
            url, raw = extract_image(nested)
            if url or raw:
                return url, raw
    elif isinstance(value, list):
        for nested in value:
            url, raw = extract_image(nested)
            if url or raw:
                return url, raw
    return None, None


def fallback_image_url(prompt: str) -> str:
    cleaned = re.sub(r"\s+", " ", prompt).strip()[:700]
    return f"{IMAGE_FALLBACK_ENDPOINT}{quote(cleaned, safe='')}?width=1024&height=1024&nologo=true"


def send_ai_response(chat_id: int, prompt: str, status_id: int) -> None:
    try:
        response = ai_request(prompt)
        image_url, image_bytes = extract_image(response)
        text = extract_text(response).strip()
        image_request = prompt.lstrip().lower().startswith("/image")
        if image_request and not image_url and not image_bytes:
            image_url = fallback_image_url(prompt.lstrip()[len("/image") :].strip())
        if image_url:
            telegram_call(
                "sendPhoto",
                {
                    "chat_id": str(chat_id),
                    "photo": image_url,
                    "caption": "Generated image",
                },
            )
        elif image_bytes:
            telegram_upload(
                "sendPhoto",
                "photo",
                "streamly-generated.png",
                image_bytes,
                {"chat_id": str(chat_id), "caption": "Generated image"},
            )
        if image_request:
            edit_message(
                chat_id,
                status_id,
                f"<b>Image ready</b>\n\n{escape(text[:700] or 'ছবি তৈরি করা হয়েছে।')}",
            )
            return
        edit_message(
            chat_id,
            status_id,
            f"<b>AI assistant</b>\n\n{escape(text[:MAX_TEXT_LENGTH] or 'Response পাওয়া গেছে।')}",
        )
    except Exception as error:
        log("AI request failed: %s", error)
        edit_message(
            chat_id,
            status_id,
            "<b>AI assistant</b>\n\nএই মুহূর্তে উত্তর আনা যায়নি। কিছুক্ষণ পর আবার চেষ্টা করুন।",
        )


def welcome_text(first_name: str = "") -> str:
    greeting = f"স্বাগতম, {escape(first_name)}" if first_name else "স্বাগতম"
    return (
        f"<b>{greeting} — Streamly</b>\n\n"
        "AI assistant ব্যবহার করে প্রশ্নের উত্তর নিতে বা ছবি তৈরি করতে পারবেন।\n\n"
        "<i>শুরু করতে নিচের AI assistant বোতামটি চাপুন।</i>"
    )


def help_text() -> str:
    return (
        "<b>Streamly কীভাবে ব্যবহার করবেন</b>\n\n"
        "১. <b>AI assistant</b> চাপুন\n"
        "২. আপনার প্রশ্ন লিখুন\n"
        "৩. ছবি চাইলে এভাবে লিখুন: <code>/image a futuristic city at night</code>\n\n"
        "AI assistant আপনার প্রশ্নের উত্তর দেবে এবং প্রয়োজনে ছবি তৈরি করে পাঠাবে।"
    )


def process_message(message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (message.get("text") or "").strip()
    first_name = (message.get("from") or {}).get("first_name", "")
    if not text:
        send_message(chat_id, "আপনার প্রশ্ন লিখুন অথবা নিচের menu ব্যবহার করুন।", reply_markup=main_keyboard())
        return

    command = text.split(maxsplit=1)[0].lower()
    if command == "/start":
        CHAT_MODES[chat_id] = "home"
        send_message(chat_id, welcome_text(first_name), reply_markup=main_keyboard())
        return
    if command == "/help":
        send_message(chat_id, help_text(), reply_markup=main_keyboard())
        return
    if command in {"/cancel", "/stop"}:
        CHAT_MODES[chat_id] = "home"
        send_message(chat_id, "Cancelled. আবার শুরু করতে পারেন।", reply_markup=main_keyboard())
        return
    if command == "/ai":
        prompt = text[len(command) :].strip()
        CHAT_MODES[chat_id] = "ai"
        if prompt:
            status = send_message(chat_id, "💭 Thinking....")
            EXECUTOR.submit(send_ai_response, chat_id, prompt, status["message_id"])
        else:
            send_message(chat_id, "AI mode চালু। আপনার প্রশ্ন লিখুন।")
        return
    if command == "/image":
        prompt = text[len(command) :].strip()
        if not prompt:
            send_message(chat_id, "এভাবে লিখুন:\n<code>/image a futuristic city at night</code>")
            return
        status = send_message(chat_id, "AI image তৈরি করছি…")
        EXECUTOR.submit(send_ai_response, chat_id, f"/image {prompt}", status["message_id"])
        return

    mode = CHAT_MODES.get(chat_id, "home")
    if mode == "ai":
        status = send_message(chat_id, "💭 Thinking....")
        EXECUTOR.submit(send_ai_response, chat_id, text, status["message_id"])
        return
    send_message(
        chat_id,
        "AI assistant ব্যবহার করতে নিচের বোতামটি চাপুন।",
        reply_markup=main_keyboard(),
    )


def process_callback(callback: dict[str, Any]) -> None:
    callback_id = callback.get("id", "")
    data = callback.get("data", "")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    answer_callback(callback_id)
    if chat_id is None or message_id is None:
        return

    if data == "home":
        CHAT_MODES[chat_id] = "home"
        edit_message(chat_id, message_id, welcome_text(), reply_markup=main_keyboard())
    elif data == "help":
        edit_message(chat_id, message_id, help_text(), reply_markup=main_keyboard())
    elif data == "cancel":
        CHAT_MODES[chat_id] = "home"
        edit_message(chat_id, message_id, "Cancelled. আবার শুরু করতে পারেন।", reply_markup=main_keyboard())
    elif data == "mode:ai":
        CHAT_MODES[chat_id] = "ai"
        edit_message(chat_id, message_id, "AI assistant mode চালু। আপনার প্রশ্ন লিখুন।")


def polling_loop() -> None:
    offset = 0
    backoff = 2
    while not SHUTDOWN.is_set():
        try:
            updates = telegram_call(
                "getUpdates",
                {
                    "offset": str(offset),
                    "timeout": "25",
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
            )
            backoff = 2
            for update in updates or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                try:
                    if update.get("callback_query"):
                        process_callback(update["callback_query"])
                    elif update.get("message"):
                        process_message(update["message"])
                except Exception:
                    LOG.exception("Update handling failed")
        except Exception as error:
            log("Polling error: %s", error)
            SHUTDOWN.wait(backoff)
            backoff = min(backoff * 2, 30)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/healthz", "/health"}:
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps(
            {
                "ok": True,
                "service": "streamly",
                "ai": True,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_health_server() -> ThreadingHTTPServer:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    log("Health server listening on 0.0.0.0:%s", port)
    return server


# ---------------------------------------------------------------------------
# Keep-alive (self-ping) — Render free/hobby web services sleep after a
# period of no inbound HTTP traffic. This background thread pings the
# service's own public health endpoint on a fixed interval so it always
# looks "active" and never goes idle.
# ---------------------------------------------------------------------------
KEEP_ALIVE_MIN_SECONDS = 12 * 60   # 12 minutes
KEEP_ALIVE_MAX_SECONDS = 14 * 60   # 14 minutes


def _keep_alive_url() -> str | None:
    """
    Figure out the public URL to ping. Render automatically sets
    RENDER_EXTERNAL_URL for web services — no manual config needed there.
    KEEP_ALIVE_URL can be set manually to override/for other hosts.
    """
    explicit = os.getenv("KEEP_ALIVE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + "/healthz"
    render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if render_url:
        return render_url.rstrip("/") + "/healthz"
    return None


def keep_alive_loop() -> None:
    url = _keep_alive_url()
    if not url:
        log(
            "Keep-alive disabled: no RENDER_EXTERNAL_URL or KEEP_ALIVE_URL "
            "found in environment."
        )
        return
    log("Keep-alive enabled — pinging %s every ~12-14 minutes", url)
    while not SHUTDOWN.wait(random.uniform(KEEP_ALIVE_MIN_SECONDS, KEEP_ALIVE_MAX_SECONDS)):
        try:
            status, _, _ = http_request(url, method="GET", timeout=20, max_bytes=4096)
            log("Keep-alive ping ok (HTTP %s)", status)
        except Exception as error:
            log("Keep-alive ping failed: %s", error)


def start_keep_alive() -> None:
    thread = threading.Thread(target=keep_alive_loop, name="keep-alive", daemon=True)
    thread.start()


def shutdown_handler(_signum: int, _frame: Any) -> None:
    log("Shutdown signal received")
    SHUTDOWN.set()


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TOKEN secret is missing")
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    health_server = start_health_server()
    start_keep_alive()
    try:
        telegram_call("deleteWebhook", {"drop_pending_updates": "false"})
        me = telegram_call("getMe")
        log("Bot connected as @%s", me.get("username", "unknown"))
        polling_thread = threading.Thread(target=polling_loop, name="telegram-polling", daemon=True)
        polling_thread.start()
        while not SHUTDOWN.wait(1):
            pass
    finally:
        SHUTDOWN.set()
        health_server.shutdown()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)
        log("Streamly stopped")

if __name__ == "__main__":
    main()
