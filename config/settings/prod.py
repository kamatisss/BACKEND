import os
import dj_database_url
from .base import *

DEBUG = False

# Railway and Vercel host configuration
ALLOWED_HOSTS = ['localhost', '127.0.0.1', '.railway.app']

# Security: CORS settings
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://frontend-7zgf.vercel.app",
    "https://shemireys-backend.up.railway.app",
]

# Dynamically add the frontend URL from environment variable if set
FRONTEND_URL = os.environ.get('FRONTEND_URL')
if FRONTEND_URL:
    clean_url = FRONTEND_URL.rstrip('/')
    if clean_url not in CORS_ALLOWED_ORIGINS:
        CORS_ALLOWED_ORIGINS.append(clean_url)

# Database configuration using Railway/Neon environment variables
# Ensure your DATABASE_URL is set in Railway Variables as:
# postgres://USER:PASSWORD@HOST:PORT/NAME?sslmode=require
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600,
        ssl_require=True
    )
}