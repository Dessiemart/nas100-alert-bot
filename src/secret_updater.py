"""
Pushes updated secret values to this repo's GitHub Actions secrets via
the REST API. Used so the bot can keep its own cTrader tokens fresh
without you manually copy-pasting new values every time they rotate.

Encryption method is GitHub's own documented approach (LibSodium sealed
box) - see:
https://docs.github.com/en/rest/actions/secrets#create-or-update-a-repository-secret
"""
from base64 import b64encode

import requests
from nacl import encoding, public

GITHUB_API = "https://api.github.com"


def _encrypt(public_key_b64: str, secret_value: str) -> str:
    """GitHub's documented sealed-box encryption method. Do not modify -
    this must match their spec exactly or the secret update will be
    silently accepted but garbled, breaking the NEXT run instead of
    this one."""
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return b64encode(encrypted).decode("utf-8")


def update_repo_secret(owner: str, repo: str, pat: str, secret_name: str, secret_value: str) -> None:
    """Raises on failure - caller decides whether that's fatal."""
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    key_resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo}/actions/secrets/public-key",
        headers=headers,
        timeout=20,
    )
    key_resp.raise_for_status()
    key_data = key_resp.json()

    encrypted_value = _encrypt(key_data["key"], secret_value)

    put_resp = requests.put(
        f"{GITHUB_API}/repos/{owner}/{repo}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted_value, "key_id": key_data["key_id"]},
        timeout=20,
    )
    put_resp.raise_for_status()
