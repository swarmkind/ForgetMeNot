FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    FORGETMENOT_HOST=0.0.0.0 FORGETMENOT_DATA=/data PATH=/app/.venv/bin:$PATH
RUN groupadd --gid 10001 forgetmenot && useradd --uid 10001 --gid forgetmenot --create-home forgetmenot
COPY pyproject.toml uv.lock ./
COPY forgetmenot ./forgetmenot
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev && mkdir /data && chown forgetmenot:forgetmenot /data
USER forgetmenot
VOLUME /data
EXPOSE 8787
CMD ["forgetmenot"]
