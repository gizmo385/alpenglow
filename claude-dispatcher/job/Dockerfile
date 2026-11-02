# The image every Claude job runs in, plus the egress proxy in front of it.
# Built on the *job* daemon (rootless), not the main one: see ../README.md.
FROM node:24-slim

# Pinned; bump deliberately and rebuild.
ARG CLAUDE_CODE_VERSION=2.1.309
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

COPY egress-proxy.js /opt/egress-proxy.js

# The node user (uid 1000). Jobs run with a read-only root filesystem and
# tmpfs for $HOME and /work.
USER node
WORKDIR /work
ENV CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_AUTOUPDATER=1
ENTRYPOINT ["claude"]
