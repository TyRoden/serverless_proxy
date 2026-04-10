FROM python:3.12-slim

WORKDIR /app

# Copy requirements first, then install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .

EXPOSE 8002 5001

CMD ["python3", "simple_bridge.py"]