from .base import *


DEBUG = False

ALLOWED_HOSTS = ['landscapesss.pythonanywhere.com']

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