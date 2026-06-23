import os
import dj_database_url
from .base import *

# --- DEMO MODE SETTINGS ---
# Enabled to bypass strict proxy checks and show detailed errors if needed
DEBUG = True

# Allow any host to access the backend during the demo
ALLOWED_HOSTS = ['*']

# Allow your Vercel frontend (and any other origin) to connect without CORS errors
CORS_ALLOW_ALL_ORIGINS = True

# --- DATABASE CONFIGURATION ---
# Connects to Neon/Railway PostgreSQL using the DATABASE_URL variable
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600,
        ssl_require=True
    )
}