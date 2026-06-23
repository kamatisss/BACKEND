import os
import dj_database_url
from .base import *

DEBUG = True # Keep this for your demo
ALLOWED_HOSTS = ['*']
CORS_ALLOW_ALL_ORIGINS = True

# Static Files
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# Media Files
MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# CSRF Fixes
CSRF_TRUSTED_ORIGINS = [
    "https://shemireys-backend.up.railway.app",
    "https://frontend-7zgf.vercel.app",
]

# Database
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL'),
        conn_max_age=600,
        ssl_require=True
    )
}