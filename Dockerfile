FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY pandacode-run.sh .

EXPOSE 8770
VOLUME ["/root/.anker-tou"]
CMD ["python", "app/server.py", "--host", "0.0.0.0", "--port", "8770"]
