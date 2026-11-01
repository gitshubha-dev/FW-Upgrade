# Controller image: only the controller's code and dependencies.
FROM python:3.12-slim

# Print logs immediately instead of buffering them.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY controller.py db.py metrics.py config.py config.yaml ./

EXPOSE 8080
CMD ["uvicorn", "controller:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
