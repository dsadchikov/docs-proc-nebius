# Minimal diagnostic image — no vLLM, no model, no nginx, no GPU.
# Bisects the Nebius endpoint failure: if THIS reaches RUNNING, the platform /
# create-flow / networking work and the fault is in the heavy components.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir fastapi==0.115.0 "uvicorn==0.30.0"

COPY minapp.py /app/minapp.py
WORKDIR /app

EXPOSE 8080
CMD ["uvicorn", "minapp:app", "--host", "0.0.0.0", "--port", "8080"]
