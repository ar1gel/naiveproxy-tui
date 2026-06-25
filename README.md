# NaiveProxy TUI

Terminal UI для управления [NaïveProxy](https://github.com/klzgrad/naiveproxy) — запуск/остановка клиента, редактирование конфигурации, просмотр логов и деплой сервера на VPS.

## Возможности

- **Dashboard** — статус процесса, быстрые клавиши для управления
- **Config Editor** — полноценный редактор `config.json` (listen, proxy, timeout, headers, resolver и т.д.)
- **Process Control** — запуск, остановка, перезапуск `naive` клиента с отображением PID
- **Log Viewer** — просмотр логов в реальном времени (follow/scroll режимы)
- **VPS Deploy** — деплой серверной части (Caddy + Naive fork of forwardproxy) на удалённый сервер через SSH
- **Zero dependencies** — только стандартная библиотека Python (curses)

## Установка

```sh
git clone https://github.com/klzgrad/naiveproxy
# Скачать бинарник naive для своей платформы из релизов
# Положить naive в ту же директорию

git clone https://github.com/klzgrad/naiveproxy  # или просто создать config.json вручную

# Установить TUI
git clone <this-repo> ~/naiveproxy-tui
cd ~/naiveproxy-tui
sudo make install
# Запуск:
naiveproxy-tui
```

### Без установки

```sh
cd ~/naiveproxy-tui
./naiveproxy-tui
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
