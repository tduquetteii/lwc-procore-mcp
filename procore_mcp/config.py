"""Environment variables and constants. Imported first by every other module."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PUBLIC_URL      = os.environ['PUBLIC_URL'].rstrip('/')
CLIENT_ID       = os.environ['PROCORE_CLIENT_ID']
CLIENT_SECRET   = os.environ['PROCORE_CLIENT_SECRET']
COMPANY_ID      = os.environ['PROCORE_COMPANY_ID']

PROCORE_API     = 'https://api.procore.com'
PROCORE_LOGIN   = 'https://login.procore.com'
ISSUER          = f'{PUBLIC_URL}/'
REPO_URL        = 'https://github.com/tduquetteii/lwc-procore-mcp'
ICON_PATH       = Path(__file__).resolve().parent.parent / 'icon.svg'
