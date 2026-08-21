# VOBIA ERP — Deployment Checklist

Production deployment is intentionally not executed yet.

Before staging/production:

- choose the private hosting vendor and region;
- provision managed PostgreSQL; never use local SQLite;
- generate unique application/database secrets in a secret manager;
- enforce HTTPS, secure cookies, CSRF trusted origin, SSL redirect, and approved HSTS scope;
- provision private raw-file/object storage and deny public access;
- run migrations, `check --deploy`, full tests, and a staging restore test;
- define encrypted backup schedule, retention, restore RTO/RPO, and evidence owner;
- enable monitoring for 5xx, login lockout, job/import failures, storage, database, and reconciliation;
- restrict network/database access and establish log retention;
- migrate data through staged import/approval—never direct table edits;
- complete UAT and seven consecutive parallel-run cycles;
- document rollback/fallback to Google Sheets and obtain Adit's explicit cutover approval.

Production flags expected:

```text
DJANGO_DEBUG=0
VOBIA_USE_SQLITE=0
DJANGO_SECURE_SSL_REDIRECT=1
DJANGO_SECURE_HSTS_SECONDS=31536000
SESSION_COOKIE_SECURE=True (derived)
CSRF_COOKIE_SECURE=True (derived)
```

`DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` and preload must only be enabled after the final domain/subdomain scope is confirmed; HSTS is intentionally not guessed.
