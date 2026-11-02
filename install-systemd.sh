#!/usr/bin/env bash
# Pasang CoVIDbot sebagai service systemd. Butuh akses root.
#
#   ./install-systemd.sh               pasang, aktifkan, dan jalankan
#   ./install-systemd.sh --user NAMA   jalankan bot sebagai user NAMA
#   ./install-systemd.sh --dry-run     tampilkan file service tanpa memasang
#   ./install-systemd.sh --uninstall   hentikan dan hapus service
#
# Bot tetap berjalan sebagai user biasa, bukan root. Tanpa --user, dipakai
# pemilik folder proyek. Data di folder proyek tidak pernah dihapus.

set -euo pipefail

readonly SERVICE=covidbot
readonly UNIT_FILE=/etc/systemd/system/covidbot.service

PROJECT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly PROJECT_DIR

die() {
    echo "Error: $*" >&2
    exit 1
}

action=install
run_user=$(stat -c %U "$PROJECT_DIR")
original_args=("$@")

while (($#)); do
    case $1 in
        --user)
            (($# >= 2)) || die "--user butuh nama user"
            run_user=$2
            shift 2
            ;;
        --dry-run) action=dry-run; shift ;;
        --uninstall) action=uninstall; shift ;;
        -h | --help)
            sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "opsi tidak dikenal: $1 (lihat --help)" ;;
    esac
done

render_unit() {
    cat <<EOF
[Unit]
Description=CoVIDbot IRC bot
Wants=network-online.target
After=network-online.target

[Service]
User=$run_user
WorkingDirectory=$PROJECT_DIR
ExecStart=$python_bin run.py
Restart=on-failure
RestartSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
}

as_run_user() {
    runuser -u "$run_user" -- "$@"
}

require_root() {
    ((EUID == 0)) && return
    command -v sudo >/dev/null || die "butuh akses root, dan sudo tidak tersedia"
    echo "Butuh akses root, mencoba lewat sudo..."
    exec sudo -- bash "$PROJECT_DIR/install-systemd.sh" "${original_args[@]}"
}

uninstall() {
    if [[ ! -e $UNIT_FILE ]]; then
        echo "Service $SERVICE tidak terpasang."
        return
    fi
    systemctl disable --now "$SERVICE"
    rm -f -- /etc/systemd/system/covidbot.service
    systemctl daemon-reload
    echo "Service $SERVICE dihapus. Data di $PROJECT_DIR tidak diubah."
}

install() {
    id -u "$run_user" >/dev/null 2>&1 || die "user tidak ada: $run_user"
    [[ $(id -u "$run_user") != 0 ]] ||
        die "bot tidak boleh berjalan sebagai root; pakai --user NAMA"
    "$python_bin" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
        die "butuh Python 3.11 atau lebih baru"
    [[ -f $PROJECT_DIR/config.toml ]] ||
        die "config.toml belum ada; salin dari config.example.toml lalu isi"

    as_run_user test -w "$PROJECT_DIR" ||
        die "user $run_user tidak bisa menulis ke $PROJECT_DIR"

    # Gagal di sini lebih jelas daripada service yang terus gagal start.
    (cd -- "$PROJECT_DIR" &&
        as_run_user "$python_bin" -B -c \
            'from covidbot.config import load_config; load_config("config.toml")') ||
        die "config.toml tidak valid"

    chown -- "$run_user" "$PROJECT_DIR/config.toml"
    chmod 600 -- "$PROJECT_DIR/config.toml" # berisi password

    if pgrep -u "$run_user" -f "$python_bin run\.py|python3 run\.py" >/dev/null; then
        echo "Peringatan: ada bot lain yang berjalan sebagai $run_user." >&2
        echo "Matikan dulu, supaya dua bot tidak berebut nick dan data." >&2
    fi

    render_unit >"$UNIT_FILE"
    chmod 644 -- "$UNIT_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE"
    systemctl restart "$SERVICE"

    sleep 2
    if ! systemctl is-active --quiet "$SERVICE"; then
        journalctl -u "$SERVICE" -n 20 --no-pager >&2 || true
        die "service gagal berjalan"
    fi

    cat <<EOF

CoVIDbot berjalan sebagai user $run_user dan akan hidup lagi setelah reboot.

  systemctl status $SERVICE      lihat keadaan bot
  systemctl restart $SERVICE     setelah mengubah kode atau config
  systemctl stop $SERVICE        matikan
  tail -f $PROJECT_DIR/logs/covidbot.log
EOF
}

python_bin=$(command -v python3) || die "python3 tidak ditemukan"

case $action in
    dry-run)
        render_unit
        ;;
    uninstall)
        require_root
        uninstall
        ;;
    install)
        command -v systemctl >/dev/null || die "systemd tidak tersedia"
        require_root
        install
        ;;
esac
