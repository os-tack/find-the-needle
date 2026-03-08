FROM python:3.12-alpine

WORKDIR /app

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

COPY test.sh /test.sh
RUN chmod +x /test.sh

EXPOSE 5000

CMD ["python", "app.py"]
