"""Encrypting what the platforms give us.

A platform refresh token is a standing right to post as that person. The database holds runs,
copy and sessions as plain rows — fine for a backup to pass through many hands — but not these.
The key lives in the environment on a real server (`SHORTS_SECRET_KEY`); on a laptop with no
key set, one is written next to the database so a local install works without ceremony, and the
difference is said out loud rather than hidden.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _fernet(key: bytes):
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("cryptography is not installed - pip install -e '.[web]'") from exc
    return Fernet(key)


def key_for(data_dir: Path) -> bytes:
    """The key from the environment, or a local one made once and kept beside the database."""
    from_env = os.getenv("SHORTS_SECRET_KEY")
    if from_env:
        return from_env.encode("ascii")

    path = Path(data_dir) / "secret.key"
    if not path.exists():
        from cryptography.fernet import Fernet

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # Windows: the data directory and the user profile are the protection
    return path.read_bytes()


def seal(data_dir: Path, value: dict) -> bytes:
    return _fernet(key_for(data_dir)).encrypt(json.dumps(value).encode("utf-8"))


def open_sealed(data_dir: Path, blob: bytes) -> dict:
    """Unseal, or raise. A token sealed with a key that is gone is a reconnect, not a crash."""
    from cryptography.fernet import InvalidToken

    try:
        return json.loads(_fernet(key_for(data_dir)).decrypt(bytes(blob)))
    except InvalidToken as exc:
        raise KeyError("this account was connected with a different key - reconnect it") from exc
