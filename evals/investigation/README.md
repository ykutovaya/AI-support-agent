# Проверка полного расследования

Каждый случай начинается с нового обращения и отдельного учебного магазина.
Работают настоящее ядро и общий исполнитель, режимы `manual/local`.
Клиент и сотрудник имитируются по закреплённым правилам. Эталонные решения
используются проверкой сотрудника и оценщиком, в запросы модели не входят.

Набор: 4 разработочных и 6 контрольных обращений в `cases.json`.
Проверяются обмен, возврат, передача специалисту, уточнение email, несколько
заказов, изменение намерения и цены, недопустимый обмен. Новые формулировки,
которых не понимает ограниченный оценщик, получают `review`.

Команды из корня проекта, PowerShell. Каталог серии должен быть новым:

```powershell
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --init --output evals/investigation/new-series
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --run --phase connection --output evals/investigation/new-series --key-env ../tau2-bench/.env --budget-usd 5
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --run --phase development --output evals/investigation/new-series --key-env ../tau2-bench/.env --budget-usd 5
# После разбора разработочных попыток и проверки необходимых исправлений:
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --freeze --output evals/investigation/new-series
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --run --phase control --output evals/investigation/new-series --key-env ../tau2-bench/.env --budget-usd 5
```

Эти команды описывают воспроизведение; новую платную серию нужно согласовать.
Для серии от 3 ноября согласованы `gpt-6-luna`, $5 на все вызовы, одна проверка
подключения, 4 разработочных, 6 контрольных и до 4 дополнительных попыток.
Повторы запускаются явно через `--repeat ID` вместо `--phase`; сохраняются как
отдельные попытки и не становятся новыми незнакомыми случаями. Повтор команды
фазы продолжает только ещё не начатые случаи. Прерванные записи не перезаписываются.

Ограничения: 60 шагов и 600 секунд на обращение, запрос до 90 секунд,
8192 выходных токена, ноль технических повторов API. Расход резервируется до
запроса; при потерянном ответе сохраняется верхняя оценка. Общий журнал включает
подключение, ошибки и повторы. Время человека не измеряется.

`series.json` закрепляет условия и задания, `freeze.json` — версию перед
контролем. `attempts.json` и `budget.json` учитывают все запуски и расходы.
Папка попытки содержит запросы и ответы модели, состояние магазина и диалога,
решения имитируемого сотрудника, итоговую оценку и метрики `full_case`.
Локальные SQLite исключены из Git. Секреты и заголовки авторизации не сохраняются.

## Два известных ответа: проверка без API

```powershell
.\var\runtime\Scripts\python.exe -m evals.investigation.check_known_replies --output evals/investigation/offline-replies-new
```

Каталог должен быть новым. Подставная модель проходит уточнение email и отказ от обмена через существующее ядро и исполнитель в `manual/local`. Клиент и сотрудник имитируются; ключи не читаются, сеть заблокирована. Сохраняются версии, точные ответы, состояние, события и результаты. Исходные данные и прежние попытки не перезаписываются.

[Проверка исправления](2026-11-03-offline-replies/summary.json) использует `investigation-v3`. Это два известных случая на заданных ответах, не новая оценка модели. Исходные 4/6 остаются в [прежней серии](2026-11-03/summary.json).
