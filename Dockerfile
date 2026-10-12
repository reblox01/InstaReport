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

# Re-declared, and this line is load-bearing rather than tidy. An ARG declared
# before FROM is a *global* ARG: it is substituted into the FROM line and then
# goes out of scope for the stage. Without this line the pin below expands to
# `playwright==`, pip rejects that as an invalid requirement, and the message
# names neither the ARG nor the cause. A static read of this file shows the pin
# present and plausible.
ARG PLAYWRIGHT_VERSION

# No uid is declared here, and the absence is the fix rather than an omission.
#
# An earlier revision created its own user: `ARG UID=1000` / `ARG GID=1000` and
# a `groupadd reporter`. The build failed with `groupadd` exit 4, which is "GID
# not unique" -- 1000 is the base image's own `ubuntu` account and 1001 is
# `pwuser`. The mistake was not the number; it was choosing one at all. A uid
# written into this file is a claim about what the base image has not already
# used, and nothing but a build can check that claim. So the claim is made
# smaller: the Playwright images ship `pwuser` for exactly this, and the `USER`
# below names it.
#
# That is still an assumption about the base image, so it is verified rather
# than trusted -- a test builds this image and refuses it if `id -u` inside is 0.

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
# is here so a first run can read the real key names instead of guessing them,
# and so `docker run --rm insta-report:local cat /app/config.example.toml` is
# answerable without a clone. Nothing resolves it at run time; the entrypoint
# points at /app/config.toml, which is the mount.
#
# `.dockerignore` excludes `config.*.toml` and re-includes this one file. A test
# asserts both halves of that; without it the pair is a trap.
COPY config.example.toml ./config.example.toml

# The one path the tool writes to. Note that this ownership is the *image's*
# default and a bind mount replaces it: compose mounts ./artifacts over /data,
# and the host directory's owner is what matters there -- which is why the
# compose file names the uid an operator has to match by hand.
RUN mkdir -p /data \
 && chown pwuser:pwuser /data \
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

USER pwuser

# No HEALTHCHECK, on purpose.
#
# `insta-report run` is a batch process: the container is alive exactly while
# the job is, and a health status on it reports nothing an orchestrator can act
# on. The usual next step is a check that runs `doctor --no-live`, and that
# exits 2 *by design* -- `EXIT_REFUSED`, because it verified the configuration
# and explicitly proved nothing about the ability to file a report, which is
# what its verdict says. (Exit 0 would mean a channel was actually rehearsed;
# exit 1 is a `run` verdict and never a `doctor --no-live` one.) A
# HEALTHCHECK demanding exit 0 would mark this container permanently unhealthy
# for being honest, which is how health statuses stop being read.
#
# The smoke test is a command, in the README, run by a person:
#     docker compose run --rm reporter doctor --no-live
# An exit of 2 there is the correct answer and is documented as such.
#
# No CMD that files reports. The image is a tool, not a job, and a default CMD
# that acts on whatever target list happens to be mounted is a default nobody
# remembers is there.
ENTRYPOINT ["insta-report", "--config", "/app/config.toml"]
CMD ["--help"]
