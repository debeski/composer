FROM python:3.14-slim

WORKDIR /app
ENV PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
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

# The one dependency composer does not reimplement. `dlux-update` mirrors
# django-lux[updater]'s trust decisions, including its refusal to install a
# wheel it cannot verify — without this the whole inline update path fails
# closed, on every release, in every deployment. Same floor as the extra.
RUN pip install --no-cache-dir "pypi-attestations>=0.0.29"

COPY composer /app/composer
COPY VERSION /app/VERSION
# Reference wrappers for `composer check`: a deployment compares its start.sh /
# start.ps1 against the composer it is actually running, with no registry call.
COPY start.sh start.ps1 wrappers-history.json /app/wrappers/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Forward arguments from the wrapper script to the entrypoint router
ENTRYPOINT ["/app/entrypoint.sh"]
