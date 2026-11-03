# Проверка полного расследования

Каждый случай начинается с нового обращения и отдельного учебного магазина.
Работают настоящее ядро и общий исполнитель в `manual/local`. Клиент и сотрудник
имитируются. Эталонные решения доступны оценщику, но не модели. Неизвестные
оценщику формулировки получают `review`.

Текущие версии: `investigation-v6` и `interventions-v3`. [Исправление текста](2026-11-03-grader-metrics/validation.json) и [итоговая проверка](2026-11-03-final-check/validation.json) сохранены отдельно от платных серий. [Исправленный набор v2](control-new-2026-11-03-v2-card.md) содержит целые обозначения товара и явную причину отзыва; это уже известные случаи для повторных проверок. В метриках v3 подтверждённый отзыв клиентом отделён от общего флага `revoked`, который ставится также после исполнения.

Загрузчик проверяет структуру данных с указанием случая и поля ошибки. Необязательное `interactions[].customer_intent` (`refusal`, `request_change`, `unknown`) используется только для метрик. Стенд сохраняет его связь с фактическим событием и отзывом в `customer-annotations.json` и `state.json`; модели передаётся только сообщение клиента. Без разметки причина остаётся неизвестной. Старые результаты не пересчитываются автоматически.

## Шесть новых контрольных обращений

Набор и отличия от прежних заданий описаны в [карточке данных](data_card.md).
Для выполненной серии согласованы до $5, шесть обращений и одно подключение,
без повторов. Для следующей серии нужны отдельное разрешение и новый каталог.

```powershell
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --init --output evals/investigation/new-control --control-file evals/investigation/control-new-2026-11-03.json --budget-usd 5
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --freeze --output evals/investigation/new-control
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --run --phase connection --output evals/investigation/new-control --key-env ../tau2-bench/.env --budget-usd 5
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --run --phase control --output evals/investigation/new-control --key-env ../tau2-bench/.env --budget-usd 5
```

`--control-file` принимает ровно шесть новых идентификаторов со `split=control`.
Файл копируется в серию как `control-cases.json`; его хеш проверяется перед
запусками. Старые задания не добавляются. Версии закрепляются до подключения.
Оценщик проверяется заранее на отдельных разработочных примерах. После начала
контроля данные, оценщик и приложение не меняются; `review` остаются в результате.

## Три известных случая

Команды из корня проекта, PowerShell. Ниже — параметры отдельно согласованной
проверки: `gpt-6-luna`, до $5, три обращения и одно подключение, без повторов.
Для нового запуска нужно отдельное разрешение; каталог должен быть новым.

```powershell
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --init --output evals/investigation/known-cases-new --budget-usd 5 --cases control-identity control-intent control-price
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --freeze --output evals/investigation/known-cases-new
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --run --phase connection --output evals/investigation/known-cases-new --key-env ../tau2-bench/.env --budget-usd 5
.\var\runtime\Scripts\python.exe evals/investigation/run_investigation.py --run --phase regression --output evals/investigation/known-cases-new --key-env ../tau2-bench/.env --budget-usd 5
```

`--init` сохраняет явный бюджет и выбранные случаи. Без `--cases` подготавливается
прежний набор из 4 разработочных и 6 контрольных обращений; перед фазой `control`
нужны завершённая разработочная часть и `--freeze`. Для выбранных известных
случаев версия закрепляется до первого API-вызова.

Повторы по умолчанию запрещены. Только при отдельном согласовании их число
задаётся `--max-repeats N` при создании серии, а запуск — `--repeat ID`.
Повтор команды фазы продолжает лишь ещё не начатые случаи; прерванные и
неуспешные попытки сохраняются. Исторические серии новым стендом не продолжаются.

Ограничения: 60 шагов и 600 секунд на обращение, запрос до 90 секунд,
8192 выходных токена, ноль технических повторов API. Расход резервируется до
запроса; при потерянном ответе сохраняется верхняя оценка. Подключение, ошибки и
повторы входят в общий предел. Время реального сотрудника не измеряется.

`series.json` закрепляет условия и задания, `freeze.json` — версии исполняемых
файлов. `attempts.json` содержит разрешённые пределы и список попыток;
`budget.json` учитывает все вызовы и расходы. В папке попытки сохраняются запросы,
ответы, состояние магазина, диалог, решения сотрудника и метрики `full_case`.
SQLite исключены из Git. Ключи и заголовки авторизации не сохраняются.

В `result.json` прежний `status` сохраняется. Поле `outcome` различает завершение
без исправлений, завершение с помощью, `review` и ошибку. Подсказка вызвать
инструмент отмечается `structured_action_hint_used`. Обязательные подтверждения
и штатное ручное продолжение не считаются исправлениями. Эти три известных
случая не являются новым независимым контролем и не заменяют исходные 4/6.

## Два известных ответа: проверка без API

Для поиска по имени и индексу с последующим обменом и для отказа после прежнего согласия:

```powershell
.\var\runtime\Scripts\python.exe -m evals.investigation.check_known_replies --set name-zip-refusal --output evals/investigation/offline-final-new
```

[Оба процесса завершены](2026-11-03-final-check/flows/summary.json); исходные `review` не изменены. Ниже — прежний набор с уточнением email, сохранённый как вариант по умолчанию.

```powershell
.\var\runtime\Scripts\python.exe -m evals.investigation.check_known_replies --output evals/investigation/offline-replies-new
```

Каталог должен быть новым. Подставная модель проходит уточнение email и отказ от
обмена через существующее ядро и исполнитель в `manual/local`. Клиент и
сотрудник имитируются; ключи не читаются, сеть заблокирована. Сохраняются версии,
точные ответы, состояние, события и результаты.

Команда использует две новые формулировки из [известных случаев](2026-11-03-known-cases/review.json).
[Проверка исправления](2026-11-03-offline-new-replies/summary.json) завершена
с `investigation-v4`. Это проверка заданных ответов, не новая оценка модели.
[Предыдущая проверка v3](2026-11-03-offline-replies/summary.json), исходные
[4/6](2026-11-03/summary.json) и платные `review` сохранены.
