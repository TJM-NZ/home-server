FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    google-api-python-client \
    google-auth-httplib2 \
    google-auth-oauthlib

COPY backup.py .

ENTRYPOINT ["python", "-u", "backup.py"]
CMD ["backup"]
