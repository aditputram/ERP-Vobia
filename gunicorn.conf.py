import os


bind = f"0.0.0.0:{os.getenv('PORT', '10000')}"
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
threads = int(os.getenv("WEB_THREADS", "4"))
timeout = int(os.getenv("WEB_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
max_requests = 1000
max_requests_jitter = 100
