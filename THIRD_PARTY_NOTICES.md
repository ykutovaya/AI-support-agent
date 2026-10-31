# Заимствованные материалы

## τ-bench

Используется пакет `tau2` версии 2.0.0 из [репозитория Sierra Research](https://github.com/sierra-research/tau2-bench/tree/da0835f6c67baef58bdda18f2a3b037dfb3a1754), commit `da0835f6c67baef58bdda18f2a3b037dfb3a1754`:

- В `src/support_agent/agent.py` сохранена [инструкция базового `LLMAgent`](https://github.com/sierra-research/tau2-bench/blob/da0835f6c67baef58bdda18f2a3b037dfb3a1754/src/tau2/agent/llm_agent.py) для сопоставимого контрольного прогона.
- Адаптер магазина использует типы данных и [инструменты retail](https://github.com/sierra-research/tau2-bench/blob/da0835f6c67baef58bdda18f2a3b037dfb3a1754/src/tau2/domains/retail/tools.py) из установленного пакета.
- Источник испытательного магазина, правил и заданий — [данные домена retail](https://github.com/sierra-research/tau2-bench/tree/da0835f6c67baef58bdda18f2a3b037dfb3a1754/data/tau2/domains/retail) той же версии. Исходные оценки и диалоги в `evals/retail-baseline/` и `evals/core-integration/` получены на этих данных; версия и настройки записаны в манифестах.

В репозитории τ-bench указана лицензия MIT. Полный текст [LICENSE](https://github.com/sierra-research/tau2-bench/blob/da0835f6c67baef58bdda18f2a3b037dfb3a1754/LICENSE) сохранён ниже.

## Прямые зависимости приложения

Версии закреплены в `pyproject.toml`. Ссылки на источники и обозначения лицензий сверены с метаданными установленных пакетов 31 октября 2026 года. Полные тексты лицензий поставляются с соответствующими дистрибутивами; таблица их не заменяет.

| Пакет | Версия | Источник | Лицензия |
|---|---|---|---|
| pydantic | 2.13.5 | [pydantic/pydantic](https://github.com/pydantic/pydantic) | MIT |
| jsonschema | 4.25.1 | [python-jsonschema/jsonschema](https://github.com/python-jsonschema/jsonschema) | MIT |
| litellm | 1.81.11 | [BerriAI/litellm](https://github.com/BerriAI/litellm) | MIT |
| python-dotenv | 1.2.4 | [theskumar/python-dotenv](https://github.com/theskumar/python-dotenv) | BSD-3-Clause |
| openai | 2.20.0 | [openai/openai-python](https://github.com/openai/openai-python) | Apache-2.0 |
| streamlit | 1.66.0 | [streamlit/streamlit](https://github.com/streamlit/streamlit) | Apache-2.0 |
| pytest, проверки | 8.3.5 | [pytest-dev/pytest](https://github.com/pytest-dev/pytest) | MIT |
| ruff, проверки | 0.11.7 | [astral-sh/ruff](https://github.com/astral-sh/ruff) | MIT |

## Собственные учебные материалы

`tests/retail_data.py`, `evals/reapproval/base_shop.json`, `evals/reapproval/policy.md` и `evals/reapproval/scenarios.json` — собственные синтетические примеры в формате retail. Они не являются исходными заданиями τ-bench или действующими правилами Softline. Результаты в `evals/reapproval/` и `evals/acceptance/` описывают эти отдельные испытания; происхождение данных также указано в [описании стенда](evals/reapproval/README.md).

## MIT License — Sierra Research

MIT License

Copyright (c) 2025 Sierra Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
