"""Authentication using python-garminconnect's token store.

Credentials are never persisted by this project. On first login the user is
prompted (or may set ``GARMIN_EMAIL`` / ``GARMIN_PASSWORD`` for that one run);
python-garminconnect then writes ``garmin_tokens.json`` into the token store
directory, and later runs resume from it without a password.
"""

from __future__ import annotations

import getpass
import logging
import os

from garminconnect import Garmin, GarminConnectAuthenticationError

from .config import Settings

log = logging.getLogger(__name__)


class AuthRequired(RuntimeError):
    """Raised when no usable cached session exists and interactive login is not allowed."""


def _prompt_mfa() -> str:
    return input("Garmin MFA code: ").strip()


def get_client(settings: Settings, interactive: bool = False) -> Garmin:
    """Return a logged-in ``Garmin`` client.

    Tries the cached token store first. Falls back to credentials only when
    ``interactive`` is true (the ``login`` command) or env credentials exist.
    """
    tokenstore = str(settings.tokenstore)
    token_file = settings.tokenstore / "garmin_tokens.json"

    if token_file.exists():
        try:
            client = Garmin(is_cn=settings.is_cn)
            client.login(tokenstore)
            log.debug("Resumed Garmin session from %s", token_file)
            return client
        except GarminConnectAuthenticationError as e:
            log.warning("Cached Garmin session rejected: %s", e)
            if not interactive:
                raise AuthRequired(
                    "Cached Garmin session is invalid. Run `garmin-data login` again."
                ) from e

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not (email and password):
        if not interactive:
            raise AuthRequired(
                f"No cached Garmin session in {settings.tokenstore}. Run `garmin-data login` first."
            )
        email = email or input("Garmin email: ").strip()
        password = password or getpass.getpass("Garmin password: ")

    client = Garmin(email=email, password=password, is_cn=settings.is_cn, prompt_mfa=_prompt_mfa)
    # Passing the tokenstore path makes the library dump tokens after login.
    client.login(tokenstore)
    log.info("Logged in; session cached in %s", settings.tokenstore)
    return client
