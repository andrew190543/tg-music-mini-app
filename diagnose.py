"""Self-diagnostics for the Telegram Mini App deployment.

This script is intentionally dependency-light: it only uses the Python
standard library plus :mod:`dotenv` because it must also be useful before the
application is started, and it does not need to bring the bot/API up.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

PROJECT_DIR = Path(__file__).resolve().parent
ENV_PATH = PROJECT_DIR / ".env"
DEFAULT_PORT = 8000

LOCAL_TIMEOUT = 5.0
TUNNEL_TIMEOUT = 15.0
TELEGRAM_TIMEOUT = 10.0


class ActionResult:
    """Simple [OK]/[FAIL] result for one check."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.details: list[str] = []

    def ok(self, detail: str | None = None) -> None:
        if detail:
            self.details.append(detail)

    def warn(self, warning: str) -> None:
        self.warnings.append(warning)

    def fail(self, reason: str) -> None:
        self.errors.append(reason)

    def render(self) -> str:
        mark = "[OK]" if not self.errors else "[FAIL]"
        return f"{mark} {self.name}{' :: '.join(self.errors)}"


def _read_env() -> dict[str, Any]:
    values = dotenv_values(ENV_PATH)
    return dict(values or {})


def check_env() -> tuple[int | None, str, ActionResult]:
    result = ActionResult("environment")
    if not ENV_PATH.is_file():
        result.fail(f".env not found at {ENV_PATH}")
        return None, "", result

    values = _read_env()
    result.ok(f"loaded {ENV_PATH}")

    raw_port = str(values.get("PORT", "")).strip() or str(
        os.getenv("PORT", DEFAULT_PORT)
    )
    raw_url = str(values.get("WEBAPP_URL", "")).strip().rstrip("/") or os.getenv(
        "WEBAPP_URL", ""
    ).strip().rstrip("/")

    port: int | None = None
    try:
        port = int(raw_port)
    except ValueError:
        result.fail(f" порт `.env` не число: {raw_port!r}")
    else:
        if not 1 <= port <= 65535:
            result.fail(f" порт `{port}` вне 1..65535")

    if not raw_url:
        result.fail(" `WEBAPP_URL` пуст")
    elif not raw_url.lower().startswith("https://"):
        result.fail(f" `WEBAPP_URL` не HTTPS: {raw_url!r}")
    else:
        result.ok(f"WEBAPP_URL = {raw_url}")

    if port is not None:
        result.ok(f"PORT = {port}")

    if result.errors:
        for error in result.errors:
            print(result.render().replace("!  ", ""))
        print(result.render())
        # Still return whatever we parsed, so later checks can show why.
        return (port if port is not None else None), raw_url, result
    return port, raw_url, result


def check_port_owner(port: int | None) -> ActionResult:
    result = ActionResult("port owner")
    if port is None:
        result.fail(" порт неизвестен")
        return result

    pattern = re.compile(
        r"TCP\s+[^\s]+:(\d+)\s+[^\s]+:[^\s]+\s+LISTENING\s+(\d+)\s*",
        re.IGNORECASE,
    )
    try:
        netstat = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            ),
        )
    except Exception as exc:  # pragma: no cover - diagnostic helper only
        result.fail(f" netstat failed: {exc}")
        return result

    pids: list[int] = []
    for line in netstat.stdout.splitlines():
        match = pattern.match(line.strip())
        if match and int(match.group(1)) == port:
            pids.append(int(match.group(2)))

    unique_pids = sorted(set(pids))
    if not unique_pids:
        result.fail(f" порт {port} не занять ни одним процессом")
        return result

    details: list[str] = []
    for pid in unique_pids:
        process_name = _read_process_name(pid)
        details.append(f"PID {pid}: {process_name or 'Unknown'}")
    result.ok(", ".join(details))
    return result


def _read_process_name(pid: int) -> str:
    """Return the executable name for PID using tasklist/Get-Process."""
    if hasattr(os, "name") and os.name == "nt":
        command = ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]
    else:
        command = ["ps", "-p", str(pid), "-o", "comm="]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            ),
        )
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        if os.name == "nt":
            parts = next(iter(csv.reader([first_line])), [])
            return parts[0] if parts else ""
        return first_line.strip()
    except Exception:
        return "Unknown"


