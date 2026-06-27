"""Node-RED credential-store read/write with AES-256-CTR crypto.

Why this module exists
----------------------
Node-RED stores node credentials (API tokens, passwords, etc.) in a separate
credential-store file, encrypted with AES-256-CTR using a key derived from
``credentialSecret`` in settings.js.  Two "secret-free" paths were empirically
proven NOT to work:

  (a) Inline credentials in flows.json + restart — NR ignores them on startup.
  (b) POST /flows Admin-API deploy with inline credentials — returns 200 but
      the credential never lands in the store.

The only working path: read and write the credential store file directly,
performing the AES crypto ourselves.

File format
-----------
The cred file *path* is resolved by the caller — Node-RED derives it as
``userDir / (basename(flowFile, ext) + "_cred" + ext)`` (see
``config.NodeRedConfig.effective_credentials_file``); this module only takes a
``cred_file`` path and operates on its contents.

When ``credentialSecret`` is set::

    {"$": "<32-hex-IV><base64-ciphertext>"}

  Algorithm : AES-256-CTR
  Key        : SHA-256(credentialSecret, utf-8) — 32 raw bytes
  IV         : first 16 bytes (32 hex chars) of the ``$`` string
  Ciphertext : base64-decode of the remaining string
  Plaintext  : JSON object ``{"<nodeId>": {<cred fields>}, ...}``

When ``credentialSecret: false`` (encryption disabled), the file contains the
plain ``{nodeId: {...}}`` object directly (no ``$`` wrapper).

Secret resolution order (``resolve_secret``)
--------------------------------------------
1. ``<user_dir>/settings.js`` → ``credentialSecret`` key (quoted string or
   ``false`` literal).
2. ``<user_dir>/.config.runtime.json`` or ``.config.json`` → ``_credentialSecret``.
3. Raises ``ValueError`` with a diagnostic message.

The secret is resolved transiently at call time and never stored in pairflow
config or on disk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import writer

# ---------------------------------------------------------------------------
# Internal crypto helpers
# ---------------------------------------------------------------------------


def _derive_key(secret: str) -> bytes:
    """SHA-256 of the UTF-8-encoded secret → 32-byte AES key."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def _decrypt_encoded(secret: str, encoded: str) -> dict[str, Any]:
    """Decrypt the ``$`` string value from a cred file.

    ``encoded`` = 32-hex IV + base64 ciphertext (Node-RED's format).
    Returns the decrypted JSON object.
    """
    iv_hex = encoded[:32]
    b64_part = encoded[32:]
    iv = bytes.fromhex(iv_hex)
    key = _derive_key(secret)
    ciphertext = base64.b64decode(b64_part)

    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    dec = cipher.decryptor()
    plaintext = dec.update(ciphertext) + dec.finalize()
    return json.loads(plaintext.decode("utf-8"))


def _encrypt_store_to_string(secret: str, store: dict[str, Any]) -> str:
    """Encrypt ``store`` and return the ``$`` string value.

    Generates a fresh random 16-byte IV on every call.
    Returns ``iv.hex() + base64(ciphertext)``.
    """
    iv = os.urandom(16)
    key = _derive_key(secret)
    plaintext = json.dumps(store).encode("utf-8")

    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    enc = cipher.encryptor()
    ciphertext = enc.update(plaintext) + enc.finalize()
    return iv.hex() + base64.b64encode(ciphertext).decode("ascii")


# ---------------------------------------------------------------------------
# Public pure functions
# ---------------------------------------------------------------------------


def resolve_secret(user_dir: Path) -> str | None:
    """Resolve the Node-RED credential secret from ``user_dir``.

    Returns the secret string, or ``None`` when encryption is explicitly
    disabled (``credentialSecret: false`` in settings.js).

    Raises ``ValueError`` with a diagnostic message when no secret can be
    determined — either because settings.js is absent/has no
    ``credentialSecret`` key *and* neither .config.runtime.json nor
    .config.json carries ``_credentialSecret``.
    """
    user_dir = Path(user_dir)

    # --- 1. settings.js ----------------------------------------------------
    settings_js = user_dir / "settings.js"
    if settings_js.is_file():
        try:
            text = settings_js.read_text(encoding="utf-8")
        except OSError:
            pass
        else:
            # Match:  credentialSecret: "value"
            #         credentialSecret: 'value'
            #         credentialSecret: false
            m = re.search(
                r"""credentialSecret\s*:\s*(?:"([^"\\]*)"|'([^'\\]*)'|(false))""",
                text,
            )
            if m:
                if m.group(3):  # literal `false` → encryption disabled
                    return None
                return m.group(1) if m.group(1) is not None else m.group(2)

    # --- 2. .config.runtime.json / .config.json ----------------------------
    for config_name in (".config.runtime.json", ".config.json"):
        config_file = user_dir / config_name
        if config_file.is_file():
            try:
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
                secret = cfg.get("_credentialSecret")
                if secret is not None:
                    return str(secret)
            except Exception:  # noqa: BLE001 — corrupt file, keep trying
                pass

    raise ValueError(
        f"Cannot determine the Node-RED credential secret. Searched:\n"
        f"  {user_dir / 'settings.js'} — credentialSecret key\n"
        f"  {user_dir / '.config.runtime.json'} — _credentialSecret key\n"
        f"  {user_dir / '.config.json'} — _credentialSecret key\n"
        "Set credentialSecret in settings.js (or ensure one of the config "
        "files is present and readable) and restart Node-RED."
    )


