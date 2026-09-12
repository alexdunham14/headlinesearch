#!/bin/sh
# Create the Cloudflare Tunnel that carries the Worker's requests to ClickHouse
# and point db.newsheadlinesearch.com at it. Run once, from anywhere, with an
# API token that has Account > Cloudflare Tunnel > Edit, Zone > Zone > Read
# and Zone > DNS > Edit on the zone:
#   CF_API_TOKEN=... ./server/tunnel.sh
# Prints the tunnel token that server/setup.sh takes as CF_TUNNEL_TOKEN.
# Idempotent: a tunnel of the same name is reused, the record overwritten.
set -eu
: "${CF_API_TOKEN:?}"
ZONE=newsheadlinesearch.com; HOST="db.$ZONE"; NAME=headlinesearch-db
API=https://api.cloudflare.com/client/v4
api() { curl -fsS -H "Authorization: Bearer $CF_API_TOKEN" -H 'Content-Type: application/json' "$@"; }
j() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }
zone_json=$(api "$API/zones?name=$ZONE")
zone=$(printf '%s' "$zone_json" | j 'd["result"][0]["id"]')
acct=$(printf '%s' "$zone_json" | j 'd["result"][0]["account"]["id"]')
id=$(api "$API/accounts/$acct/cfd_tunnel?name=$NAME&is_deleted=false" | j 'next((t["id"] for t in d["result"]), "")')
if [ -z "$id" ]; then
  id=$(api -X POST "$API/accounts/$acct/cfd_tunnel" --data "{\"name\":\"$NAME\",\"config_src\":\"cloudflare\"}" | j 'd["result"]["id"]')
  echo "created tunnel $NAME"
fi
# Only the one hostname reaches ClickHouse; anything else the tunnel answers 404.
api -X PUT "$API/accounts/$acct/cfd_tunnel/$id/configurations" \
  --data "{\"config\":{\"ingress\":[{\"hostname\":\"$HOST\",\"service\":\"http://127.0.0.1:8123\"},{\"service\":\"http_status:404\"}]}}" >/dev/null
rec=$(api "$API/zones/$zone/dns_records?name=$HOST" | j 'next((r["id"] for r in d["result"]), "")')
body="{\"type\":\"CNAME\",\"name\":\"db\",\"content\":\"$id.cfargotunnel.com\",\"proxied\":true,\"ttl\":1}"
if [ -n "$rec" ]; then api -X PUT "$API/zones/$zone/dns_records/$rec" --data "$body" >/dev/null
else api -X POST "$API/zones/$zone/dns_records" --data "$body" >/dev/null; fi
echo "tunnel $NAME is $id; $HOST -> $id.cfargotunnel.com (proxied)"
echo "CF_TUNNEL_TOKEN for server/setup.sh:"
api "$API/accounts/$acct/cfd_tunnel/$id/token" | j 'd["result"]'
