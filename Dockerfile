# firefox-auto-turnstile base image.
#
# Alpine-based Firefox (jlesage/firefox) + mitmproxy + a small HTTP API.
# The container drives a real Firefox via xdotool; Turnstile runs in this
# clean browser while a MITM proxy substitutes the target page, keeping the
# address bar on the real hostname (which is what the sitekey is bound to).

# Pinned to the latest stable release at the time of writing.  Multi-arch:
# amd64, arm64 (also 386/arm, unused here).
ARG FIREFOX_IMAGE=jlesage/firefox:v26.08.3
FROM ${FIREFOX_IMAGE}

# mitmproxy: intercepts target-site documents and captures the token.
# The Alpine community repo ships a current mitmproxy, so no pip needed.
# xinput: streams X input events so the API can record click coordinates
# (used to teach/remember the Turnstile checkbox position).
RUN add-pkg mitmproxy xinput

COPY src/relay_addon.py \
     src/api_server.py \
     src/upproxy.py \
     src/nav.sh \
     /opt/turnstile-relay/

COPY rootfs/ /

RUN chmod +x /etc/services.d/mitm/run \
             /etc/services.d/api/run \
             /etc/services.d/upproxy/run \
             /etc/cont-init.d/99-relay-init \
             /opt/turnstile-relay/nav.sh

# 8081 = relay API.  (5800 web UI and 5900 VNC are inherited from the base.)
EXPOSE 8081
