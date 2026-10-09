FROM docker.io/library/python:3.12.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DAGSTER_HOME=/opt/dagster/home DBT_PROFILES_DIR=/opt/analytics/dbt
WORKDIR /opt/analytics
COPY analytics/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 1000 --create-home analytics && mkdir -p /opt/dagster/home && chown -R 1000:1000 /opt/dagster /opt/analytics
COPY --chown=1000:1000 analytics/ /opt/analytics/
USER 1000:1000
CMD ["dagster", "api", "grpc", "-h", "0.0.0.0", "-p", "3030", "-m", "definitions"]
