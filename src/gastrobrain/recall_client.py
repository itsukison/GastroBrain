"""Server-only Recall transport. Creation is deliberately never retried blindly."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class RecallConfig:
    region: str
    api_key: str
    verification_secret: str
    public_api_url: str
    web_url: str
    worker_token: str
    max_seconds: int = 7200

    @classmethod
    def from_settings(cls, s):
        values = (s.recall_region, s.recall_api_key, s.recall_webhook_verification_secret,
                  s.public_api_base_url, s.recall_web_url, s.recall_worker_token)
        if not all(values):
            raise ValueError("Recall runtime settings are incomplete")
        if values[0] not in {"ap-northeast-1", "us-east-1", "us-west-2", "eu-central-1"}:
            raise ValueError("Unsupported Recall region")
        for value in values[3:5]:
            url = urlsplit(value)
            if url.scheme != "https" or not url.hostname or url.query or url.fragment or url.username or url.path not in {'', '/'}:
                raise ValueError("Recall public origins must be HTTPS URLs without credentials or queries")
            if url.hostname == 'localhost' or url.hostname.endswith(('.localhost', '.local', '.internal')):
                raise ValueError("Recall origins must be public")
            try:
                address = ipaddress.ip_address(url.hostname)
            except ValueError:
                address = None
            if address and not address.is_global:
                raise ValueError("Recall origins must be public")
        return cls(*values, max_seconds=s.recall_max_seconds)

    @property
    def base_url(self):
        return f"https://{self.region}.recall.ai"


class RecallError(Exception):
    def __init__(self, status: int = 0):
        self.status = status
        super().__init__(f"Recall request failed (HTTP {status or 'unknown'})")


class RecallClient:
    def __init__(self, config: RecallConfig, transport=None):
        self.config = config
        self.transport = transport

    def request(self, method: str, path: str, *, payload=None, params=None):
        with httpx.Client(base_url=self.config.base_url, timeout=20, transport=self.transport,
                          headers={"Authorization": f"Token {self.config.api_key}"}) as client:
            # Only GETs are safe to retry without a scheduling reconciliation.
            for attempt in range(3 if method == "GET" else 1):
                try:
                    response = client.request(method, path, json=payload, params=params)
                except httpx.HTTPError as exc:
                    raise RecallError() from exc
                if response.status_code in {429, 503, 507} and method == "GET" and attempt < 2:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    # Long delays belong to the durable job scheduler, not this request.
                    if delay > 5:
                        raise RecallError(response.status_code)
                    time.sleep(max(0, delay))
                    continue
                if not response.is_success:
                    raise RecallError(response.status_code)
                return response.json() if response.content else {}

    def create_bot(self, run_id: str, meeting_url: str, launch_token: str):
        c = self.config
        return self.request("POST", "/api/v1/bot/", payload={
            "meeting_url": meeting_url, "bot_name": "商談AI",
            "metadata": {"gastrobrain_run_id": run_id},
            "variant": {"google_meet": "web_4_core"},
            "output_media": {"camera": {"kind": "webpage", "config": {
                "url": f"{c.web_url.rstrip('/')}/voice/recall#launch={launch_token}",
            }}},
            "recording_config": {
                "video_mixed_mp4": None,
                "transcript": {"provider": {"meeting_captions": {"language_code": "ja"}}},
                "realtime_endpoints": [{"type": "webhook",
                    "url": f"{c.public_api_url.rstrip('/')}/v1/recall/webhook",
                    "events": ["transcript.data", "participant_events.chat_message"]}],
            },
            "automatic_leave": {"waiting_room_timeout": 600, "noone_joined_timeout": 600,
                "in_call_not_recording_timeout": 180,
                "in_call_recording_timeout": c.max_seconds},
        })

    def leave(self, bot_id: str):
        return self.request("POST", f"/api/v1/bot/{bot_id}/leave_call/")

    def bot(self, bot_id: str):
        return self.request("GET", f"/api/v1/bot/{bot_id}/")

    def find_run(self, run_id: str):
        bots = []
        for page in range(1, 11):
            result = self.request("GET", "/api/v1/bot/", params={"metadata__gastrobrain_run_id": run_id, "page": page})
            bots.extend(b for b in result.get("results", [])
                        if b.get("metadata", {}).get("gastrobrain_run_id") == run_id)
            if not result.get("next"):
                return bots
        raise RecallError()  # Never act on an incomplete reconciliation.

    def transcript(self, transcript_id: str):
        resource = self.request("GET", f"/api/v1/transcript/{transcript_id}/")
        url = resource.get("data", {}).get("download_url")
        if not url or urlsplit(url).scheme != "https":
            raise RecallError()
        # The signed download URL is returned by Recall, never by the caller.
        # Do not send the API Authorization header to a storage host.
        with httpx.Client(timeout=30, transport=self.transport) as client:
            response = client.get(url)
            if not response.is_success:
                raise RecallError(response.status_code)
            if len(response.content) > 20_000_000:
                raise RecallError()
            return response.json()


def verify_webhook(raw: bytes, headers, secret: str, now: float | None = None) -> str:
    event_id = headers.get("webhook-id") or headers.get("svix-id") or ""
    stamp = headers.get("webhook-timestamp") or headers.get("svix-timestamp") or ""
    signatures = headers.get("webhook-signature") or headers.get("svix-signature") or ""
    try:
        if not event_id or len(event_id) > 200 or abs((now or time.time()) - int(stamp)) > 300:
            raise ValueError()
        if not secret.startswith("whsec_"):
            raise ValueError()
        key = base64.b64decode(secret[6:], validate=True)
        expected = hmac.digest(key, f"{event_id}.{stamp}.".encode() + raw, "sha256")
        valid = any(version == "v1" and hmac.compare_digest(expected, base64.b64decode(sig))
                    for version, sig in (s.split(",", 1) for s in signatures.split()))
        if not valid:
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid Recall signature") from exc
    return event_id


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def caption_key(transcript_id: str, segment: dict) -> str:
    # Same key for live and final download forms; no webhook arrival timestamp.
    words = segment.get("words") or []
    normalized = [(w.get("text", ""), (w.get("start_timestamp") or {}).get("relative"))
                  for w in words]
    return digest(json.dumps([transcript_id, (segment.get("participant") or {}).get("id"),
                              normalized], ensure_ascii=False, sort_keys=True))
