from .base import *


DEBUG = False

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '.railway.app']

# Disable allowing all origins in production for security
CORS_ALLOW_ALL_ORIGINS = False

# Allow specified origins
CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "https://frontend-7zgf.vercel.app",  
]

# Dynamically add the frontend URL from environment variable if set
import os
FRONTEND_URL = os.environ.get('FRONTEND_URL')
if FRONTEND_URL:
    clean_url = FRONTEND_URL.rstrip('/')
    if clean_url not in CORS_ALLOWED_ORIGINS:
        CORS_ALLOWED_ORIGINS.append(clean_url)

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'neondb',
        'USER': 'neondb_owner',
        'PASSWORD': 'npg_7sqpUcoYL4en',  # Put the password you just unhidden here
        'HOST': 'ep-soft-lake-adeezirr-pooler.c-2.us-east-1.aws.neon.tech',
        'PORT': '5432',
        'OPTIONS': {
            'sslmode': 'require',
        },
    }
}