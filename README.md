# CoVIDbot

Bot IRC proteksi channel, ditulis dengan Python. Ini adalah port dari script
eggdrop `Covid.5.0.tcl`, dan berjalan mandiri tanpa eggdrop.

Fitur utama:

- Koneksi tahan gangguan: keep-alive, deteksi lag, netsplit, dan server stoned,
  reconnect otomatis, serta TLS
- Identify otomatis ke NickServ
- Login owner dengan password, bukan hostmask
- Perintah channel: kick, ban, op, voice, topic, dan lainnya
- Guard: proteksi flood, join burst, serta balasan untuk kick dan deop
- Filter badword, daftar ignore, autovoice, dan rejoin setelah kick

## Kebutuhan

- Linux
- Python 3.11 atau lebih baru. Tidak ada dependency lain.
- systemd, kalau ingin menjalankannya sebagai service

## Pemasangan

```bash
tar xzf covidbot-5.0.tar.gz
cd CoVIDbot
cp config.example.toml config.toml
chmod 600 config.toml
```

Isi `config.toml`, minimal bagian ini:

```toml
[bot]
nick = "NamaBot"
channels = ["#channelanda"]
owner = "NickAnda"
owner_key = "kata-kunci-acak-minimal-12-karakter"

[[servers]]
host = "irc.contoh.net"
port = 6697
tls = true
```

Lalu jalankan:

```bash
python3 run.py
```

Tambahkan `--debug` untuk mencatat semua baris IRC. Password tetap disamarkan.
Berkas config lain bisa dipakai dengan `python3 run.py /path/ke/config.toml`.

## Membuat owner

Lakukan sekali, dari IRC, memakai nick yang sama dengan `owner` di config:

1. Kirim `/msg <bot> <owner_key>`.
2. Dalam 5 menit, kirim `/msg <bot> setpass <password>`. Password minimal 10
   karakter dan tanpa spasi.

Kata kunci berhenti berlaku begitu owner dibuat. Setelah itu baris `owner` dan
`owner_key` boleh dihapus dari config, tetapi harus keduanya sekaligus.

Untuk login berikutnya:

```
/msg <bot> auth <password>
/msg <bot> auth <handle> <password>     dari nick lain
```

Login berakhir saat `deauth`, `QUIT`, host berubah, atau bot terputus.

**Lupa password:** matikan bot, lalu jalankan
`python3 run.py --add-owner <handle>`.

## Menjalankan di background

Dengan akses root, pasang sebagai service systemd:

```bash
./install-systemd.sh
```

Bot tetap berjalan sebagai user biasa, yaitu pemilik folder proyek. Pilihan lain:

```bash
./install-systemd.sh --user NAMA    # jalankan sebagai user tertentu
./install-systemd.sh --dry-run      # lihat file service tanpa memasang
./install-systemd.sh --uninstall    # hapus service, data tetap ada
```

Perintah sehari-hari:

```bash
systemctl status covidbot
systemctl restart covidbot
systemctl stop covidbot
tail -f logs/covidbot.log
```

Service hidup lagi setelah reboot atau crash, tetapi tidak setelah `/msg <bot> die`.

## Perintah

Semua perintah butuh login, kecuali `auth`. Pengguna lain tidak mendapat balasan.

### Lewat `/msg <bot>`

| Perintah | Fungsi |
|---|---|
| `auth <password>` | Login |
| `deauth` | Logout |
| `join <#chan>` | Masuk channel, tersimpan setelah restart |
| `part <#chan>` | Keluar channel |
| `nick <nick> <password>` | Ganti nick bot dan password NickServ-nya |
| `realname <teks>` | Ganti realname, bot akan tersambung ulang |
| `identify <password>` | Identify manual ke NickServ |
| `ignore add <mask> [menit]` | Abaikan host, tanpa menit berarti permanen |
| `ignore del <mask>` | Hapus ignore |
| `ignore list` | Lihat daftar ignore |
| `cvdset <kategori> <opsi> <nilai>` | Ubah ambang proteksi, tersimpan |
| `!badword <kata> ["frasa"] [wild*]` | Tambah kata terlarang |
| `help` | Daftar perintah |
| `restart [alasan]` | Mulai ulang proses bot |
| `die [alasan]` | Matikan bot |

Kategori `cvdset`: `rejoin <delay/max/window/cooldown>`,
`automod <max/window/duration>`, dan `guard <grace>`.

### Di channel

