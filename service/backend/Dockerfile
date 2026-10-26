# Build from the common parent: docker build -f service/backend/Dockerfile -t donghaeng-backend .
FROM python:3.12.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_MODE=final
WORKDIR /workspace/service/backend
COPY service/backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system app && useradd --system --gid app --home-dir /workspace app \
    && mkdir -p /workspace/service/backend/runtime \
    && chown -R app:app /workspace/service/backend/runtime
COPY model/final_model/__init__.py /workspace/model/final_model/__init__.py
COPY model/final_model/src /workspace/model/final_model/src
COPY model/final_model/schemas /workspace/model/final_model/schemas
COPY model/data/i5/national_2025 /workspace/model/data/i5/national_2025
COPY model/data/i5/national_2025_10group /workspace/model/data/i5/national_2025_10group
COPY service/backend/app ./app
USER app
EXPOSE 8000
# Published I5 is included; mount separately delivered activity bundles read-only.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