def check_local_server(port: int | None) -> ActionResult:
    result = ActionResult("local /app/")
    if port is None:
        result.fail(" нет корректного порта")
        return result

    url = f"http://127.0.0.1:{port}/app/"
    try:
        with urllib.request.urlopen(url, timeout=LOCAL_TIMEOUT) as response:
            if response.status == 200:
                result.ok(f"{url} -> {response.status}")
            else:
                result.fail(f" {url} ответил {response.status}")
    except urllib.error.HTTPError as exc:
        result.fail(f" {url} -> HTTP {exc.code}")
    except Exception as exc:
        result.fail(f" {url} недоступен: {exc}")
    return result


def check_tunnel(webapp_url: str) -> ActionResult:
    result = ActionResult("tunnel /app/")
    if not webapp_url:
        result.fail(" `WEBAPP_URL` не задан")
        return result

    url = f"{webapp_url}/app/"
    headers = {
        "X-Pinggy-No-Screen": "true",
        "User-Agent": "TGMusic-Diagnose/1.0",
    }
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=TUNNEL_TIMEOUT) as response:
            body = response.read(500).decode("utf-8", "replace")
            if response.status == 200 and "TG Music" in body:
                result.ok(f"{url} -> {response.status}")
            elif response.status == 200:
                result.warn(
                    f"{url} -> 200, но тело не похоже на Mini App "
                    f"(первые 500 символов: {body!r})"
                )
            else:
                result.fail(f" {url} -> {response.status}")
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 502}:
            result.fail(f" {url} -> HTTP {exc.code} (туннель/адрес битый)")
        else:
            result.fail(f" {url} -> HTTP {exc.code}")
    except Exception as exc:
        result.fail(f" {url} недоступен: {exc}")
    return result


def check_telegram_menu(chat_id: int | None = None) -> ActionResult:
    """Read the Telegram menu button with/without a concrete chat id."""
    result = ActionResult("Telegram getChatMenuButton")
    try:
        token = str(_read_env().get("BOT_TOKEN", "")).strip()
    except Exception:
        result.fail(" BOT_TOKEN unavailable")
        return result

    if not token:
        result.fail(" BOT_TOKEN не задан")
        return result

    endpoint = "https://api.telegram.org/bot" + token + "/getChatMenuButton"
    if chat_id is not None:
        endpoint += "?chat_id=" + str(chat_id)
    try:
        with urllib.request.urlopen(endpoint, timeout=TELEGRAM_TIMEOUT) as response:
            payload = json.load(response)
        if not payload.get("ok"):
            result.fail(f" Telegram вернул not ok: {payload.get('description')}")
            return result

        button = payload.get("result") or {}
        button_type = button.get("type")
        url = button.get("web_app", {}).get("url") if button_type == "web_app" else None
        text = button.get("text")
        expected_extension = "/app/"

        if button_type == "web_app" and url:
            pretty = f"{url} ({text!r})"
            if url.endswith(expected_extension.removesuffix("/")):
                pretty += " [tail OK]"
            else:
                result.warn(f"Expected {pretty} to end with {expected_extension}")
            result.ok(pretty)
        else:
            result.fail(
                f" Telegram вернул {button_type!r}, ожидаем web_app {url!r}"
            )
    except Exception as exc:
        result.fail(f" Telegram запрос не удался: {exc}")
    return result


def _stdout_only(report_lines: list[str]) -> None:
    sys.stdout.write("\n".join(report_lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chat-id",
        type=int,
        default=None,
        help="chat id of a private chat whose menu button should be checked",
    )
    args = parser.parse_args()

    print(f"Проект: {PROJECT_DIR}")
    print(f".env: {ENV_PATH}")
    print("")
    print("=".center(78, "="))
    print("")
    port, webapp_url, env_result = check_env()
    port_result = check_port_owner(port)
    local_result = check_local_server(port)
    tunnel_result = check_tunnel(webapp_url)
    telegram_result = check_telegram_menu(args.chat_id)

    results = [
        env_result,
        port_result,
        local_result,
        tunnel_result,
        telegram_result,
    ]
    lines: list[str] = []
    for result in results:
        lines.append(result.render())
        if result.details:
            lines.extend(f"  - {detail}" for detail in result.details)
        if result.warnings:
            lines.extend(
                f"  ! Предупреждение: {detail}" for detail in result.warnings
            )
        lines.append("")

    _stdout_only(lines)
    return 0 if all(not result.errors for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
