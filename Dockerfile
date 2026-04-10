FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install uvicorn

COPY . .

EXPOSE 8002

CMD ["uvicorn", "simple_bridge:app", "--host", "0.0.0.0", "--port", "8002"]