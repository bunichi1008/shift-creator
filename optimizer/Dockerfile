FROM python:3.12-slim
WORKDIR /app/optimizer
COPY optimizer/pyproject.toml ./
COPY optimizer/shift_optimizer ./shift_optimizer
RUN pip install --no-cache-dir . && adduser --system --uid 10001 --group app
COPY contracts/demo.json /app/contracts/demo.json
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DEMO_DATA_PATH=/app/contracts/demo.json PORT=8080
USER 10001
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn shift_optimizer.api:create_app --factory --host 0.0.0.0 --port \"$PORT\""]
