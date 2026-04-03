"""
Slack API client that authenticates using desktop app credentials.

Uses the xoxc- token + d cookie extracted from the local Slack app.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import json

from .credentials import SlackCredentials, get_credentials


SLACK_API_BASE = "https://slack.com/api"


@dataclass
class Channel:
    id: str
    name: str
    is_channel: bool
    is_im: bool
    is_mpim: bool
    is_private: bool
    is_archived: bool
    num_members: int | None = None
    topic: str = ""
    purpose: str = ""

    @property
    def display_name(self) -> str:
        if self.is_im or self.is_mpim:
            return self.name
        prefix = "" if not self.is_private else ""
        return f"{prefix}{self.name}"

    @classmethod
    def from_api(cls, data: dict) -> Channel:
        return cls(
            id=data["id"],
            name=data.get("name", data.get("user", data["id"])),
            is_channel=data.get("is_channel", False),
            is_im=data.get("is_im", False),
            is_mpim=data.get("is_mpim", False),
            is_private=data.get("is_private", False),
            is_archived=data.get("is_archived", False),
            num_members=data.get("num_members"),
            topic=data.get("topic", {}).get("value", ""),
            purpose=data.get("purpose", {}).get("value", ""),
        )


@dataclass
class Message:
    ts: str
    user: str
    text: str
    thread_ts: str | None = None
    reply_count: int = 0
    reactions: list[dict] = field(default_factory=list)

    @property
    def timestamp(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts), tz=timezone.utc)

    @classmethod
    def from_api(cls, data: dict) -> Message:
        return cls(
            ts=data["ts"],
            user=data.get("user", data.get("bot_id", "unknown")),
            text=data.get("text", ""),
            thread_ts=data.get("thread_ts"),
            reply_count=data.get("reply_count", 0),
            reactions=data.get("reactions", []),
        )


@dataclass
class User:
    id: str
    name: str
    real_name: str
    display_name: str
    is_bot: bool

    @classmethod
    def from_api(cls, data: dict) -> User:
        profile = data.get("profile", {})
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            real_name=profile.get("real_name", data.get("real_name", "")),
            display_name=profile.get("display_name", ""),
            is_bot=data.get("is_bot", False),
        )


class SlackClient:
    """Slack API client using desktop app credentials."""

    def __init__(self, credentials: SlackCredentials | None = None):
        self.credentials = credentials or get_credentials()
        self._user_cache: dict[str, User] = {}

    def _api_call(self, method: str, **params: Any) -> dict:
        """Make an authenticated Slack API call."""
        url = f"{SLACK_API_BASE}/{method}"
        params["token"] = self.credentials.token
        body = urlencode(params).encode("utf-8")

        req = Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Cookie", f"d={self.credentials.d_cookie}")

        try:
            with urlopen(req) as resp:
                data = json.loads(resp.read())
        except HTTPError as e:
            raise RuntimeError(f"Slack API error: {e.code} {e.reason}") from e

        if not data.get("ok"):
            raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
        return data

    def _paginate(self, method: str, result_key: str, **params: Any) -> list[dict]:
        """Auto-paginate a cursor-based Slack API method."""
        results = []
        cursor = None
        while True:
            if cursor:
                params["cursor"] = cursor
            data = self._api_call(method, **params)
            results.extend(data.get(result_key, []))
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return results

    # -- Auth --

    def auth_test(self) -> dict:
        """Test authentication and return workspace/user info."""
        return self._api_call("auth.test")

    # -- Users --

    def get_user(self, user_id: str) -> User:
        """Look up a user by ID (cached)."""
        if user_id not in self._user_cache:
            data = self._api_call("users.info", user=user_id)
            self._user_cache[user_id] = User.from_api(data["user"])
        return self._user_cache[user_id]

    def resolve_user_name(self, user_id: str) -> str:
        """Get a display-friendly name for a user ID."""
        try:
            user = self.get_user(user_id)
            return user.display_name or user.real_name or user.name
        except RuntimeError:
            return user_id

    # -- Channels --

    def list_channels(self, include_archived: bool = False) -> list[Channel]:
        """List public and private channels the user is a member of."""
        params = {
            "types": "public_channel,private_channel",
            "exclude_archived": "false" if include_archived else "true",
            "limit": 200,
        }
        raw = self._paginate("conversations.list", "channels", **params)
        return [Channel.from_api(c) for c in raw]

    def list_dms(self) -> list[Channel]:
        """List direct message conversations."""
        raw = self._paginate("conversations.list", "channels", types="im", limit=200)
        channels = []
        for c in raw:
            ch = Channel.from_api(c)
            # Resolve the DM partner's name
            if c.get("user"):
                ch.name = self.resolve_user_name(c["user"])
            channels.append(ch)
        return channels

    def list_group_dms(self) -> list[Channel]:
        """List multi-party direct message conversations."""
        raw = self._paginate("conversations.list", "channels", types="mpim", limit=200)
        return [Channel.from_api(c) for c in raw]

    # -- Messages --

    def get_messages(
        self, channel_id: str, limit: int = 20, oldest: str | None = None
    ) -> list[Message]:
        """Get recent messages from a channel/DM."""
        params: dict[str, Any] = {"channel": channel_id, "limit": limit}
        if oldest:
            params["oldest"] = oldest
        data = self._api_call("conversations.history", **params)
        return [Message.from_api(m) for m in data.get("messages", [])]

    def get_thread(self, channel_id: str, thread_ts: str) -> list[Message]:
        """Get all replies in a thread."""
        raw = self._paginate(
            "conversations.replies", "messages",
            channel=channel_id, ts=thread_ts, limit=200,
        )
        return [Message.from_api(m) for m in raw]

    # -- Sending --

    def send_message(self, channel_id: str, text: str, thread_ts: str | None = None) -> Message:
        """Send a message to a channel or DM."""
        params: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            params["thread_ts"] = thread_ts
        data = self._api_call("chat.postMessage", **params)
        return Message.from_api(data["message"])

    def send_dm(self, user_id: str, text: str) -> Message:
        """Open (or reuse) a DM with a user and send a message."""
        data = self._api_call("conversations.open", users=user_id)
        channel_id = data["channel"]["id"]
        return self.send_message(channel_id, text)
