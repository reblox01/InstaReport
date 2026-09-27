# The Playwright image, pinned to the driver version in the lockfile.
#
# Not a convenience. The Playwright Python package carries its own Node driver
# and a browsers.json naming exact Chromium revisions, and it refuses to run a
# browser whose revision its driver does not know about. An image built on
# `python:3.11-slim` plus `playwright install chromium` therefore produces a
# *different* browser on every rebuild, weeks apart, with no way to tell from
# the outside which one a failing selector ran against.
#
# The browser channel is the only channel that has ever been implemented, and it
# has never been run against live Instagram at all -- the selector is matched
# against a committed golden fixture and a hand-transcribed DOM. A reproduction
# environment is worth more here than a small image.
#
# The tag must equal the `playwright` version in the dev environment. This image
# was built against 1.63.0 (Chromium revision 1243); a mismatch is a launch
# failure at first report, not a warning.
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
