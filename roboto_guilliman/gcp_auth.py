"""Local GCP auth: ADC on Cloud Run/GCE; gcloud user token for local dev only."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from google.auth.credentials import Credentials as AuthCredentials
from google.auth.exceptions import DefaultCredentialsError
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

_GCLOUD_TOKEN_TTL_SECONDS = 3000
_cached_gcloud_credentials: tuple[Credentials, float] | None = None


def _running_on_gcp() -> bool:
    """Cloud Run, GCE, and other GCP runtimes expose metadata-based ADC."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("K_REVISION"))


def _adc_configured() -> bool:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    adc_paths = [
        Path(os.environ.get("APPDATA", "")) / "gcloud/application_default_credentials.json",
        Path.home() / ".config/gcloud/application_default_credentials.json",
    ]
    return any(path.exists() for path in adc_paths)


def _gcloud_executable() -> str | None:
    gcloud = shutil.which("gcloud")
    if gcloud is not None:
        return gcloud
    default = Path.home() / "AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"
    if default.exists():
        return str(default)
    return None


def gcloud_user_credentials() -> Credentials:
    global _cached_gcloud_credentials
    now = time.time()
    if _cached_gcloud_credentials is not None:
        credentials, cached_at = _cached_gcloud_credentials
        if now - cached_at < _GCLOUD_TOKEN_TTL_SECONDS:
            return credentials

    gcloud = _gcloud_executable()
    if gcloud is None:
        raise FileNotFoundError(
            "gcloud not found; run `gcloud auth application-default login` or install the Cloud SDK."
        )
    token = subprocess.check_output([gcloud, "auth", "print-access-token"], text=True).strip()
    credentials = Credentials(token=token)
    _cached_gcloud_credentials = (credentials, now)
    return credentials


def optional_local_credentials() -> AuthCredentials | None:
    """Return explicit credentials for local dev; None lets clients use runtime ADC."""
    if _running_on_gcp():
        return None
    if _adc_configured():
        return None
    try:
        import google.auth

        google.auth.default()
        return None
    except DefaultCredentialsError:
        pass
    gcloud = _gcloud_executable()
    if gcloud is None:
        raise DefaultCredentialsError(
            "No GCP credentials found. On Cloud Run use the runtime service account; "
            "locally run `gcloud auth application-default login`."
        )
    logger.info("ADC not found; using gcloud user access token")
    return gcloud_user_credentials()
