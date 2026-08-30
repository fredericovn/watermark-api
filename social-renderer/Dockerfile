FROM python:3.13-slim-bookworm AS base

ARG GOOGLE_FONTS_COMMIT=ade3d1533e06b2b1462ffcde8e08b129627ca360

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY social-renderer/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/assets/fonts \
 && curl --fail --location --silent --show-error \
    "https://raw.githubusercontent.com/google/fonts/${GOOGLE_FONTS_COMMIT}/ofl/poppins/Poppins-Regular.ttf" \
    -o /app/assets/fonts/Poppins-Regular.ttf \
 && curl --fail --location --silent --show-error \
    "https://raw.githubusercontent.com/google/fonts/${GOOGLE_FONTS_COMMIT}/ofl/poppins/Poppins-SemiBold.ttf" \
    -o /app/assets/fonts/Poppins-SemiBold.ttf \
 && curl --fail --location --silent --show-error \
    "https://raw.githubusercontent.com/google/fonts/${GOOGLE_FONTS_COMMIT}/ofl/playfairdisplay/PlayfairDisplay%5Bwght%5D.ttf" \
    -o /app/assets/fonts/PlayfairDisplay.ttf

COPY logo.png /app/assets/logo.png
COPY social-renderer/app.py social-renderer/renderer.py /app/

ENV PORT=8080 \
    MAX_SOURCE_BYTES=26214400 \
    GUNICORN_TIMEOUT=180
EXPOSE 8080

FROM base AS test
RUN pip install --no-cache-dir pytest==8.4.2
COPY social-renderer/tests /app/tests
CMD ["pytest", "-q", "/app/tests"]

FROM base AS runtime
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 2 --timeout ${GUNICORN_TIMEOUT} app:app"]
