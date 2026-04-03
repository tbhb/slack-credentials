"""CLI for slack-credentials."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from .client import SlackClient
from .credentials import get_credentials


def cmd_status(client: SlackClient, _args: argparse.Namespace) -> None:
    """Show authentication status and credential diagnostics."""
    creds = client.credentials
    print("Credentials")
    print(f"  Token:    {creds.token[:20]}...{creds.token[-8:]}")
    print(f"  d cookie: {creds.d_cookie[:20]}...{creds.d_cookie[-8:]}")
    if creds.user_id:
        print(f"  User ID:  {creds.user_id}")
    if creds.team_id:
        print(f"  Team ID:  {creds.team_id}")

    print()
    print("API check")
    try:
        info = client.auth_test()
        print(f"  Status:    authenticated")
        print(f"  Workspace: {info.get('team')} ({info.get('url', '')})")
        print(f"  User:      {info.get('user')} ({info.get('user_id', '')})")
        print(f"  Team ID:   {info.get('team_id', '')}")
    except RuntimeError as e:
        print(f"  Status:    FAILED")
        print(f"  Error:     {e}")
        sys.exit(1)


def cmd_channels(client: SlackClient, args: argparse.Namespace) -> None:
    """List channels."""
    channels = client.list_channels(include_archived=args.archived)
    if not channels:
        print("No channels found.")
        return

    # Sort by name
    channels.sort(key=lambda c: c.name.lower())
    name_width = max(len(c.display_name) for c in channels)

    for ch in channels:
        members = f"{ch.num_members:>4} members" if ch.num_members is not None else ""
        topic = f"  {ch.topic}" if ch.topic else ""
        print(f"  {ch.display_name:<{name_width}}  {ch.id}  {members}{topic}")


def cmd_dms(client: SlackClient, _args: argparse.Namespace) -> None:
    """List direct message conversations."""
    dms = client.list_dms()
    if not dms:
        print("No DMs found.")
        return

    dms.sort(key=lambda c: c.name.lower())
    name_width = max(len(c.name) for c in dms)

    for dm in dms:
        print(f"  {dm.name:<{name_width}}  {dm.id}")

    group_dms = client.list_group_dms()
    if group_dms:
        print()
        print("Group DMs:")
        for gdm in group_dms:
            print(f"  {gdm.name}  {gdm.id}")


def cmd_messages(client: SlackClient, args: argparse.Namespace) -> None:
    """Show recent messages in a channel/DM."""
    messages = client.get_messages(args.channel, limit=args.limit)
    if not messages:
        print("No messages found.")
        return

    # Messages come newest-first; reverse for chronological display
    messages.reverse()
    for msg in messages:
        name = client.resolve_user_name(msg.user)
        time_str = msg.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
        thread_indicator = f" [{msg.reply_count} replies]" if msg.reply_count else ""
        print(f"  {time_str}  {name}: {msg.text}{thread_indicator}")


def cmd_thread(client: SlackClient, args: argparse.Namespace) -> None:
    """Show all messages in a thread."""
    messages = client.get_thread(args.channel, args.thread_ts)
    if not messages:
        print("No messages found.")
        return

    for msg in messages:
        name = client.resolve_user_name(msg.user)
        time_str = msg.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"  {time_str}  {name}: {msg.text}")


def cmd_send(client: SlackClient, args: argparse.Namespace) -> None:
    """Send a DM to a user."""
    # Accept user ID directly, or look up by name
    user_id = args.user
    if not user_id.startswith("U"):
        # Try to find the user by name in existing DMs
        dms = client.list_dms()
        match = [dm for dm in dms if dm.name.lower() == user_id.lower()]
        if not match:
            # Search all workspace members
            members = client._paginate("users.list", "members", limit=200)
            for m in members:
                name = m.get("name", "")
                profile = m.get("profile", {})
                display = profile.get("display_name", "")
                real = profile.get("real_name", "")
                if user_id.lower() in (name.lower(), display.lower(), real.lower()):
                    user_id = m["id"]
                    break
            else:
                print(f"Could not find user: {args.user}", file=sys.stderr)
                sys.exit(1)
        else:
            # The DM channel has the user's ID in the API data; re-fetch to get it
            # Easier: just send to the DM channel directly
            msg = client.send_message(match[0].id, args.message)
            name = client.resolve_user_name(msg.user)
            time_str = msg.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
            print(f"  {time_str}  {name}: {msg.text}")
            return

    msg = client.send_dm(user_id, args.message)
    name = client.resolve_user_name(msg.user)
    time_str = msg.timestamp.astimezone().strftime("%Y-%m-%d %H:%M")
    print(f"  {time_str}  {name}: {msg.text}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="slack-credentials",
        description="Access Slack using desktop app credentials",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Show auth status and diagnostics")

    ch_parser = subparsers.add_parser("channels", help="List channels")
    ch_parser.add_argument("--archived", action="store_true", help="Include archived channels")

    subparsers.add_parser("dms", help="List direct messages")

    msg_parser = subparsers.add_parser("messages", help="Show recent messages")
    msg_parser.add_argument("channel", help="Channel or DM ID")
    msg_parser.add_argument("-n", "--limit", type=int, default=20, help="Number of messages")

    thread_parser = subparsers.add_parser("thread", help="Show thread messages")
    thread_parser.add_argument("channel", help="Channel ID")
    thread_parser.add_argument("thread_ts", help="Thread timestamp")

    send_parser = subparsers.add_parser("send", help="Send a DM to a user")
    send_parser.add_argument("user", help="User ID (U...) or display name")
    send_parser.add_argument("message", help="Message text")

    args = parser.parse_args(argv)

    creds = get_credentials()
    client = SlackClient(creds)

    commands = {
        "status": cmd_status,
        "channels": cmd_channels,
        "dms": cmd_dms,
        "messages": cmd_messages,
        "thread": cmd_thread,
        "send": cmd_send,
    }
    commands[args.command](client, args)


if __name__ == "__main__":
    main()