def decrypt_store(raw: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """Decrypt a parsed cred-file JSON object into a ``{nodeId: {...}}`` store.

    ``raw``   : the dict loaded directly from the cred file.
    ``secret``: the credential secret (or ``None`` for plaintext stores).

    When ``secret`` is ``None``, the file is unencrypted and ``raw`` is
    already the store — returned as-is.

    When the ``$`` key is present, the value is decrypted with AES-256-CTR.
    """
    if secret is None:
        # Encryption disabled; the file IS the store.
        return raw
    if "$" not in raw:
        # Plaintext store despite a secret being configured — treat it as-is.
        # (Happens with empty / freshly-initialised NR installs.)
        return raw
    return _decrypt_encoded(secret, raw["$"])


def encrypt_store(store: dict[str, Any], secret: str | None) -> dict[str, Any]:
    """Encrypt a ``{nodeId: {...}}`` store for writing to the cred file.

    Returns ``{"$": "<encoded>"}`` when ``secret`` is provided, or the plain
    store dict when ``secret`` is ``None`` (encryption disabled).
    """
    if secret is None:
        return store
    return {"$": _encrypt_store_to_string(secret, store)}


def read_store(cred_file: Path, secret: str | None) -> dict[str, Any]:
    """Read and decrypt the credential store from ``cred_file``.

    Raises ``FileNotFoundError`` if the file does not exist (caller decides
    whether to treat that as an empty store or an error).
    """
    raw: dict[str, Any] = json.loads(cred_file.read_text(encoding="utf-8"))
    return decrypt_store(raw, secret)


def set_node_credentials(
    cred_file: Path,
    node_id: str,
    creds: dict[str, Any],
    secret: str | None,
    *,
    merge: bool = True,
) -> dict[str, Any]:
    """Write (or merge) credentials for ``node_id`` into the cred store.

    Follows the same optimistic-locking pattern as the flows mutating helpers:
    capture mtime → read → mutate → atomic-write with expected_mtime.

    When the cred file does not yet exist it is created (no backup, no mtime
    check — there is nothing to protect).

    Parameters
    ----------
    cred_file:
        Path to the credential store file (e.g. ``flows_homeassistant_cred.json``).
    node_id:
        Node-RED node id whose credentials to write.
    creds:
        Credential fields, e.g. ``{"token": "abc123"}``.  Values are written
        to disk encrypted; they are never returned or logged.
    secret:
        The credential secret (from ``resolve_secret``).
    merge:
        When ``True`` (default), existing fields for ``node_id`` are kept and
        ``creds`` is merged on top.  When ``False``, ``node_id``'s entry is
        replaced entirely.

    Returns
    -------
    ``{node_id, keys_set, merged}`` — key names only, no values.
    """
    cred_file = Path(cred_file)

    try:
        mtime: float | None = writer.current_mtime(cred_file)
        store = read_store(cred_file, secret)
    except FileNotFoundError:
        mtime = None
        store = {}

    existing = store.get(node_id, {}) if merge else {}
    store[node_id] = {**existing, **creds}

    writer.atomic_write_json(
        cred_file,
        encrypt_store(store, secret),
        expected_mtime=mtime,
    )

    return {
        "node_id": node_id,
        "keys_set": sorted(creds.keys()),
        "merged": merge and bool(existing),
    }


def delete_node_credentials(
    cred_file: Path,
    node_id: str,
    secret: str | None,
    *,
    missing_ok: bool = False,
) -> dict[str, Any]:
    """Remove credentials for ``node_id`` from the cred store.

    Returns ``{"deleted": node_id, "found": True}`` on success.
    Returns ``{"deleted": None, "found": False}`` when the node has no
    credentials and ``missing_ok=True``.
    Raises ``KeyError`` when not found and ``missing_ok=False``.
    """
    cred_file = Path(cred_file)

    try:
        mtime: float | None = writer.current_mtime(cred_file)
        store = read_store(cred_file, secret)
    except FileNotFoundError:
        if missing_ok:
            return {"deleted": None, "found": False}
        raise KeyError(f"Credential file not found: {cred_file}") from None

    if node_id not in store:
        if missing_ok:
            return {"deleted": None, "found": False}
        raise KeyError(f"No credentials stored for node {node_id!r}")

    del store[node_id]
    writer.atomic_write_json(
        cred_file,
        encrypt_store(store, secret),
        expected_mtime=mtime,
    )
    return {"deleted": node_id, "found": True}


def list_node_credentials(
    cred_file: Path,
    secret: str | None,
) -> dict[str, Any]:
    """List all nodes that have stored credentials, with key names only.

    Credential *values* are never returned.  Only the field names (e.g.
    ``["password", "username"]``) are included per node so the caller can
    verify which fields are present without reading sensitive data.

    Returns ``{"count": int, "nodes": [{"node_id": str, "keys": [str, ...]}, …]}``.
    When the cred file does not exist, returns ``{"count": 0, "nodes": []}``.
    """
    cred_file = Path(cred_file)

    try:
        store = read_store(cred_file, secret)
    except FileNotFoundError:
        return {"count": 0, "nodes": []}

    nodes = [
        {"node_id": nid, "keys": sorted(creds.keys())}
        for nid, creds in sorted(store.items())
        if isinstance(creds, dict)
    ]
    return {"count": len(nodes), "nodes": nodes}
