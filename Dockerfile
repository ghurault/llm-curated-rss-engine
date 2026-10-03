# The engine as a configuration repository consumes it: one image, pinned by
# digest. `base` is the environment with none of the application in it,
# `runtime` adds the package, `devcontainer` adds what a person needs instead —
# so development and the 06:00 run differ only in how the package is installed.

ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG NODE_IMAGE=node:24-trixie-slim

FROM ${NODE_IMAGE} AS node

FROM ${PYTHON_IMAGE} AS base

# Node is here because `curate-deploy` shells out to `npx wrangler`. Copied from
# the official image rather than installed with apt: Debian trixie packages Node
# 20, wrangler 4 needs 22, and no official image carries both languages.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# One release, installed rather than fetched per run: `npx wrangler@4` resolves
# the range against the registry every morning. The bare package name is npx's
# "run what is installed" form — see `wrangler_package` in deploy.py.
ARG WRANGLER_VERSION=4.127.0
ENV CURATED_FEED_WRANGLER=wrangler
RUN npm install --global --no-fund --no-audit "wrangler@${WRANGLER_VERSION}" \
    && npm cache clean --force

# Before the source, so that editing a module does not reinstall every
# dependency. The pinned application set, without the development extras.
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS devcontainer

# The hooks need git and make; the VS Code server needs libatomic1 on a slim
# base. The rest is what makes a shell habitable.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        git \
        less \
        libatomic1 \
        make \
        openssh-client \
        procps \
    && rm -rf /var/lib/apt/lists/*

# The formatters pre-commit runs as `language: system`, plus hadolint for its VS
# Code extension. Pinned here, since the hook config cannot pin a system tool.
ARG PRETTIER_VERSION=3.9.9
ARG TAPLO_VERSION=0.10.0
ARG SHFMT_VERSION=3.14.1
ARG HADOLINT_VERSION=2.15.1
RUN npm install --global --no-fund --no-audit "prettier@${PRETTIER_VERSION}" \
    && npm cache clean --force \
    && case "$(uname -m)" in \
        aarch64) taplo=aarch64 shfmt=arm64 hadolint=arm64 ;; \
        *) taplo=x86_64 shfmt=amd64 hadolint=x86_64 ;; \
    esac \
    && curl -fsSL -o /tmp/taplo.gz \
        "https://github.com/tamasfe/taplo/releases/download/${TAPLO_VERSION}/taplo-linux-${taplo}.gz" \
    && gunzip -c /tmp/taplo.gz > /usr/local/bin/taplo \
    && rm /tmp/taplo.gz \
    && curl -fsSL -o /usr/local/bin/shfmt \
        "https://github.com/mvdan/sh/releases/download/v${SHFMT_VERSION}/shfmt_v${SHFMT_VERSION}_linux_${shfmt}" \
    && curl -fsSL -o /usr/local/bin/hadolint \
        "https://github.com/hadolint/hadolint/releases/download/v${HADOLINT_VERSION}/hadolint-linux-${hadolint}" \
    && chmod 0755 /usr/local/bin/taplo /usr/local/bin/shfmt /usr/local/bin/hadolint

# postCreate installs into this interpreter as a non-root user whose uid is not
# known here, so everywhere pip installs to has to be writable by anyone. That is
# every path in its install scheme, not just the packages and the console
# scripts: a wheel carrying data files writes them under the prefix, which is
# also why the prefix itself has to be writable rather than only what is in it.
# Its own stage, so the image the pipeline runs from is untouched.
RUN chmod a+rwX /usr/local \
    && chmod -R a+rwX \
        /usr/local/bin \
        /usr/local/etc \
        /usr/local/include \
        /usr/local/lib/python3*/site-packages \
        /usr/local/share

# Last, so that a bare `docker build .` gives the image that runs the pipeline.
FROM base AS runtime

# The context deliberately has no .git for setuptools_scm to read. The default
# keeps a bare `docker build` working; CI passes the version it computed.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0+unknown

COPY pyproject.toml README.md LICENSE ./
COPY src/ src/
RUN pip install --no-cache-dir --no-deps .

# No ENTRYPOINT: Actions starts a job container with its own command and
# docker-execs each step into it, overriding whatever the image declares. The
# console scripts are on PATH, so nothing needs one.
CMD ["bash"]
