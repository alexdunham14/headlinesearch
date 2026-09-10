#!/bin/sh
# Turn a fresh Debian/Ubuntu box into the Headline Search database server.
# Run as root from a checkout of this repo:
#   CH_SEARCH_PASSWORD=... CH_INGEST_PASSWORD=... R2_ACCOUNT_ID=... \
#   R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... ./server/setup.sh
# Idempotent; re-run after editing anything in server/.
set -eu
cd "$(dirname "$0")/.."
: "${CH_SEARCH_PASSWORD:?}" "${CH_INGEST_PASSWORD:?}" "${R2_ACCOUNT_ID:?}" "${R2_ACCESS_KEY_ID:?}" "${R2_SECRET_ACCESS_KEY:?}"
CH_ADMIN_PASSWORD="${CH_ADMIN_PASSWORD:-$(head -c 24 /dev/urandom | base64 | tr -d '/+=')}"

# 1. ClickHouse from the official apt repo.
if ! command -v clickhouse-server >/dev/null; then
  apt-get install -y apt-transport-https ca-certificates curl gnupg ufw python3
  curl -fsSL https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key | gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg
  echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg] https://packages.clickhouse.com/deb stable main" > /etc/apt/sources.list.d/clickhouse.list
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y clickhouse-server clickhouse-client
fi

# 2. Config and users. Passwords reach the server through its environment file.
install -m 644 server/config.xml /etc/clickhouse-server/config.d/headlinesearch.xml
install -m 644 server/users.xml /etc/clickhouse-server/users.d/headlinesearch.xml
mkdir -p /etc/systemd/system/clickhouse-server.service.d
umask 077
cat > /etc/clickhouse-server/headlinesearch.env <<ENV
CH_SEARCH_PASSWORD=$CH_SEARCH_PASSWORD
CH_INGEST_PASSWORD=$CH_INGEST_PASSWORD
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

# 3. Firewall: ssh from anywhere, ClickHouse HTTP from Cloudflare only.
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
for ip in $(curl -fsS https://www.cloudflare.com/ips-v4) $(curl -fsS https://www.cloudflare.com/ips-v6); do
  ufw allow from "$ip" to any port 8080 proto tcp
done
ufw --force enable

systemctl daemon-reload
systemctl enable --now clickhouse-server
systemctl restart clickhouse-server
sleep 3
clickhouse-client --multiquery < scripts/schema.sql

# 4. Ingest: the repo at /opt/headlinesearch, run every six hours as its own user.
id -u headlines >/dev/null 2>&1 || useradd -r -m -d /var/lib/headlines -s /usr/sbin/nologin headlines
umask 077
cat > /var/lib/headlines/env <<ENV
CH_URL=http://127.0.0.1:8080
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
echo "done. next ingest: $(systemctl list-timers ingest.timer --no-legend | awk '{print $1, $2, $3}')"
