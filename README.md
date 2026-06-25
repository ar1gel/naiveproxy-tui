# NaiveProxy TUI

Terminal UI для управления [NaïveProxy](https://github.com/klzgrad/naiveproxy) — запуск/остановка клиента, редактирование конфигурации, просмотр логов и деплой сервера на VPS.

## Возможности

- **Dashboard** — статус процесса, быстрые клавиши для управления
- **Config Editor** — полноценный редактор `config.json` (listen, proxy, timeout, headers, resolver и т.д.)
- **Process Control** — запуск, остановка, перезапуск `naive` клиента с отображением PID
- **Log Viewer** — просмотр логов в реальном времени (follow/scroll режимы)
- **VPS Deploy** — деплой серверной части (Caddy + Naive fork of forwardproxy) на удалённый сервер через SSH
- **Zero dependencies** — только стандартная библиотека Python (curses)

## Быстрый старт

```sh
# 1. Клонировать и запустить TUI (без установки)
git clone https://github.com/ar1gel/naiveproxy-tui.git && cd naiveproxy-tui && ./naiveproxy-tui
```

TUI сам покажет подсказки. Для работы нужно:

1. Скачать [naiveproxy](https://github.com/klzgrad/naiveproxy/releases/latest) бинарник для своей платформы
2. Положить `naive` рядом с `naiveproxy-tui` (или в `$PATH`)
3. Настроить `config.json` через Config Editor в TUI
4. Запустить клиент (кнопка `1` на Dashboard или Process Control)

### Деплой на VPS (одной командой)

В TUI перейти на экран **VPS Deploy** (`←/→`), заполнить поля (host, domain, email, auth) и нажать `d`.

Либо вручную:

```sh
ssh root@your-vps 'bash -s' <<'SCRIPT'
apt-get update -qq && apt-get install -y -qq curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq && apt-get install -y -qq caddy golang-go
go install github.com/caddyserver/xcaddy/cmd/xcaddy@latest
~/go/bin/xcaddy build --with github.com/caddyserver/forwardproxy=github.com/klzgrad/forwardproxy@naive -o /usr/bin/caddy-naive
setcap cap_net_bind_service=+ep /usr/bin/caddy-naive
cat > /etc/caddy/Caddyfile <<EOF
:443, your-domain.com {
  tls you@email.com
  forward_proxy {
    basic_auth user pass
    hide_ip
    hide_via
    probe_resistance
  }
  file_server { root /var/www/html }
}
EOF
cat > /etc/systemd/system/caddy-naive.service << 'UNIT'
[Unit]
Description=Caddy Naive Proxy
After=network.target

[Service]
Type=notify
ExecStart=/usr/bin/caddy-naive run --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=always
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload && systemctl enable --now caddy-naive
SCRIPT
```

## Использование

### Навигация

| Клавиша | Действие |
|---------|----------|
| `←` `→` / `Tab` | Переключение между экранами |
| `↑` `↓` / `j` `k` | Навигация внутри экрана |
| `Enter` | Редактировать поле |
| `q` / `Esc` | Назад / Выйти |
| `s` | Сохранить (Config Editor, VPS) |
| `d` | Дефолтные настройки / Деплой |

### Dashboard (`1`)

Быстрый доступ ко всем функциям. Статус процесса, текущая конфигурация.

### Config Editor

Редактирование всех параметров `naive`:
- listen (SOCKS5/HTTP/redir)
- proxy (https/quic)
- log, concurrency, timeouts
- extra-headers, host-resolver-rules
- post-quantum toggle

### VPS Deploy

Заполните поля:
- **VPS Host/IP** — адрес сервера
- **SSH Port / User** — подключение
- **Domain** — ваш домен (должен смотреть на VPS)
- **Email** — для Let's Encrypt TLS
- **Proxy Auth User/Pass** — для авторизации клиентов

Скрипт автоматически:
1. Устанавливает Caddy на VPS
2. Собирает Naïve fork of forwardproxy через xcaddy
3. Настраивает Caddyfile с `probe_resistance`
4. Запускает systemd-сервис `caddy-naive`

## Структура проекта

```
naiveproxy-tui/
├── main.py              # TUI приложение (curses)
├── naiveproxy-tui       # entry point (скрипт)
├── Makefile             # установка/удаление
├── naive-client.service # systemd user unit для клиента
├── LICENSE
└── README.md
```

## Зависимости

- Python 3.7+
- `naive` бинарник (скачать из [релизов](https://github.com/klzgrad/naiveproxy/releases))
- `ssh` (для VPS деплоя, опционально)
- Никаких сторонних Python-пакетов

## Советы

1. Если `naive` не в PATH, положите его в ту же директорию что и TUI
2. Для `systemd --user` автостарта клиента: `systemctl --user enable naive-client.service`
3. VPS деплой использует `probe_resistance` — ваш сервер не будет отвечать на probe-запросы
4. Для HTTP/3 используйте `quic://` в proxy URI
