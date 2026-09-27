# The Playwright image, pinned to the driver version.
#
# Stated precisely, because the obvious justification is wrong and the real one
# is better. The first draft of this comment said the base image is what makes
# the build reproducible, and that is not true: the `playwright` Python package
# bundles a browsers.json naming exact Chromium revisions, so pinning
# `playwright==1.63.0` and running `playwright install chromium` fetches the same
# revision 1243 this image carries. The pin does that work, not the base.
#
# The base image is here for the part the pin cannot do. `playwright install
# --with-deps` installs the browser's shared libraries from an Ubuntu package
# list, `python:3.11-slim` is Debian, and the result is a build step that
# depends on a distribution the browser is less tested against. So the choice is
# python:3.11-slim (small, one apt step, Debian) versus this (2.5 GB, nothing to
# go wrong) -- and the right answer for a tool whose primary channel has never
# been pointed at live Instagram is the one that cannot fail at build time. Size
# is not the constraint. "Which build failed" is.
#
# The tag must equal the `playwright` version installed below. A mismatch is a
# launch failure at the first report, not a warning -- hence the shared ARG.
ARG PLAYWRIGHT_VERSION=1.63.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

# Stated rather than inherited, because "runs as root in a container" is a
# default nobody chose. The tool opens a browser, makes HTTP requests, and
# writes a ledger; none of that needs root, and /data is the only thing it owns.
ARG UID=1000
ARG GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# The four runtime dependencies, pinned explicitly and installed before the
# source, so a one-line edit does not reinstall the world. These four are the
# complete runtime set in pyproject.toml, and a test asserts it, so this list
# cannot silently fall behind the metadata.
RUN python -m pip install --no-cache-dir \
        "playwright==${PLAYWRIGHT_VERSION}" \
        "httpx>=0.27" \
        "rich>=13.0" \
        "tenacity>=8.2"

# The package, installed rather than merely copied onto sys.path, so the console
# script exists and the installed metadata is real. Not `-e`: an editable
# install inside an image makes the installed artifact depend on the /app source
# layout, which is a property an image should not have.
COPY pyproject.toml ./
COPY insta_report ./insta_report
RUN python -m pip install --no-cache-dir --no-deps . \
 && rm -rf /root/.cache

# The operator's config is bind-mounted at run time, never copied. The example
# is here because a first run needs to see the real key names rather than infer
# them, and because the anchors file it points at has to exist somewhere.
COPY config.example.toml ./config.example.toml

RUN groupadd --gid "${GID}" reporter \
 && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin reporter \
 && mkdir -p /data \
 && chown -R "${UID}:${GID}" /data /app \
 && chmod -R go-w /app

# Declared and deliberately empty. The sessionid arrives at run time from
# compose's env_file, which names a file outside this repository.
#
# Baking a secret into a layer does not hide it. It is in the image, in the
# build cache, in `docker history`, and in anything the image is pushed to --
# and this repository is public, so a pushed image is a published secret.
#
# The variable exists and is empty so that `os.environ.get(name)` returns a
# string and the configuration's "is it set" check produces the correct
# refusal, rather than reporting a missing variable as something an operator
# cannot act on.
ENV IG_SESSIONID_ALPHA=

USER reporter

# No HEALTHCHECK, on purpose.
#
# `insta-report run` is a batch process: the container is alive exactly while
# the job is, and a health status on it reports nothing an orchestrator can act
# on. The usual next step is a check that runs `doctor --no-live`, and that
# exits 1 *by design* -- it verified the configuration and explicitly proved
# nothing about the ability to file a report, which is what its verdict says. A
# HEALTHCHECK demanding exit 0 would mark this container permanently unhealthy
# for being honest, which is how health statuses stop being read.
#
# The smoke test is a command, in the README, run by a person:
#     docker compose run --rm reporter doctor --no-live
# An exit of 1 there is the correct answer and is documented as such.
#
# No CMD that files reports. The image is a tool, not a job, and a default CMD
# that acts on whatever target list happens to be mounted is a default nobody
# remembers is there.
ENTRYPOINT ["insta-report", "--config", "/app/config.toml"]
CMD ["--help"]
