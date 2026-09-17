FROM alpine:3.19
RUN apk add --no-cache xl2tpd ppp iptables iproute2 bash procps coreutils socat mariadb-client
RUN mkdir -p /etc/xl2tpd /etc/ppp /var/run/xl2tpd /var/log/xl2tpd
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 1701/udp
ENTRYPOINT ["/entrypoint.sh"]
