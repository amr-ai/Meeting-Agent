FROM python:3.13-slim
WORKDIR /opt/meeting-agent
RUN apt-get update && apt-get install -y --no-install-recommends libportaudio2 libgomp1 && rm -rf /var/lib/apt/lists/*
COPY . .
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-deps .
RUN useradd --create-home agent && mkdir -p data && chown -R agent:agent /opt/meeting-agent
USER agent
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
