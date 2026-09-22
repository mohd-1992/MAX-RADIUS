# ==============================================================================
# Production FreeRADIUS 3.x with MySQL/MariaDB Support & MikroTik Dictionaries
# ==============================================================================
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    freeradius \
    freeradius-mysql \
    freeradius-utils \
    mariadb-client \
    curl \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Create working directory and copy entrypoint script
WORKDIR /etc/freeradius/3.0
COPY entrypoint.sh /entrypoint.sh
COPY queries.conf /etc/freeradius/3.0/mods-config/sql/main/mysql/queries.conf
RUN chmod +x /entrypoint.sh

# Expose standard FreeRADIUS Ports inside container (Auth & Acct)
EXPOSE 1812/udp
EXPOSE 1813/udp

ENTRYPOINT ["/entrypoint.sh"]