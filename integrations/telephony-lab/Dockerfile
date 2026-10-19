FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac
ENV DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod +x /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends asterisk sip-tester tcpdump python3 iproute2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY config/ /etc/asterisk/
RUN chmod -R a+rX /etc/asterisk
COPY scenario.xml lab.py verify_pcap.py /lab/
WORKDIR /out
ENTRYPOINT ["python3", "/lab/lab.py"]
