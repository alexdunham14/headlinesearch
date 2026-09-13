#!/bin/sh
# Turn a fresh Debian/Ubuntu box into the Headline Search database server.
# Run as root from a checkout of this repo:
#   CH_SEARCH_PASSWORD=... CH_INGEST_PASSWORD=... CH_COLLECT_PASSWORD=... R2_ACCOUNT_ID=... \
#   R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... CF_TUNNEL_TOKEN=... ./server/setup.sh
# CF_TUNNEL_TOKEN comes from server/tunnel.sh. With it the Worker reaches
# ClickHouse through a Cloudflare Tunnel: cloudflared runs here, ClickHouse
# listens on localhost only, and no port but ssh is open. Without it (how
# the box ran until the tunnel), ClickHouse listens on every interface and
# the firewall admits port 8123 from Cloudflare's published ranges.
# Idempotent; re-run after editing anything in server/.
set -eu
cd "$(dirname "$0")/.."
: "${CH_SEARCH_PASSWORD:?}" "${CH_INGEST_PASSWORD:?}" "${CH_COLLECT_PASSWORD:?}" "${R2_ACCOUNT_ID:?}" "${R2_ACCESS_KEY_ID:?}" "${R2_SECRET_ACCESS_KEY:?}"
CH_ADMIN_PASSWORD="${CH_ADMIN_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"
CF_TUNNEL_TOKEN="${CF_TUNNEL_TOKEN:-}"

# 1. ClickHouse from the official apt repo; cloudflared from Cloudflare's.
if ! command -v clickhouse-server >/dev/null; then
  apt-get install -y apt-transport-https ca-certificates curl gnupg ufw python3
  curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" > /etc/apt/sources.list.d/clickhouse.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y clickhouse-server clickhouse-client
fi
if [ -n "$CF_TUNNEL_TOKEN" ] && ! command -v cloudflared >/dev/null; then
  curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" > /etc/apt/sources.list.d/cloudflared.list
  apt-get update
  apt-get install -y cloudflared
fi

# 2. sshd: keys only is the image's default; no root login either (admin has sudo).
printf 'PermitRootLogin no\n' > /etc/ssh/sshd_config.d/headlinesearch.conf
sshd -t && systemctl reload ssh

# 3. Config and users. Passwords reach the server through its environment file.
install -m 644 server/config.xml /etc/clickhouse-server/config.d/headlinesearch.xml
install -m 644 server/users.xml /etc/clickhouse-server/users.d/headlinesearch.xml
# Where to listen. :: is a dual-stack socket that also serves IPv4; 0.0.0.0 is
# for boxes without IPv6, where :: fails; listen_try lets whichever fails be
# skipped (without it the second bind aborts the server on a dual-stack box).
if [ -n "$CF_TUNNEL_TOKEN" ]; then
  printf '<clickhouse><listen_host>127.0.0.1</listen_host><listen_host>::1</listen_host><listen_try>1</listen_try></clickhouse>\n' > /etc/clickhouse-server/config.d/listen.xml
else
  printf '<clickhouse><listen_host>::</listen_host><listen_host>0.0.0.0</listen_host><listen_try>1</listen_try></clickhouse>\n' > /etc/clickhouse-server/config.d/listen.xml
fi
mkdir -p /etc/systemd/system/clickhouse-server.service.d
umask 077
cat > /etc/clickhouse-server/headlinesearch.env <<ENV
CH_SEARCH_PASSWORD=$CH_SEARCH_PASSWORD
CH_INGEST_PASSWORD=$CH_INGEST_PASSWORD
CH_COLLECT_PASSWORD=$CH_COLLECT_PASSWORD
CH_ADMIN_PASSWORD=$CH_ADMIN_PASSWORD
ENV
chown clickhouse:clickhouse /etc/clickhouse-server/headlinesearch.env
mkdir -p /root/.clickhouse-client
cat > /root/.clickhouse-client/config.xml <<CLIENT
<config><user>default</user><password>$CH_ADMIN_PASSWORD</password></config>
CLIENT
cat > /etc/systemd/system/clickhouse-server.service.d/env.conf <<'UNIT'
[Service]
EnvironmentFile=/etc/clickhouse-server/headlinesearch.env
UNIT
umask 022

# 4. Firewall: ssh from anywhere (the cloud firewall in front narrows that to
# one address); ClickHouse HTTP from Cloudflare only, and only without the tunnel.
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
if [ -z "$CF_TUNNEL_TOKEN" ]; then
  for ip in $(curl -fsS https://www.cloudflare.com/ips-v4) $(curl -fsS https://www.cloudflare.com/ips-v6); do
    ufw allow from "$ip" to any port 8123 proto tcp
  done
fi
ufw --force enable

systemctl daemon-reload
systemctl enable --now clickhouse-server
systemctl restart clickhouse-server
sleep 3
clickhouse-client --multiquery < scripts/schema.sql
clickhouse-client --multiquery < scripts/collect_schema.sql

# 5. The tunnel: cloudflared as its own user, the token in a root-only env file.
if [ -n "$CF_TUNNEL_TOKEN" ]; then
  id -u cloudflared >/dev/null 2>&1 || useradd -r -m -d /var/lib/cloudflared -s /usr/sbin/nologin cloudflared
  mkdir -p /etc/cloudflared
  umask 077
  printf 'TUNNEL_TOKEN=%s\n' "$CF_TUNNEL_TOKEN" > /etc/cloudflared/env
  chown cloudflared:cloudflared /etc/cloudflared/env
  umask 022
  install -m 644 server/cloudflared.service /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now cloudflared
  systemctl restart cloudflared
fi

# 6. Ingest: the repo at /opt/headlinesearch, run every six hours as its own user.
id -u headlines >/dev/null 2>&1 || useradd -r -m -d /var/lib/headlines -s /usr/sbin/nologin headlines
umask 077
cat > /var/lib/headlines/env <<ENV
CH_URL=http://127.0.0.1:8123
CH_USER=ingest
CH_PASSWORD=$CH_INGEST_PASSWORD
R2_ACCOUNT_ID=$R2_ACCOUNT_ID
R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
R2_BUCKET=gdelt-gkg
ENV
chown headlines:headlines /var/lib/headlines/env
umask 022
install -m 644 server/ingest.service server/ingest.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ingest.timer

# 7. The collector (scripts/collect.py): the same user, its own ClickHouse
# user and database, every hour.
umask 077
cat > /var/lib/headlines/collect.env <<ENV
CH_URL=http://127.0.0.1:8123
CH_USER=collect
CH_PASSWORD=$CH_COLLECT_PASSWORD
ENV
chown headlines:headlines /var/lib/headlines/collect.env
umask 022
install -m 644 server/collect.service server/collect.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now collect.timer
echo "done. next ingest: $(systemctl list-timers ingest.timer --no-legend | awk '{print $1, $2, $3}')"
echo "next collect: $(systemctl list-timers collect.timer --no-legend | awk '{print $1, $2, $3}')"
