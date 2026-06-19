"""Local GCP auth: ADC when present, else active gcloud user token."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from google.auth.credentials import Credentials as AuthCredentials
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)


def _adc_configured() -> bool:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return True
    adc_paths = [
        Path(os.environ.get("APPDATA", "")) / "gcloud/application_default_credentials.json",
        Path.home() / ".config/gcloud/application_default_credentials.json",
    ]
    return any(path.exists() for path in adc_paths)


def _gcloud_executable() -> str:
    gcloud = shutil.which("gcloud")
    if gcloud is not None:
        return gcloud
    default = Path.home() / "AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"
    if default.exists():
        return str(default)
    raise FileNotFoundError(
        "gcloud not found; run `gcloud auth application-default login` or install the Cloud SDK."
    )


def gcloud_user_credentials() -> Credentials:
    gcloud = _gcloud_executable()
    token = subprocess.check_output([gcloud, "auth", "print-access-token"], text=True).strip()
    return Credentials(token=token)


def optional_local_credentials() -> AuthCredentials | None:
    """Return gcloud user credentials when ADC is not configured."""
    if _adc_configured():
        return None
    logger.info("ADC not found; using gcloud user access token")
    return gcloud_user_credentials()
