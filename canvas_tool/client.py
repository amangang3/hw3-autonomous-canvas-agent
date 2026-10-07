from __future__ import annotations

import json
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from .safety import first_nonempty_line, normalize_text, redact, strip_html

BASE_URL = "https://canvas.mit.edu"
TOPIC_TITLE = "Homework 3: Agent Discussion Forum"


class CanvasError(RuntimeError):
    pass


def load_token(path: Path | None = None) -> str:
    path = path or Path.home() / ".config" / "hw3-agent" / "env"
    try:
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            raise CanvasError("credential file must have mode 0600")
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("CANVAS_TOKEN="):
                token = line.split("=", 1)[1].strip()
                if token:
                    return token
    except CanvasError:
        raise
    except OSError as exc:
        raise CanvasError("credential file unavailable") from exc
    raise CanvasError("CANVAS_TOKEN is missing")


class CanvasClient:
    def __init__(self, token: str, *, opener: Callable = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep,
                 jitter: Callable[[], float] = random.random, faults=None) -> None:
        self._token = token
        self._opener = opener
        self._sleep = sleep
        self._jitter = jitter
        self.faults = faults

    def _url(self, path: str) -> str:
        if not path.startswith("/api/v1/"):
            raise CanvasError("request path is outside Canvas API")
        url = urllib.parse.urljoin(BASE_URL, path)
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "canvas.mit.edu":
            raise CanvasError("request origin is not allowed")
        return url

    def _request(self, method: str, path: str, data: dict[str, Any] | None = None):
        url = self._url(path)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        if self.faults and method == "GET":
            self.faults.before_get()
        return self._opener(req, timeout=20)

    @staticmethod
    def _decode(response) -> Any:
        try:
            return json.loads(response.read().decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise CanvasError("malformed JSON response") from exc

    def get_json(self, path: str) -> Any:
        data, _ = self._get_page(path)
        return data

    def _get_page(self, path: str) -> tuple[Any, Any]:
        delays = [1, 2]
        for attempt in range(3):
            try:
                with self._request("GET", path) as response:
                    status = getattr(response, "status", 200)
                    if status >= 500 or status == 429:
                        raise urllib.error.HTTPError(path, status, "transient", response.headers, None)
                    if status >= 400:
                        raise urllib.error.HTTPError(path, status, "failed", response.headers, None)
                    return self._decode(response), response.headers
            except urllib.error.HTTPError as exc:
                if exc.code != 429 and not 500 <= exc.code < 600:
                    raise CanvasError(f"Canvas GET failed with HTTP {exc.code}") from exc
                if attempt == 2:
                    raise CanvasError("Canvas GET failed after retries") from exc
                delay = self._retry_after(exc.headers) or delays[attempt] + self._jitter()
                self._sleep(delay)
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                if attempt == 2:
                    raise CanvasError("Canvas GET failed after retries") from exc
                self._sleep(delays[attempt] + self._jitter())
        raise CanvasError("Canvas GET failed")

    @staticmethod
    def _retry_after(headers) -> float | None:
        if not headers:
            return None
        value = headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                return None

    def get_paginated(self, path: str, max_pages: int = 100) -> list[Any]:
        items: list[Any] = []
        current = self._url(path)
        seen: set[str] = set()
        for _ in range(max_pages):
            if current in seen:
                raise CanvasError("pagination loop")
            seen.add(current)
            parsed = urllib.parse.urlsplit(current)
            if parsed.scheme != "https" or parsed.netloc != "canvas.mit.edu" or not parsed.path.startswith("/api/v1/"):
                raise CanvasError("unsafe pagination link")
            relative = parsed.path + (("?" + parsed.query) if parsed.query else "")
            page, headers = self._get_page(relative)
            if not isinstance(page, list):
                raise CanvasError("paginated response is not a list")
            items.extend(page)
            current = self._next_link(headers)
            if not current:
                return items
        raise CanvasError("pagination page limit exceeded")

    @staticmethod
    def _next_link(headers) -> str | None:
        link = headers.get("Link") if headers else None
        if not link:
            return None
        for chunk in link.split(","):
            match = re.match(r'\s*<([^>]+)>\s*;\s*rel="?([^";]+)"?', chunk)
            if match and match.group(2) == "next":
                url = match.group(1)
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme != "https" or parsed.netloc != "canvas.mit.edu" or not parsed.path.startswith("/api/v1/"):
                    raise CanvasError("unsafe pagination link")
                return url
        return None

    def post_json(self, path: str, data: dict[str, Any]) -> Any:
        try:
            with self._request("POST", path, data) as response:
                return self._decode(response)
        except urllib.error.HTTPError as exc:
            raise CanvasError(f"Canvas POST failed with HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise CanvasError("Canvas POST acknowledgement uncertain") from exc

    def discover(self) -> dict[str, int]:
        user = self.get_json("/api/v1/users/self")
        if not isinstance(user, dict) or not isinstance(user.get("id"), (int, str)):
            raise CanvasError("self response missing id")
        matches: list[tuple[int, int]] = []
        courses = self.get_paginated("/api/v1/courses?enrollment_state=active")
        for course in courses:
            if not isinstance(course, dict) or not str(course.get("id", "")).isdigit():
                raise CanvasError("course response missing id")
            cid = int(course["id"])
            topics = self.get_paginated(
                f"/api/v1/courses/{cid}/discussion_topics?search_term=Agent%20Discussion%20Forum"
            )
            for topic in topics:
                if isinstance(topic, dict) and topic.get("title") == TOPIC_TITLE and str(topic.get("id", "")).isdigit():
                    matches.append((cid, int(topic["id"])))
        if len(matches) != 1:
            raise CanvasError("expected exactly one exact-title topic")
        return {"course_id": matches[0][0], "topic_id": matches[0][1], "self_user_id": int(user["id"])}

    def get_control_line(self, course_id: int, topic_id: int) -> str:
        data = self.get_json(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}")
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            raise CanvasError("topic response missing message")
        return first_nonempty_line(data["message"])

    def get_entries(self, course_id: int, topic_id: int) -> list[dict[str, Any]]:
        data = self.get_json(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view")
        if self.faults:
            data = self.faults.after_view(data)
        if not isinstance(data, dict) or not isinstance(data.get("view"), list):
            raise CanvasError("view response malformed")
        participants = {
            int(p["id"]): p.get("display_name", "Unknown")
            for p in data.get("participants", [])
            if isinstance(p, dict) and str(p.get("id", "")).isdigit()
        }
        result: list[dict[str, Any]] = []

        def walk(raw: dict[str, Any], parent: int | None = None) -> None:
            if not isinstance(raw, dict) or not str(raw.get("id", "")).isdigit():
                raise CanvasError("entry response missing id")
            entry_id = int(raw["id"])
            user_id = raw.get("user_id")
            deleted = bool(raw.get("deleted") or raw.get("deleted_at"))
            result.append({
                "id": entry_id,
                "parent_id": parent,
                "user_id": int(user_id) if str(user_id or "").isdigit() else None,
                "author_name": participants.get(int(user_id), "Unknown") if str(user_id or "").isdigit() else "Unknown",
                "created_at": raw.get("created_at"),
                "text": strip_html(raw.get("message", "")),
                "deleted": deleted,
            })
            for child in raw.get("replies", []) or []:
                walk(child, entry_id)
        for entry in data["view"]:
            walk(entry)
        return result

    def post_entry(self, course_id: int, topic_id: int, message: str, parent_id: int | None = None) -> int:
        if parent_id is None:
            path = f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries"
        else:
            path = f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries/{int(parent_id)}/replies"
        data = self.post_json(path, {"message": message})
        if not isinstance(data, dict) or not str(data.get("id", "")).isdigit():
            raise CanvasError("POST response missing entry id")
        return int(data["id"])
