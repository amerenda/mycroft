"""GitHub App authentication for the agent runtime.

Generates short-lived installation tokens from GitHub App credentials.
Used by git tools for clone/push auth and by gh CLI for API access.
"""

from __future__ import annotations

import logging
import os
import time

import httpx
import jwt

log = logging.getLogger(__name__)

# Cache the token (valid for 1 hour, we refresh at 50 min)
_cached_token: str = ""
_token_expires_at: float = 0


def _get_app_credentials() -> tuple[str, str, str]:
    """Read GitHub App credentials from env vars."""
    app_id = os.environ.get("GITHUB_APP_ID", "")
    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
    return app_id, installation_id, private_key


def _generate_jwt(app_id: str, private_key: str) -> str:
    """Generate a JWT for GitHub App authentication (valid 10 minutes)."""
    now = int(time.time())
    payload = {
        "iat": now - 60,  # issued at (60s clock skew buffer)
        "exp": now + (10 * 60),  # expires in 10 minutes
        "iss": app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def get_installation_token() -> str:
    """Get a GitHub installation token, using cache if valid.

    Returns empty string if App credentials are not configured.
    Falls back to GITHUB_TOKEN env var if App auth fails.
    """
    global _cached_token, _token_expires_at

    # Return cached token if still valid (50 min buffer)
    if _cached_token and time.time() < _token_expires_at:
        return _cached_token

    app_id, installation_id, private_key = _get_app_credentials()

    if not all([app_id, installation_id, private_key]):
        # Fall back to PAT
        pat = os.environ.get("GITHUB_TOKEN", "")
        if pat:
            log.info("GitHub App not configured, falling back to GITHUB_TOKEN")
        return pat

    try:
        app_jwt = _generate_jwt(app_id, private_key)

        resp = httpx.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        _cached_token = data["token"]
        # Token is valid for 1 hour, refresh at 50 minutes
        _token_expires_at = time.time() + (50 * 60)
        log.info("Generated GitHub App installation token (expires in 60m)")
        return _cached_token

    except Exception as e:
        log.error("Failed to generate GitHub App token: %s", e)
        # Fall back to PAT
        pat = os.environ.get("GITHUB_TOKEN", "")
        if pat:
            log.info("Falling back to GITHUB_TOKEN")
        return pat


def get_bot_identity() -> tuple[str, str]:
    """Return (name, email) for the GitHub App bot user.

    GitHub Apps commit as '<app-slug>[bot]' with a noreply email.
    Falls back to generic identity if App ID not available.
    """
    app_id = os.environ.get("GITHUB_APP_ID", "")
    if app_id:
        # GitHub convention: <app-name>[bot] with the App's bot user ID
        # The bot user ID = app_id for installation tokens
        return "coder-bot[bot]", f"{app_id}+coder-bot[bot]@users.noreply.github.com"
    return "mycroft-agent", "mycroft@amerenda.com"
