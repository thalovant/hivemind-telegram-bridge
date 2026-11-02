FROM python:3.14-slim

WORKDIR /app
COPY . /app

# force the current hivemind-bus-client alpha rather than whatever a stale
# base layer might already have cached, since this bridge depends on the
# run_forever()-after-connect() fix and the current identity/handshake API
RUN pip install --no-cache-dir --upgrade "hivemind-bus-client>=1.0.13a1" \
    && pip install --no-cache-dir .

# The bridge reads its settings from the environment (TELEGRAM_BOT_TOKEN,
# HIVEMIND_ACCESS_KEY, HIVEMIND_PASSWORD, HIVEMIND_HOST, ...), or a secret
# from the file named by <VARIABLE>_FILE, so no secret is ever passed on
# the command line where other processes can read it. Nothing secret is
# baked into the image.
ENV HIVEMIND_HOST=ws://127.0.0.1 \
    HIVEMIND_PORT=5678 \
    HIVEMIND_SITE_ID=telegram \
    HIVEMIND_LANG=en-us

# An unprivileged user. Its home holds the HiveMind identity file
# (~/.config/hivemind), which must persist across restarts: mount a volume
# there, or the hub sees a new identity key on every start.
RUN useradd --create-home --uid 10001 bridge
USER bridge

ENTRYPOINT ["hivemind-telegram-bridge"]