| Perintah | Fungsi |
|---|---|
| `` `auth `` | Lihat status login |
| `` `deauth `` | Logout |
| `` `ver `` | Informasi bot |
| `` `logo `` | Tampilkan logo |
| `` `ping [nick] `` | CTCP ping |
| `` `k <nick> [alasan] `` | Kick |
| `` `kb <nick> [alasan] `` | Kick dan ban |
| `` `+b <nick> `` | Ban |
| `` `-b <nick/mask> [#chan] `` | Cabut ban |
| `` `mub [#chan] `` | Cabut semua ban |
| `` `+o `` / `` `-o [nick] `` | Beri atau cabut op |
| `` `+v `` / `` `-v [nick] `` | Beri atau cabut voice |
| `` `mv [#chan] `` | Voice massal |
| `` `mdv [#chan] `` | Cabut voice massal |
| `` `mode <mode> `` | Ubah mode channel |
| `` `topic <teks> `` | Ubah topic |
| `` `join <#chan> ``, `` `part <#chan> ``, `` `cycle `` | Keluar masuk channel |
| `` `+guard ``, `` `-guard `` | Nyalakan atau matikan guard |
| `` `+badword ``, `` `-badword `` | Nyalakan atau matikan filter badword |
| `` `status `` | Keadaan proteksi channel |
| `` `+f ``, `` `-f <nick> `` | Tambah atau hapus friend |
| `` `+av ``, `` `-av <nick> `` | Tambah atau hapus autovoice |
| `` `deluser <handle> `` | Hapus user |
| `` `userlist `` | Daftar user |

Sebagian perintah juga bisa dipanggil dengan nama bot, misalnya `NamaBot kb orang`.

## Pengaturan

Semua pilihan ada di `config.example.toml`. Yang paling sering dipakai:

| Bagian | Kunci | Keterangan |
|---|---|---|
| `[bot]` | `nick`, `alt_nick` | Nick utama dan cadangan |
| `[bot]` | `channels` | Channel yang dimasuki saat start |
| `[bot]` | `admins` | Nick penerima notifikasi lag, netsplit, dan reconnect |
| `[bot]` | `nickpass` | Password NickServ, kosong berarti tidak identify |
| `[bot]` | `nickserv` | Target NickServ, DALnet memakai `NickServ@services.dal.net` |
| `[bot]` | `umode` | Mode user setelah tersambung |
| `[[servers]]` | `host`, `port`, `tls` | Boleh lebih dari satu, dicoba bergantian |
| `[[servers]]` | `tls_verify` | Isi `false` untuk sertifikat self-signed |
| `[timing]` | `ping_interval`, `lag_warn`, `stoned_timeout` | Pengawasan koneksi, dalam detik |
| `[guard]` | `flood_*` | Batas flood, format `jumlah:detik` |
| `[guard]` | `opcmd` | Cara meminta op, `%c%` channel dan `%n%` nick bot |

## File data

Semua file ini dibuat otomatis dan tersimpan dengan izin 600:

| File | Isi |
|---|---|
| `data/users.json` | User, flag, dan hash password |
| `data/channels.json` | Channel tambahan serta status guard dan badword |
| `data/badwords.db` | Kata terlarang, satu per baris |
| `data/ignores.json` | Daftar ignore |
| `data/settings.json` | Nilai dari `cvdset` |
| `data/kickcount` | Nomor urut kick |

Log ada di `logs/covidbot.log`. Untuk pindah server, salin `config.toml` dan
folder `data/`. Jangan mengubah file data saat bot berjalan, karena akan tertimpa.

Daftar lama dari eggdrop bisa dipakai dengan menyalin `scripts/covid_badwords.db`
ke `data/badwords.db` saat bot mati.

## Hal yang perlu diketahui

- **Bot butuh op:** guard dan filter badword tidak bisa menghukum tanpa op.
  Daftarkan channel ke ChanServ dan beri bot auto-op, karena op hilang saat bot
  restart. Tanpa op selama 30 detik, guard mati sendiri dan harus dinyalakan lagi.
- **Pakai TLS di luar LAN:** password owner dan NickServ terkirim polos tanpa TLS.
- **Satu host, satu perlakuan:** friend dan autovoice dikenali dari
  `*!*@host`, dan hitungan flood juga per host.
- **Ban ditolak kalau terlalu luas:** ban yang juga mengenai bot, owner, atau friend
  tidak dipasang.
- **Jangan jalankan dua bot dari folder yang sama:** keduanya akan berebut data.
- **Ignore tidak cocok untuk spammer:** pengguna yang di-ignore lolos dari filter
  badword, walaupun guard tetap menghitung flood mereka.
