# Предупреждения Netdata для WB OPS

`health.d/wb-ops-resources.conf` добавляет четыре независимые проверки. Штатные
правила Netdata сохраняются. Проверки видны в Netdata и `/api/v1/alarms?all`;
`to: silent` отключает только внешние уведомления. Для Telegram/email/Cloud нужен
отдельно выбранный пользователем канал и его настройка.

| Проверка | Warning | Critical | Окно / задержка уведомлений |
| --- | --- | --- | --- |
| Доступная RAM (MemAvailable) | <15% | <7% | среднее за минуту, задержка 30 с |
| Место на файловой системе | >85% занято или <2 GiB | >95% или <1 GiB | задержка 1 мин |
| Load5 / число CPU | >1.5 | >3 | пятиминутная нагрузка, задержка 2 мин |
| OOM | — | хотя бы одно завершение за 30 мин | факт события сохраняется в окне 30 мин |

Гистерезис снижает дребезг статуса; усреднение памяти и Load5 сглаживает пики.
`delay` задерживает только выполнение уведомлений, статус в dashboard меняется
при пересечении порога сразу. При `to: silent` уведомления не отправляются;
задержки уже заданы для будущего подключения канала. Проверка диска охватывает
корневую ФС и отдельные разделы `/opt`/данных; псевдо-ФС и маленькие системные
разделы `/boot` и `/boot/*` исключены, поскольку они не хранят отчёты WB OPS.

## Установка на сервере

Выполнять из проверенного релиза WB OPS. Для стандартной пакетной установки
пользовательский каталог — `/etc/netdata`; для static install часто
`/opt/netdata/etc/netdata`. Сначала найдите текущую установку и её `health config`
в `[directories]` (`netdata.conf`); не копируйте конфиг в штатный `conf.d`.

```sh
command -v netdatacli
curl -fsS http://127.0.0.1:19999/netdata.conf
```

После подтверждения каталога (ниже пример для пакетной установки):

```sh
sudo install -d -o root -g root -m 0755 /etc/netdata/health.d
sudo install -o root -g root -m 0644 deploy/netdata/health.d/wb-ops-resources.conf /etc/netdata/health.d/wb-ops-resources.conf
sudo netdatacli reload-health
```

`reload-health` применяет правила без рестарта службы. Если `netdatacli` установлен
в `/opt/netdata/usr/sbin`, используйте его абсолютный путь. Изменения nginx/SSH
не нужны. Если health глобально выключен или `enabled alarms` исключает `wb_ops_*`,
это нужно исправить в действующей конфигурации Netdata до проверки правил.

## Проверка

Локально (Node.js без дополнительных пакетов):

```sh
node deploy/netdata/test_health_config.cjs
node deploy/netdata/test_dashboard.cjs
```

После reload подождите 60–90 секунд для привязки к собираемым метрикам:

```sh
curl -fsS 'http://127.0.0.1:19999/api/v1/alarms?all' | python3 -c 'import json,sys; a=[v for v in json.load(sys.stdin)["alarms"].values() if v.get("name", "").startswith("wb_ops_")]; print(json.dumps(a, ensure_ascii=False, indent=2)); expected={"wb_ops_available_memory", "wb_ops_disk_space", "wb_ops_system_load", "wb_ops_oom_kills"}; assert expected <= {v["name"] for v in a}, "Не все WB OPS alarms привязались к метрикам"; assert all(v.get("status") not in ("UNDEFINED", "UNINITIALIZED") for v in a), "Проверьте доступность метрик и выражения"'
sudo journalctl -u netdata --since '-5 min' --no-pager
```

Команда должна показать все четыре имени (дисковых экземпляров может быть
несколько), статус `CLEAR`, `WARNING` или `CRITICAL`, без ошибок парсинга в журнале.
Если `mem.available` или `mem.oom_kill` не собираются, выясните состояние Linux
`/proc` collector по `/api/v1/charts` и журналу; отсутствие alarm нельзя считать
успешной установкой. Полная проверка требует работающего Linux Netdata: локальная
статическая проверка не заменяет загрузку правил самим агентом.

Откат только этих правил: удалите `/etc/netdata/health.d/wb-ops-resources.conf`
и выполните `sudo netdatacli reload-health`. Штатные правила сохранятся.

Основа синтаксиса: [официальный справочник Netdata](https://learn.netdata.cloud/docs/alerts-%26-notifications/alert-configuration-reference),
[штатные RAM rules](https://github.com/netdata/netdata/blob/master/src/health/health.d/ram.conf),
[штатные disk rules](https://github.com/netdata/netdata/blob/master/src/health/health.d/disks.conf),
[штатные load rules](https://github.com/netdata/netdata/blob/master/src/health/health.d/load.conf).
