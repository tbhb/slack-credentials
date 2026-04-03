# slack-credentials

Extract Slack desktop app credentials from macOS and use them for API access — no OAuth app or bot token required.

## How it works

The Slack desktop app is an Electron app. Like all Electron apps on macOS, it stores sensitive data using a combination of the macOS Keychain and local encrypted storage. This tool extracts and decrypts those credentials so you can make Slack API calls as yourself.

### Where Slack stores credentials

| Component | Location |
|-----------|----------|
| Encryption key | macOS Keychain, service "Slack Safe Storage", account "Slack Key" |
| `xoxc-` client token | `~/Library/Application Support/Slack/Local Storage/leveldb/*.ldb` (plaintext) |
| `d` cookie (encrypted) | `~/Library/Application Support/Slack/Cookies` (SQLite database) |

The `xoxc-` token alone won't authenticate — Slack requires the `d` cookie alongside it. The token goes in the `Authorization: Bearer` header, and the `d` cookie goes in a `Cookie` header.

### The encryption scheme

The Cookies SQLite database stores cookie values encrypted using Chromium's cookie encryption. On macOS, this works as follows:

1. **Key material**: A password is stored in the macOS Keychain under "Slack Safe Storage". This is a base64-encoded key that Electron's `safeStorage` API manages.

2. **Key derivation**: The password is fed through PBKDF2-SHA1 with Chromium's hardcoded parameters:
   - Salt: `saltysalt`
   - Iterations: 1003
   - Key length: 16 bytes (AES-128)

3. **Encryption**: AES-128-CBC with a fixed IV of 16 space characters (`0x20`). Encrypted values are prefixed with `v10` to identify the scheme.

4. **Plaintext format**: The decrypted value has a **32-byte binary header** (likely an HMAC-SHA256 for integrity) prepended to the actual cookie value. The real value starts at byte 32.

So the full decryption is:

```
encrypted_value = v10 || ciphertext
key = PBKDF2-SHA1(keychain_password, "saltysalt", 1003, 16)
iv  = " " * 16
plaintext = AES-128-CBC-decrypt(key, iv, ciphertext)
cookie_value = plaintext[32:]
```

### How we figured out the 32-byte header

The standard Chromium v10 decryption is well-documented, but decrypting the cookies initially produced a `utf-8 codec can't decode` error. Adding a hex dump of the raw decrypted bytes revealed the actual cookie value (`xoxd-...`) sitting clearly at byte offset 32, with 32 bytes of binary data before it.

The clincher: decrypting two different cookies (`b` and `d`) produced **identical** first 32 bytes. In CBC mode, a wrong IV would only corrupt the first block (16 bytes), and the "garbage" would differ per cookie since each has different ciphertext. Identical bytes meant the decryption was correct all along — those 32 bytes are an intentional binary prefix, not corruption.

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/):

```
uv sync
```

## CLI usage

```bash
# Check auth status and credential health
uv run slack-credentials status

# List channels
uv run slack-credentials channels

# List DMs
uv run slack-credentials dms

# Show recent messages in a channel or DM
uv run slack-credentials messages <channel-id> -n 20

# Show a thread
uv run slack-credentials thread <channel-id> <thread-ts>

# Send a DM (by name or user ID)
uv run slack-credentials send tony "hello from the CLI"
uv run slack-credentials send U07FBUU72BD "hello by user ID"
```

## Library usage

```python
from slack_credentials import SlackClient

client = SlackClient()

# Auth check
info = client.auth_test()

# List conversations
channels = client.list_channels()
dms = client.list_dms()

# Read messages
messages = client.get_messages("<channel-id>", limit=10)
thread = client.get_thread("<channel-id>", "<thread-ts>")

# Send messages
client.send_dm("<user-id>", "hello")
client.send_message("<channel-id>", "hello channel")
```

## Limitations

- **macOS only** — relies on the macOS Keychain and Slack's Electron storage paths.
- **Requires Slack desktop app** — you must be signed in to the desktop app; this extracts its session credentials.
- **Session-scoped** — the `xoxc-` token and `d` cookie are tied to your desktop app session. If you sign out of Slack, you'll need to sign back in and the credentials will change.
- **Read-only database access** — the Cookies database is opened in read-only mode to avoid interfering with Slack.
