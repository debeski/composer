FROM python:3.14-slim

WORKDIR /app
ENV PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    age \
    && rm -rf /var/lib/apt/lists/*

RUN install -m 0755 -d /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && \
    chmod a+r /etc/apt/keyrings/docker.asc

RUN echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
    $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null

RUN apt-get update && apt-get install -y --no-install-recommends \
    docker-ce-cli \
    docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH=$(dpkg --print-architecture) && \
    SOPS_VERSION="v3.9.0" && \
    if [ "$ARCH" = "amd64" ]; then SOPS_ARCH="amd64"; elif [ "$ARCH" = "arm64" ]; then SOPS_ARCH="arm64"; else echo "Unsupported arch"; exit 1; fi && \
    curl -sL "https://github.com/getsops/sops/releases/download/${SOPS_VERSION}/sops-${SOPS_VERSION}.linux.${SOPS_ARCH}" -o /usr/local/bin/sops && \
    chmod +x /usr/local/bin/sops

COPY composer /app/composer
COPY VERSION /app/VERSION
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Forward arguments from the wrapper script to the entrypoint router
ENTRYPOINT ["/app/entrypoint.sh"]
