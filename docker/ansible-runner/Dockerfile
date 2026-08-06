FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 python3-pip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir ansible psycopg2-binary

COPY requirements.yml /tmp/requirements.yml
RUN ansible-galaxy collection install -r /tmp/requirements.yml -p /usr/share/ansible/collections

COPY docker/ansible-runner/ansible.cfg /etc/ansible/ansible.cfg
COPY docker/ansible-runner/entrypoint.sh /entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
