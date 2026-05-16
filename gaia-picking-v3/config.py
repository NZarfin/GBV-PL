import os

IMAP_HOST     = os.getenv("IMAP_HOST",     "imap.strato.de")
IMAP_PORT     = int(os.getenv("IMAP_PORT", 993))
IMAP_USER     = os.getenv("IMAP_USER",     "Pickinglists@gaiaherbs.nl")
IMAP_PASS     = os.getenv("IMAP_PASS",     "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 60))
BASE_URL      = os.getenv("BASE_URL",      "http://localhost:5000")

SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.strato.de")
SMTP_PORT     = int(os.getenv("SMTP_PORT", 587))
SMTP_USER     = os.getenv("SMTP_USER",     IMAP_USER)
SMTP_PASS     = os.getenv("SMTP_PASS",     IMAP_PASS)

SECRET_KEY    = os.getenv("SECRET_KEY",    "change-me-before-deploy")
DB_PATH       = os.getenv("DB_PATH",       "picking.db")
