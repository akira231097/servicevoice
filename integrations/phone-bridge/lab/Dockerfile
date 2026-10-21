FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
ENV DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update && apt-get install -y --no-install-recommends asterisk sip-tester tcpdump python3 nodejs iproute2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY integrations/phone-bridge/lab/config/ /etc/asterisk/
COPY app/static/duplex-session.js app/static/duplex-coordinator.js app/static/duplex-microphone.js /repo/app/static/
COPY integrations/phone-bridge/media.mjs integrations/phone-bridge/server.mjs /repo/integrations/phone-bridge/
COPY integrations/phone-bridge/lab/ /lab/
RUN printf '{"type":"module"}\n' > /repo/package.json && chmod -R a+rX /etc/asterisk /repo /lab
WORKDIR /out
ENTRYPOINT ["python3", "/lab/run.py"]
