FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    google-api-python-client \
    google-auth-httplib2 \
    google-auth-oauthlib \
    requests

COPY email_common.py backup.py cleanup.py ./

ENTRYPOINT ["python", "-u"]
CMD ["backup.py"]
