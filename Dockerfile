FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock \
    && useradd --uid 10001 --create-home appuser
COPY *.py ./
COPY templates/ templates/
COPY static/ static/
COPY sql/ sql/
COPY deployment/ deployment/
USER appuser
EXPOSE 8000
CMD ["gunicorn", "--config", "deployment/gunicorn.conf.py", "app:app"]
