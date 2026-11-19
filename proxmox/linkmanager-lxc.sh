#!/usr/bin/env bash
# Creates a new Proxmox VE LXC container and installs linkManager in it.
#
# Run it as root in the Proxmox VE host shell:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/arcsurf/linkManager/main/proxmox/linkmanager-lxc.sh)"
# or download it, review it and run it with options:
#   curl -fsSLO https://raw.githubusercontent.com/arcsurf/linkManager/main/proxmox/linkmanager-lxc.sh
#   bash linkmanager-lxc.sh --help
set -euo pipefail

REPO="arcsurf/linkManager"
REF="main"
CTID=""
HOSTNAME_CT="linkmanager"
STORAGE=""
TEMPLATE_STORAGE="local"
DISK_GB=4
MEMORY_MB=512
SWAP_MB=512
CORES=1
BRIDGE="vmbr0"
IP="dhcp"
GATEWAY=""
VLAN=""
LANGUAGE="en"
SSH_KEY_FILE=""
SOURCE=""
ASSUME_YES=0
ONBOOT=1
CREATED=0

usage() {
    cat <<EOF
Creates a Debian LXC container on this Proxmox VE host and installs linkManager in it.

Usage: $0 [options]

Container:
  -i, --ctid ID             Container ID (default: next free ID)
  -n, --hostname NAME       Hostname (default: $HOSTNAME_CT)
  -s, --storage NAME        Storage for the root disk (default: local-lvm, local-zfs or the first one available)
  -t, --template-storage N  Storage for the Debian template (default: $TEMPLATE_STORAGE)
  -d, --disk GB             Disk size in GB (default: $DISK_GB)
  -m, --memory MB           RAM in MB (default: $MEMORY_MB)
  -c, --cores N             CPU cores (default: $CORES)
      --no-onboot           Do not start the container when the host boots

Network:
  -b, --bridge NAME         Network bridge (default: $BRIDGE)
      --ip CIDR|dhcp        IPv4 address, for example 192.0.2.20/24 (default: dhcp)
      --gw IP               IPv4 gateway (required with a static IP)
      --vlan TAG            VLAN tag

linkManager:
  -l, --lang en|es          Initial panel language (default: $LANGUAGE)
      --ref REF             Branch, tag or commit to install from GitHub (default: $REF)
      --repo OWNER/NAME     GitHub repository (default: $REPO)
      --source PATH         Install from a local copy on this host instead of GitHub
                            (a linkManager folder or a .tar.gz of it)
      --ssh-key FILE        Public SSH key to authorize for root in the container

  -y, --yes                 Do not ask for confirmation
  -h, --help                Show this help
EOF
}

die() {
    echo "ERROR: $*" >&2
    if [ "$CREATED" = 1 ]; then
        echo "The container $CTID was kept to check what happened (pct enter $CTID)." >&2
        echo "To delete it: pct stop $CTID; pct destroy $CTID --purge" >&2
    fi
    exit 1
}
info() { echo "==> $*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        -i|--ctid) CTID="${2:?}"; shift ;;
        -n|--hostname) HOSTNAME_CT="${2:?}"; shift ;;
        -s|--storage) STORAGE="${2:?}"; shift ;;
        -t|--template-storage) TEMPLATE_STORAGE="${2:?}"; shift ;;
        -d|--disk) DISK_GB="${2:?}"; shift ;;
        -m|--memory) MEMORY_MB="${2:?}"; shift ;;
        -c|--cores) CORES="${2:?}"; shift ;;
        --no-onboot) ONBOOT=0 ;;
        -b|--bridge) BRIDGE="${2:?}"; shift ;;
        --ip) IP="${2:?}"; shift ;;
        --gw) GATEWAY="${2:?}"; shift ;;
        --vlan) VLAN="${2:?}"; shift ;;
        -l|--lang) LANGUAGE="${2:?}"; shift ;;
        --ref) REF="${2:?}"; shift ;;
        --repo) REPO="${2:?}"; shift ;;
        --source) SOURCE="${2:?}"; shift ;;
        --ssh-key) SSH_KEY_FILE="${2:?}"; shift ;;
        -y|--yes) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

# --- Checks ------------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "run it as root on the Proxmox VE host"
for c in pct pveam pvesm pvesh; do
    command -v "$c" >/dev/null 2>&1 || die "'$c' not found: this script must run on a Proxmox VE host"
