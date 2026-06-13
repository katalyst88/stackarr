FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stackarr/ stackarr/
COPY run.py .

ENV STACKARR_DATA=/config
VOLUME /config
EXPOSE 8484

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('STACKARR_PORT','8484')+'/api/health')" || exit 1

CMD ["python", "run.py"]
