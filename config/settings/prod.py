from .base import *


DEBUG = False

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '.railway.app']

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