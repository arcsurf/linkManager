#!/usr/bin/env bash
# Instalador de linkManager para Debian/Ubuntu (nginx + systemd).
# Uso: sudo ./install.sh
# Es idempotente: no sobrescribe links, página ni contraseña ya existentes.
set -euo pipefail

WEB_ROOT=/var/www/linkmanager
APP_DIR=/opt/linkmanager
DATA_DIR=/var/lib/linkmanager
BACKUP_DIR=/var/backups/linkmanager
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "Ejecutalo como root (sudo ./install.sh)"; exit 1; }

echo "==> Instalando dependencias"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -yq nginx python3 openssl

echo "==> Copiando la aplicación"
install -d -m 755 "$APP_DIR"
install -m 644 "$SRC/app.py" "$APP_DIR/app.py"
install -d -o www-data -g www-data -m 750 "$DATA_DIR" "$BACKUP_DIR"
install -d -o www-data -g www-data -m 755 "$WEB_ROOT" "$WEB_ROOT/images" "$WEB_ROOT/css" "$WEB_ROOT/admin"
install -o www-data -g www-data -m 644 "$SRC/admin/index.html" "$WEB_ROOT/admin/index.html"
[ -f "$WEB_ROOT/css/estilos.css" ] || install -o www-data -g www-data -m 644 "$SRC/public/css/estilos.css" "$WEB_ROOT/css/"
[ -f "$WEB_ROOT/images/link.svg" ] || install -o www-data -g www-data -m 644 "$SRC/public/images/link.svg" "$WEB_ROOT/images/"
if [ ! -f "$WEB_ROOT/index.html" ]; then
    install -o www-data -g www-data -m 644 "$SRC/public/index.html" "$WEB_ROOT/index.html"
fi
if [ ! -f "$DATA_DIR/links.json" ]; then
    install -o www-data -g www-data -m 640 "$SRC/data/links.example.json" "$DATA_DIR/links.json"
fi

PASS=""
if [ ! -f "$DATA_DIR/htpasswd" ]; then
    PASS=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-16)
    printf 'admin:%s\n' "$(printf '%s\n' "$PASS" | openssl passwd -6 -stdin)" > "$DATA_DIR/htpasswd"
    chown www-data:www-data "$DATA_DIR/htpasswd"
    chmod 640 "$DATA_DIR/htpasswd"
fi

echo "==> Configurando systemd y nginx"
install -m 644 "$SRC/deploy/linkmanager.service" /etc/systemd/system/linkmanager.service
if [ ! -f /etc/nginx/sites-available/linkmanager ]; then
    install -m 644 "$SRC/deploy/nginx-site.conf" /etc/nginx/sites-available/linkmanager
    ln -sf /etc/nginx/sites-available/linkmanager /etc/nginx/sites-enabled/linkmanager
    # El sitio por defecto de nginx también escucha en el puerto 80
    rm -f /etc/nginx/sites-enabled/default
fi
nginx -t
systemctl daemon-reload
systemctl enable --now linkmanager
systemctl restart linkmanager
systemctl reload nginx

# Genera la página con los links actuales
sleep 1
runuser -u www-data -- env LM_WEB_ROOT="$WEB_ROOT" LM_DATA="$DATA_DIR/links.json" \
    LM_BACKUP_DIR="$BACKUP_DIR" python3 -I -c \
    "import sys; sys.path.insert(0, '$APP_DIR'); import app; app.save(app.load())"

echo
echo "linkManager instalado: http://$(hostname -I | awk '{print $1}')/  (panel en /admin/)"
if [ -n "$PASS" ]; then
    echo "Usuario: admin   Contraseña: $PASS"
    echo "Cambiala desde el panel (sección 'Cambiar contraseña')."
fi
