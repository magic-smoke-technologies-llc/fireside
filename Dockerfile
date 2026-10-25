FROM python:3.12-slim
WORKDIR /app
# MySQL backend support; SQLite is the default and needs no extra package.
# pypdf + pdfminer.six are the PDF importer (PyMuPDF is AGPL and is not in here
# on purpose). python-docx is not needed: docx is read from its own zip parts.
# pins live in requirements.txt so CI installs exactly what the image runs
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 \
    FIRESIDE_DATA=/data \
    FIRESIDE_PLUGINS_ROOT=plugins
# run as a non-root user; the app only ever writes under /data
RUN useradd -r -u 10001 -m fireside \
 && mkdir -p /data && chown -R fireside:fireside /app /data
# Start as root only long enough to make the /data mount writable by the app
# user (a host bind mount is often owned by root), then drop to fireside for
# real. See docker-entrypoint.py; the app process itself stays unprivileged.
ENTRYPOINT ["python3", "/app/docker-entrypoint.py"]
EXPOSE 8788
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8788/health').status==200 else 1)"
# the v2 host: plugins discovered from ./plugins, state on the /data volume
CMD ["python3", "-m", "host.app", "--host", "0.0.0.0", "--port", "8788"]
