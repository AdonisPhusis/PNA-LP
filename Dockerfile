FROM python:3.13-slim

# System deps for python-bitcoinlib and web3
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache — only rebuilds when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc && apt-get autoremove -y

# Copy application code
COPY server.py .
COPY sdk/ sdk/
COPY static/ static/

EXPOSE 8080

# Runtime config via env vars:
#   LP_ID       - LP identifier (default: lp_pna_01)
#   LP_NAME     - Display name (default: pna LP)
#   PORT        - Server port (default: 8080)
#
# Mount points (docker run -v):
#   /root/.BathronKey    - Private keys (read-only)
#   /root/.bathron       - BATHRON data dir (flowswap DB persisted here)
#   /usr/local/bin       - bathron-cli + bitcoin-cli binaries
#   /root/.bitcoin-signet - Bitcoin Signet datadir (read-only)

CMD ["python3", "server.py"]
