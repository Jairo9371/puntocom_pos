"""Gunicorn para la red privada compartida exclusivamente con Caddy."""

import os

bind = '0.0.0.0:8000'
workers = int(os.getenv('WEB_WORKERS', '2'))
worker_class = 'gthread'
threads = 2
timeout = 60
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
accesslog = '-'
errorlog = '-'
# No registrar query strings ni cookies, que pueden contener datos personales.
access_log_format = '%(h)s %(m)s %(U)s %(s)s %(L)s'
capture_output = True
# Solo Caddy alcanza este puerto; Compose no lo publica en el servidor.
# Caddy sobrescribe los tres encabezados de esquema que Gunicorn acepta.
forwarded_allow_ips = '*'
secure_scheme_headers = {'X-FORWARDED-PROTO': 'https'}
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190
