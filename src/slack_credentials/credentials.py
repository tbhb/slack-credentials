"""
Extract Slack desktop app credentials from macOS.

The Slack desktop app (Electron) stores:
- An encryption key in the macOS Keychain under "Slack Safe Storage"
- An xoxc- client token in Local Storage (LevelDB)
- A d cookie in the Cookies SQLite database (AES-128-CBC encrypted)

The xoxc- token + d cookie together authenticate API requests.
"""

import glob
import hashlib
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding


SLACK_DATA_DIR = Path.home() / "Library" / "Application Support" / "Slack"
COOKIES_DB = SLACK_DATA_DIR / "Cookies"
LEVELDB_DIR = SLACK_DATA_DIR / "Local Storage" / "leveldb"

# Chromium on macOS: PBKDF2(password, "saltysalt", 1003) -> AES-128-CBC, IV = 16 spaces
CHROMIUM_SALT = b"saltysalt"
CHROMIUM_ITERATIONS = 1003
CHROMIUM_KEY_LENGTH = 16
CHROMIUM_IV = b" " * 16

# Decrypted cookie values have a 32-byte binary header before the actual value
PLAINTEXT_HEADER_SIZE = 32

XOXC_PATTERN = re.compile(rb"xoxc-[a-f0-9-]+")


@dataclass
class SlackCredentials:
    """Credentials extracted from the Slack desktop app."""
    token: str        # xoxc- client token
    d_cookie: str     # d cookie value (URL-encoded)
    user_id: str | None = None
    team_id: str | None = None


def _get_keychain_password() -> bytes:
    """Retrieve the Slack Safe Storage password from the macOS Keychain."""
    result = subprocess.run(
        [
            "security", "find-generic-password",
            "-s", "Slack Safe Storage",
            "-a", "Slack Key",
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not find Slack Safe Storage in keychain. "
            "Is the Slack desktop app installed and signed in?"
        )
    return result.stdout.strip().encode("utf-8")


def _derive_key(password: bytes) -> bytes:
    """Derive AES key from the keychain password using Chromium's PBKDF2 params."""
    return hashlib.pbkdf2_hmac(
        "sha1", password, CHROMIUM_SALT, CHROMIUM_ITERATIONS, dklen=CHROMIUM_KEY_LENGTH
    )


def _decrypt_cookie_value(encrypted_value: bytes, key: bytes) -> str:
    """Decrypt a Chromium v10-encrypted cookie value."""
    if encrypted_value[:3] != b"v10":
        raise ValueError(f"Unexpected encryption version: {encrypted_value[:3]!r}")

    ciphertext = encrypted_value[3:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(CHROMIUM_IV))
    decryptor = cipher.decryptor()
    plaintext_padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    plaintext = unpadder.update(plaintext_padded) + unpadder.finalize()

    return plaintext[PLAINTEXT_HEADER_SIZE:].decode("utf-8")


def _get_cookie(name: str, key: bytes) -> str:
    """Decrypt a named cookie from the Slack Cookies database."""
    conn = sqlite3.connect(f"file:{COOKIES_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT encrypted_value FROM cookies "
            "WHERE host_key = '.slack.com' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Cookie '{name}' not found in Slack cookies database")
        return _decrypt_cookie_value(row[0], key)
    finally:
        conn.close()


def _get_xoxc_token() -> str:
    """Extract the xoxc- token from Slack's LevelDB Local Storage."""
    ldb_files = sorted(LEVELDB_DIR.glob("*.ldb"), key=lambda p: p.stat().st_mtime, reverse=True)
    log_files = sorted(LEVELDB_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)

    for path in [*ldb_files, *log_files]:
        data = path.read_bytes()
        match = XOXC_PATTERN.search(data)
        if match:
            return match.group(0).decode("ascii")

    raise RuntimeError(
        "Could not find xoxc- token in Slack Local Storage. "
        "Is the Slack desktop app signed in?"
    )


def _get_user_info_from_leveldb() -> tuple[str | None, str | None]:
    """Try to extract user_id and team_id from LevelDB data."""
    user_pattern = re.compile(rb'"user_id"\s*:\s*"(U[A-Z0-9]+)"')
    # team_id can start with T or E
    team_pattern = re.compile(rb'"team_id"\s*:\s*"([TE][A-Z0-9]+)"')

    user_id = None
    team_id = None

    ldb_files = sorted(LEVELDB_DIR.glob("*.ldb"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in ldb_files:
        data = path.read_bytes()
        if user_id is None:
            m = user_pattern.search(data)
            if m:
                user_id = m.group(1).decode("ascii")
        if team_id is None:
            m = team_pattern.search(data)
            if m:
                team_id = m.group(1).decode("ascii")
        if user_id and team_id:
            break

    return user_id, team_id


def get_credentials() -> SlackCredentials:
    """Extract Slack credentials from the local desktop app.

    Returns a SlackCredentials with the xoxc token and d cookie
    needed to authenticate Slack API requests.
    """
    password = _get_keychain_password()
    key = _derive_key(password)

    token = _get_xoxc_token()
    d_cookie = _get_cookie("d", key)
    user_id, team_id = _get_user_info_from_leveldb()

    return SlackCredentials(
        token=token,
        d_cookie=d_cookie,
        user_id=user_id,
        team_id=team_id,
    )
