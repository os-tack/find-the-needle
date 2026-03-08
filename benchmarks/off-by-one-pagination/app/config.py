"""Application configuration."""

import os


class Config:
    DEBUG = os.getenv("FLASK_DEBUG", "0") == "1"
    HOST = os.getenv("FLASK_HOST", "0.0.0.0")
    PORT = int(os.getenv("FLASK_PORT", "5000"))
    PER_PAGE_DEFAULT = 10
    PER_PAGE_MAX = 100


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