done
[[ "$LANGUAGE" =~ ^(en|es)$ ]] || die "--lang must be en or es"
[[ "$DISK_GB" =~ ^[0-9]+$ && "$MEMORY_MB" =~ ^[0-9]+$ && "$CORES" =~ ^[0-9]+$ ]] || die "--disk, --memory and --cores must be numbers"
[[ "$HOSTNAME_CT" =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$ ]] || die "invalid hostname: $HOSTNAME_CT"
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "invalid --repo: $REPO"
[[ "$REF" =~ ^[A-Za-z0-9_./-]+$ ]] || die "invalid --ref: $REF"
if [ "$IP" != "dhcp" ]; then
    [[ "$IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "--ip must be dhcp or an address with prefix, like 192.0.2.20/24"
    [ -n "$GATEWAY" ] || die "a static --ip also needs --gw"
fi
[ -z "$VLAN" ] || [[ "$VLAN" =~ ^[0-9]+$ ]] || die "--vlan must be a number"
[ -z "$SSH_KEY_FILE" ] || [ -r "$SSH_KEY_FILE" ] || die "cannot read the SSH key file $SSH_KEY_FILE"
if [ -n "$SOURCE" ]; then
    [ -d "$SOURCE" ] || [ -f "$SOURCE" ] || die "--source not found: $SOURCE"
    if [ -d "$SOURCE" ]; then [ -f "$SOURCE/install.sh" ] || die "$SOURCE does not look like a linkManager folder (no install.sh)"; fi
fi
ip link show "$BRIDGE" >/dev/null 2>&1 || die "bridge $BRIDGE does not exist"

if [ -z "$CTID" ]; then
    CTID=$(pvesh get /cluster/nextid)
fi
[[ "$CTID" =~ ^[0-9]+$ ]] || die "invalid container ID: $CTID"
if pct status "$CTID" >/dev/null 2>&1 || qm status "$CTID" >/dev/null 2>&1; then
    die "ID $CTID is already used by a container or a VM"
fi

# Storage for the root disk: the given one, or local-lvm / local-zfs / the first that accepts containers.
ROOTDIR_STORAGES=$(pvesm status -content rootdir 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1}')
if [ -z "$STORAGE" ]; then
    for s in local-lvm local-zfs; do
        if grep -qx "$s" <<<"$ROOTDIR_STORAGES"; then STORAGE=$s; break; fi
    done
    [ -n "$STORAGE" ] || STORAGE=$(head -n1 <<<"$ROOTDIR_STORAGES")
fi
[ -n "$STORAGE" ] || die "no active storage accepts containers (content rootdir)"
grep -qx "$STORAGE" <<<"$ROOTDIR_STORAGES" || die "storage $STORAGE does not exist, is not active or does not accept containers"
pvesm status -content vztmpl 2>/dev/null | awk 'NR > 1 {print $1}' | grep -qx "$TEMPLATE_STORAGE" \
    || die "storage $TEMPLATE_STORAGE does not accept templates (content vztmpl)"

# Debian template: the newest one already downloaded (13, then 12), or download it.
find_template() {
    pveam list "$TEMPLATE_STORAGE" 2>/dev/null | awk 'NR > 1 {print $1}' | sed 's|.*vztmpl/||' \
        | grep -E "^debian-$1-standard_.*\.tar\.(zst|xz|gz)$" | sort -V | tail -n1
}
TEMPLATE=$(find_template 13); [ -n "$TEMPLATE" ] || TEMPLATE=$(find_template 12)
DOWNLOAD_TEMPLATE=""
if [ -z "$TEMPLATE" ]; then
    info "Updating the list of templates"
    pveam update >/dev/null
    for v in 13 12; do
        DOWNLOAD_TEMPLATE=$(pveam available --section system | awk '{print $2}' | grep -E "^debian-$v-standard_" | sort -V | tail -n1)
        [ -n "$DOWNLOAD_TEMPLATE" ] && break
    done
    [ -n "$DOWNLOAD_TEMPLATE" ] || die "no Debian 12/13 template is available"
fi

NET="name=eth0,bridge=$BRIDGE,ip=$IP"
[ -z "$GATEWAY" ] || NET="$NET,gw=$GATEWAY"
[ -z "$VLAN" ] || NET="$NET,tag=$VLAN"
if [ -n "$SOURCE" ]; then FROM="local copy $SOURCE"; else FROM="github.com/$REPO ($REF)"; fi

cat <<EOF

linkManager will be installed in a new LXC container:
  ID / hostname : $CTID / $HOSTNAME_CT
  Template      : ${TEMPLATE:-$DOWNLOAD_TEMPLATE (will be downloaded to $TEMPLATE_STORAGE)}
  Resources     : $CORES core(s), $MEMORY_MB MB RAM, $DISK_GB GB on $STORAGE
  Network       : $NET
  Start on boot : $([ "$ONBOOT" = 1 ] && echo yes || echo no)
  Language      : $LANGUAGE
  Source        : $FROM

EOF
if [ "$ASSUME_YES" != 1 ]; then
    if [ -t 0 ]; then
        read -r -p "Continue? [Y/n] " answer
        [[ "${answer:-y}" =~ ^[YySs]$ ]] || { echo "Cancelled."; exit 0; }
    else
        die "no terminal to confirm: use -y to continue without asking"
    fi
fi

# --- Container ---------------------------------------------------------------
if [ -n "$DOWNLOAD_TEMPLATE" ]; then
    info "Downloading the template $DOWNLOAD_TEMPLATE"
    pveam download "$TEMPLATE_STORAGE" "$DOWNLOAD_TEMPLATE" >/dev/null
    TEMPLATE=$DOWNLOAD_TEMPLATE
fi

trap 'die "a command failed (line $LINENO); the installation did not finish"' ERR

info "Creating the container $CTID"
CREATE_ARGS=(--hostname "$HOSTNAME_CT" --ostype debian --unprivileged 1 --features nesting=1
             --cores "$CORES" --memory "$MEMORY_MB" --swap "$SWAP_MB" --rootfs "$STORAGE:$DISK_GB"
             --net0 "$NET" --onboot "$ONBOOT" --tags linkmanager
             --description "linkManager - https://github.com/$REPO")
[ -z "$SSH_KEY_FILE" ] || CREATE_ARGS+=(--ssh-public-keys "$SSH_KEY_FILE")
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" "${CREATE_ARGS[@]}" >/dev/null
CREATED=1
pct start "$CTID"

info "Waiting for the network"
in_ct() { pct exec "$CTID" -- env LC_ALL=C.UTF-8 LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive "$@"; }
for _ in $(seq 1 60); do
    if in_ct sh -c 'ip -4 route | grep -q "^default" && getent hosts deb.debian.org >/dev/null' 2>/dev/null; then
        break
    fi
    sleep 2
done
in_ct sh -c 'ip -4 route | grep -q "^default" && getent hosts deb.debian.org >/dev/null' \
    || die "the container has no network or DNS after 2 minutes (check the bridge, DHCP or --ip/--gw)"

# --- linkManager ---------------------------------------------------------------
info "Installing curl"
in_ct sh -c 'apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null'

SRC_DIR=/opt/linkmanager-src
in_ct sh -c "rm -rf $SRC_DIR && mkdir -p $SRC_DIR"
if [ -n "$SOURCE" ]; then
    info "Copying linkManager from $SOURCE"
    TMP_TAR=$(mktemp --suffix=.tar.gz)
    if [ -d "$SOURCE" ]; then
        tar -czf "$TMP_TAR" -C "$SOURCE" --exclude=.git .
        STRIP=0
    else
        cp "$SOURCE" "$TMP_TAR"
        STRIP=1
    fi
    pct push "$CTID" "$TMP_TAR" /tmp/linkmanager.tar.gz
    rm -f "$TMP_TAR"
    in_ct sh -c "tar -xzf /tmp/linkmanager.tar.gz -C $SRC_DIR --strip-components=$STRIP && rm /tmp/linkmanager.tar.gz"
else
    info "Downloading linkManager ($REF)"
    in_ct sh -c "curl -fsSL 'https://github.com/$REPO/archive/$REF.tar.gz' | tar -xz -C $SRC_DIR --strip-components=1"
fi
in_ct test -f "$SRC_DIR/install.sh" || die "install.sh was not found in the downloaded source"

info "Running the linkManager installer"
LOG=$(mktemp)
in_ct env LM_LANGUAGE="$LANGUAGE" bash "$SRC_DIR/install.sh" 2>&1 | tee "$LOG" | grep -E '^==>' || true
grep -q "^linkManager installed" "$LOG" || { echo "--- installer output:" >&2; tail -n 30 "$LOG" >&2; rm -f "$LOG"; die "the installer failed"; }
PASSWORD=$(sed -n 's/^User: admin *Password: //p' "$LOG")
[ -n "$PASSWORD" ] || PASSWORD="(unchanged: an existing password was kept)"
rm -f "$LOG"
trap - ERR

CT_IP=$(in_ct sh -c "hostname -I | awk '{print \$1}'")
cat <<EOF

linkManager is installed in container $CTID ($HOSTNAME_CT).

  Page  : http://$CT_IP/
  Panel : http://$CT_IP/admin/
  User  : admin
  Pass  : $PASSWORD

Change the password in Settings → Change the administrator password.
Console of the container: pct enter $CTID
Update linkManager later: run this inside the container
  curl -fsSL https://github.com/$REPO/archive/main.tar.gz | tar -xz -C $SRC_DIR --strip-components=1 && bash $SRC_DIR/install.sh
EOF
