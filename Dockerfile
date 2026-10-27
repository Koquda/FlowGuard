FROM python:3.13-slim
WORKDIR /app
COPY flowguard ./flowguard
COPY web ./web
RUN useradd --create-home --uid 10001 flowguard && mkdir /data && chown flowguard:flowguard /data
USER flowguard
ENV PYTHONUNBUFFERED=1 FLOWGUARD_DB=/data/flowguard.db PROCESSOR_DB=/data/processor.db
EXPOSE 8080
CMD ["python", "-m", "flowguard", "api"]
