FROM python:3.13-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home appuser
COPY --chown=appuser:appuser app.py main.py ai_client.py ./
COPY --chown=appuser:appuser static/ ./static/
USER appuser
EXPOSE 8000
CMD ["sh", "-c", "python app.py init-db && exec python -m uvicorn app:create_app --factory --host 0.0.0.0 --port 8000 --workers 2"]

FROM app AS test
COPY --chown=appuser:appuser tests/ ./tests/

FROM app AS runtime
